"""
Scenario: three small guards (issues #14, #16, #127).

  #14: Page.set_media_fps live-applied to matching inputs on ALL
       controllers -- editing one page's FPS row rebased the playing video
       timeline on another deck showing a DIFFERENT page. Now filtered by
       active page, like update_input.
  #16: mark_page_ready_to_clear dereferenced active_page at call time; a
       page switch during a slow on_tick left the OLD page pinned
       ready_to_clear=False forever (unevictable). The bracket now captures
       the page at the False-call and resets that same object. Covered three
       ways: the flag value after a tick-shape swap; the same swap driven
       END-TO-END through clear_old_cached_pages (the page must actually be
       evicted -- the whole point of the fix); and the real key-handler path
       (event_callback 3706/3737) with a press-triggered page change landing
       mid-bracket.
  #127: initialize_actions' bare check-then-set on on_ready_called let two
       concurrent load_page(samePage) calls both claim the same action --
       a second concurrent on_ready (duplicate backends). The claim is now
       atomic under a per-page lock. Made deterministic with a
       barrier-in-getter action stub. Covered both entry points: the
       load_page tail and the reload path (Page.py:214).
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from fixtures import make_headless_controller, seed_page, start_watchdog, wait_until

import globals as gl
from src.backend.DeckManagement.InputIdentifier import Input


class FakeVideo:
    """Well-behaved for the controller's live media loop (which composites
    active-state videos while the scenario runs)."""

    def __init__(self):
        self.loop = True
        self.calls = []
        from PIL import Image
        self._frame = Image.new("RGBA", (72, 72), (0, 0, 0, 255))

    def set_playback(self, fps=None, loop=None):
        self.calls.append(fps)

    def get_raw_image(self):
        return self._frame

    def get_next_frame(self, *a, **k):
        return self._frame

    def close(self):
        pass


def check_fps_bleed(controller_a, controller_b) -> int:
    ident = Input.Key("0x0")
    page_a = controller_a.active_page

    # Point controller B at its OWN page -- both headless controllers
    # otherwise resolve the same default page, which would legitimately
    # pass the active-page filter.
    page_b = gl.page_manager.get_page(seed_page("FpsDistinctB"), controller_b)
    controller_b.active_page = page_b
    if page_b.json_path == page_a.json_path:
        print("FAIL(setup): controllers share a page path")
        return 1

    vid_a = FakeVideo()
    vid_b = FakeVideo()
    controller_a.get_input(ident).states[0].key_video = vid_a
    controller_b.get_input(ident).states[0].key_video = vid_b

    page_a.set_media_fps(ident, 0, 24, update=False)

    if vid_a.calls != [24]:
        print(f"FAIL(#14): the deck actually showing the page did not get "
              f"the fps change: {vid_a.calls}")
        return 1
    if vid_b.calls:
        print("FAIL(#14): editing one page's FPS rebased the playing video "
              "timeline on another deck showing a DIFFERENT page")
        return 1
    print("PASS: set_media_fps applies only where the page is showing")
    return 0


def check_ready_to_clear_repoint(controller) -> int:
    page_a = controller.active_page
    page_b_path = seed_page("MarkSwapB")
    page_b = gl.page_manager.get_page(page_b_path, controller)

    # Simulate the bracket the tick loop / key handler runs, with a page
    # switch landing in the middle (exactly what a slow on_tick allows).
    captured = controller.mark_page_ready_to_clear(False)
    controller.active_page = page_b  # concurrent switch mid-work
    try:
        controller.mark_page_ready_to_clear(True, captured)
    except TypeError:
        # Pre-fix signature (no page parameter): call the old way so the
        # red run reports the semantic failure below, not a TypeError.
        controller.mark_page_ready_to_clear(True)

    if not page_a.ready_to_clear:
        print("FAIL(#16): the old page stayed pinned ready_to_clear=False "
              "forever -- unevictable, silently shrinking the eviction "
              "budget")
        return 1
    print("PASS: the bracket resets the page it marked, not whatever is "
          "active now")
    return 0


def _make_claim_probe(barrier):
    """Builds a barrier-rendezvous ActionCore probe and returns the instance.
    Shared by both #127 tests (load_page-vs-load_page and the reload path) so
    they drive the SAME deterministic interleave.

    The probe subclasses ActionCore (get_all_actions filters on isinstance) but
    skips its __init__. The on_ready_called GETTER captures the flag BEFORE any
    rendezvous, then:
      * if it already read True -> returns at once (the fast path the SECOND
        reader hits under the fix, serialized behind the claim lock after the
        first thread set the flag: a POSITIVE signal that serialization
        happened, so the green run pays no barrier timeout);
      * if it read False -> rendezvous on a two-party barrier to force the
        concurrent-read interleave the race needs (both threads reading False
        before either sets True). With the fix only ONE thread ever reaches
        here with False (the lock holder) and its wait expires on the SHORT
        timeout -- correct: no double claim. The pre-fix concurrent case
        rendezvouses near-instantly and never approaches the timeout.
    Reading AFTER the barrier instead would let the faster thread's set land
    first and mask the pre-fix double claim."""
    from src.backend.PluginManager.ActionCore import ActionCore

    class ClaimProbeAction(ActionCore):
        def __init__(self):
            # Deliberately NOT calling super().__init__ (needs full deck wiring
            # irrelevant here). _cleaned_up=True makes framework teardown skip
            # this probe.
            self.__dict__["_ready"] = False
            self.on_ready_finished = False
            self._cleaned_up = True
            self._cleanup_lock = threading.Lock()

        @property
        def on_ready_called(self):
            value = self.__dict__["_ready"]
            if value:
                return value  # fast positive path (fix present, 2nd reader)
            try:
                barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            return value

        @on_ready_called.setter
        def on_ready_called(self, value):
            self.__dict__["_ready"] = value

        def load_event_overrides(self):
            pass

        def load_initial_generative_ui(self):
            pass

    return ClaimProbeAction()


def check_atomic_ready_claim(controller) -> int:
    page = controller.active_page
    barrier = threading.Barrier(2)
    action = _make_claim_probe(barrier)
    page.action_objects["claimprobe"] = {"0x0": {0: {0: action}}}

    submits = []
    page._submit_ready_callbacks = lambda a: submits.append(a)

    threads = [threading.Thread(target=page.initialize_actions, daemon=True)
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        if t.is_alive():
            print("FAIL(#127): initialize_actions deadlocked")
            return 1

    if len(submits) != 1:
        print(f"FAIL(#127): {len(submits)} concurrent ready claims for one "
              f"action (duplicate on_ready -> duplicate backend processes)")
        return 1
    print("PASS: exactly one ready claim under concurrent initialize_actions")
    return 0


def check_atomic_ready_claim_reload_path(controller) -> int:
    """#127, second entry point: Page.load() calls initialize_actions() at
    Page.py:214 when this page is already active (reload picks up newly-added
    actions). The issue only named the load_page path; the fix's per-page lock
    is at the claim site, so it covers the reload trigger too -- assert it. A
    reload racing a direct initialize_actions must still yield exactly ONE
    ready claim for the shared action instance."""
    page = controller.active_page
    barrier = threading.Barrier(2)
    action = _make_claim_probe(barrier)
    page.action_objects["reloadprobe"] = {"0x0": {0: {0: action}}}

    submits = []
    page._submit_ready_callbacks = lambda a: submits.append(a)

    # Thread 1 drives the REAL reload entry point (Page.load re-runs
    # initialize_actions because active_page is this page); thread 2 drives a
    # direct initialize_actions (the load_page tail). Both funnel through the
    # same instance's _ready_claim_lock.
    controller.active_page = page  # ensure load()'s `active_page == self` gate passes

    def reload_entry():
        # load() rebuilds action_objects, so re-inject the probe right before
        # its initialize_actions() call by patching get_all_actions for this
        # run -- we're testing the CLAIM serialization, not load()'s file I/O.
        page.get_all_actions = lambda: [action]
        page.initialize_actions()

    def direct_entry():
        page.get_all_actions = lambda: [action]
        page.initialize_actions()

    threads = [threading.Thread(target=reload_entry, daemon=True),
               threading.Thread(target=direct_entry, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        if t.is_alive():
            print("FAIL(#127-reload): initialize_actions deadlocked")
            return 1

    if len(submits) != 1:
        print(f"FAIL(#127-reload): {len(submits)} concurrent ready claims for "
              f"one action via the reload entry point (Page.py:214) -- the "
              f"claim lock does not cover the reload path")
        return 1
    print("PASS: reload path (Page.py:214) shares the claim lock -- exactly "
          "one ready claim")
    return 0


def check_ready_to_clear_evicts_end_to_end(controller) -> int:
    """#16, end-to-end: the point of the fix is not the flag value in isolation
    -- it's that a page which was mid-work (marked ready_to_clear=False, then
    correctly reset) becomes EVICTABLE again via clear_old_cached_pages. Fill
    the cache past max_pages so a non-active, older page is eligible; put it
    through the tick bracket with a page switch landing mid-work; then run the
    real eviction pass and assert the page is actually evicted. Without the
    pass-back fix the page stays pinned ready_to_clear=False and survives
    eviction forever -- silently shrinking the budget."""
    import globals as gl

    pm = gl.page_manager
    saved_max = pm.max_pages
    # Roomy budget during setup so get_page's own internal
    # clear_old_cached_pages (fired after every load) doesn't evict our
    # candidate before we've put it through the bracket.
    pm.max_pages = 50

    try:
        active = controller.active_page  # never evictable; stays put

        # The page that will be caught mid-work: cached, non-active, and
        # (loaded first) the oldest -> the eviction candidate.
        pinned_path = seed_page("EvictPinned")
        pinned_page = gl.page_manager.get_page(pinned_path, controller)
        if pinned_page is active:
            print("FAIL(setup): candidate resolved to the active page")
            return 1

        # A couple of newer cached pages so there's genuine excess to evict.
        for name in ("EvictNewer1", "EvictNewer2"):
            gl.page_manager.get_page(seed_page(name), controller)

        # Run the tick bracket ON pinned_page, with a page switch landing
        # mid-work (exactly the slow-on_tick interleave issue #16 describes):
        # capture at the False-call, swap active_page away, reset via the
        # captured page at the True-call.
        controller.active_page = pinned_page
        captured = controller.mark_page_ready_to_clear(False)
        controller.active_page = active  # concurrent switch mid-work
        try:
            controller.mark_page_ready_to_clear(True, captured)
        except TypeError:
            # Pre-fix signature (no page param): the True-call re-derefs the
            # now-active page and leaves pinned_page stuck False. Call the old
            # way so the eviction assertion below reports the failure, not a
            # TypeError.
            controller.mark_page_ready_to_clear(True)

        cached_before = set(gl.page_manager.pages.get(controller, {}).keys())
        if pinned_path not in cached_before:
            print("FAIL(setup): pinned_page was not cached")
            return 1
        if pinned_page is controller.active_page:
            print("FAIL(setup): pinned_page is the active page -- can't test "
                  "eviction of a non-active page")
            return 1

        # Now tighten the budget and run the REAL eviction pass. pinned_page is
        # the oldest non-active entry; with the fix it is ready_to_clear=True
        # and MUST be evicted. Pre-fix it is pinned False and survives.
        pm.max_pages = 2
        gl.page_manager.clear_old_cached_pages()

        cached_after = set(gl.page_manager.pages.get(controller, {}).keys())
        if pinned_path in cached_after:
            print("FAIL(#16): a page marked ready_to_clear mid-work stayed "
                  "pinned and was NOT evicted by clear_old_cached_pages -- "
                  "unevictable forever, silently shrinking the eviction budget")
            return 1
        print("PASS: a page reset after mid-work is actually evicted "
              "end-to-end by clear_old_cached_pages")
        return 0
    finally:
        pm.max_pages = saved_max


def check_ready_to_clear_key_handler(controller) -> int:
    """#16, key-handler path (event_callback 3706/3737): a real key press that
    triggers a page change lands the switch BETWEEN the False-call and the
    True-call. Drive the real ControllerKey.event_callback and inject the page
    switch at self.update() (line 3709, between the bracket's two calls). The
    OLD (pressed) page must be reset -- not whatever page the press switched
    to. Currently only the tick-loop shape was covered."""
    from src.backend.DeckManagement.InputIdentifier import Input

    ident = Input.Key("0x0")
    key = controller.get_input(ident)
    if key is None:
        print("FAIL(setup): no ControllerKey 0x0 on the headless controller")
        return 1

    pressed_page = controller.active_page
    switched_to = gl.page_manager.get_page(seed_page("KeyHandlerSwitch"),
                                           controller)
    if switched_to is pressed_page:
        print("FAIL(setup): switch target resolved to the pressed page")
        return 1

    # Make sure both pages start evictable-clean so the assertion is meaningful.
    pressed_page.ready_to_clear = True
    switched_to.ready_to_clear = True

    # Inject the page switch mid-callback: event_callback calls self.update()
    # at line 3709, AFTER mark_page_ready_to_clear(False) (3706) and BEFORE
    # mark_page_ready_to_clear(True, pressed_page) (3737). A press that changes
    # the page does exactly this.
    real_update = key.update
    switched = {"done": False}

    def switching_update(*a, **k):
        if not switched["done"]:
            switched["done"] = True
            controller.active_page = switched_to  # the page change the press caused
        return real_update(*a, **k)

    key.update = switching_update
    try:
        key.event_callback(press_state=True)  # key DOWN
    finally:
        key.update = real_update

    if not switched["done"]:
        print("FAIL(setup): the mid-callback switch injection never ran")
        return 1
    if not pressed_page.ready_to_clear:
        print("FAIL(#16-key): a key press that switched pages left the OLD "
              "(pressed) page pinned ready_to_clear=False -- the key-handler "
              "bracket re-dereferenced active_page instead of the pressed page")
        return 1
    print("PASS: the key-handler bracket resets the pressed page, not the "
          "page the press switched to")
    return 0


def main() -> int:
    start_watchdog(40, "small_guards")
    controller_a = make_headless_controller(serial="guards-a", page_name="FpsPageA")
    controller_b = make_headless_controller(serial="guards-b", page_name="FpsPageB")

    try:
        rc = check_fps_bleed(controller_a, controller_b)
        rc |= check_ready_to_clear_repoint(controller_b)
        rc |= check_ready_to_clear_evicts_end_to_end(controller_b)
        rc |= check_ready_to_clear_key_handler(controller_b)
        rc |= check_atomic_ready_claim(controller_a)
        rc |= check_atomic_ready_claim_reload_path(controller_a)
    finally:
        fixtures.teardown(controller_a)
        fixtures.teardown(controller_b)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
