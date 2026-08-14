"""
Three small guards.
"""

# Page.set_media_fps applies only to inputs on a controller showing that page.
# mark_page_ready_to_clear releases the page it captured at the False call, not
# whatever is active at the True call. initialize_actions claims on_ready
# atomically under a per-page lock, at both of its entry points.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from fixtures import make_headless_controller, seed_page, start_watchdog

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

    # Point controller B at its own page. Both headless controllers otherwise
    # resolve the same default page, which would pass the active-page filter.
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
        print(f"FAIL(a): the deck actually showing the page did not get "
              f"the fps change: {vid_a.calls}")
        return 1
    if vid_b.calls:
        print("FAIL(a): editing one page's FPS rebased the playing video "
              "timeline on another deck showing a DIFFERENT page")
        return 1
    print("PASS: set_media_fps applies only where the page is showing")
    return 0


def check_ready_to_clear_repoint(controller) -> int:
    page_a = controller.active_page
    page_b_path = seed_page("MarkSwapB")
    page_b = gl.page_manager.get_page(page_b_path, controller)

    # The baseline the bracket must return to, taken after the fetch above.
    # An "unpinned" assertion would pass on that fetch retiring page_a's own
    # reservation rather than on the bracket balancing.
    holders_before = gl.page_manager.pins.count(page_a)

    # Run the bracket the tick loop and the key handler use, with a page
    # switch landing in the middle, as a slow on_tick allows.
    captured = controller.mark_page_ready_to_clear(False)
    controller.active_page = page_b  # concurrent switch mid-work
    try:
        controller.mark_page_ready_to_clear(True, captured)
    except TypeError:
        # An older signature takes no page parameter. Call it the old way, so
        # the failure below is the semantic one rather than a TypeError.
        controller.mark_page_ready_to_clear(True)

    if not fixtures.wait_until(
            lambda: gl.page_manager.pins.count(page_a) <= holders_before):
        print("FAIL(b): the old page stayed pinned against eviction forever "
              "-- unevictable, silently shrinking the eviction budget")
        return 1
    print("PASS: the bracket resets the page it marked, not whatever is "
          "active now")
    return 0


def _make_claim_probe(barrier):
    """Build a barrier-rendezvous ActionCore probe and return the instance.

    Both ready-claim checks share it, so they drive the same interleave. The
    on_ready_called getter reads the flag before any rendezvous. A True read
    returns at once, which marks the serialized second reader.
    """
    # A False read waits on a two-party barrier to force the concurrent-read
    # interleave.
    from src.backend.PluginManager.ActionCore import ActionCore

    class ClaimProbeAction(ActionCore):
        def __init__(self):
            # super().__init__ needs full deck wiring, which this probe does
            # not use. _cleaned_up True makes the framework teardown skip it.
            self.__dict__["_ready"] = False
            self.on_ready_finished = False
            self._cleaned_up = True
            self._cleanup_lock = threading.Lock()

        @property
        def on_ready_called(self):
            value = self.__dict__["_ready"]
            if value:
                return value  # the serialized second reader
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
            print("FAIL(c): initialize_actions deadlocked")
            return 1

    if len(submits) != 1:
        print(f"FAIL(c): {len(submits)} concurrent ready claims for one "
              f"action (duplicate on_ready -> duplicate backend processes)")
        return 1
    print("PASS: exactly one ready claim under concurrent initialize_actions")
    return 0


def check_atomic_ready_claim_reload_path(controller) -> int:
    """The second entry point for the ready claim.

    Page.load calls initialize_actions when this page is already active, so a
    reload picks up newly-added actions.
    """
    # The per-page lock sits at the claim site, so a reload racing a direct
    # initialize_actions must still yield exactly one ready claim for the
    # shared action instance.
    page = controller.active_page
    barrier = threading.Barrier(2)
    action = _make_claim_probe(barrier)
    page.action_objects["reloadprobe"] = {"0x0": {0: {0: action}}}

    submits = []
    page._submit_ready_callbacks = lambda a: submits.append(a)

    # Thread 1 drives the reload entry point, where Page.load re-runs
    # initialize_actions because active_page is this page. Thread 2 drives a
    # direct initialize_actions. Both funnel through _ready_claim_lock.
    controller.active_page = page  # so load()'s active_page gate passes

    def reload_entry():
        # load() rebuilds action_objects, so patch get_all_actions to
        # re-inject the probe. The claim serialization is under test here,
        # not load()'s file I/O.
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
            print("FAIL(c-reload): initialize_actions deadlocked")
            return 1

    if len(submits) != 1:
        print(f"FAIL(c-reload): {len(submits)} concurrent ready claims for "
              f"one action via the reload entry point (Page.py:214) -- the "
              f"claim lock does not cover the reload path")
        return 1
    print("PASS: reload path (Page.py:214) shares the claim lock -- exactly "
          "one ready claim")
    return 0


def check_ready_to_clear_evicts_end_to_end(controller) -> int:
    """ready_to_clear, end to end.

    A page that was mid-work, marked ready_to_clear False and then reset, must
    become evictable again through clear_old_cached_pages.
    """
    # Without the pass-back the page stays pinned and survives eviction
    # forever, which silently shrinks the budget.
    import globals as gl

    pm = gl.page_manager
    saved_max = pm.max_pages
    # A roomy budget during setup, so the clear_old_cached_pages that
    # get_page runs after each load evicts no candidate before the bracket.
    pm.max_pages = 50

    try:
        active = controller.active_page  # never evictable, so it stays put

        # The page caught mid-work is cached, non-active and loaded first, so
        # it is the oldest and the eviction candidate.
        pinned_path = seed_page("EvictPinned")
        pinned_page = gl.page_manager.get_page(pinned_path, controller)
        if pinned_page is active:
            print("FAIL(setup): candidate resolved to the active page")
            return 1

        # A few newer cached pages, so there is genuine excess to evict.
        for name in ("EvictNewer1", "EvictNewer2"):
            gl.page_manager.get_page(seed_page(name), controller)

        # Run the tick bracket on pinned_page with a page switch landing
        # mid-work. Capture at the False call, swap active_page away, then
        # reset through the captured page at the True call.
        controller.active_page = pinned_page
        captured = controller.mark_page_ready_to_clear(False)
        controller.active_page = active  # concurrent switch mid-work
        try:
            controller.mark_page_ready_to_clear(True, captured)
        except TypeError:
            # An older signature takes no page parameter, so the True call
            # re-reads the now-active page and leaves pinned_page stuck. Call
            # it the old way, so the eviction assertion reports the failure.
            controller.mark_page_ready_to_clear(True)

        cached_before = set(gl.page_manager.pages.get(controller, {}).keys())
        if pinned_path not in cached_before:
            print("FAIL(setup): pinned_page was not cached")
            return 1
        if pinned_page is controller.active_page:
            print("FAIL(setup): pinned_page is the active page -- can't test "
                  "eviction of a non-active page")
            return 1

        # Tighten the budget and run the real eviction pass. pinned_page is
        # the oldest non-active entry and is ready_to_clear, so it must go.
        pm.max_pages = 2
        gl.page_manager.clear_old_cached_pages()

        cached_after = set(gl.page_manager.pages.get(controller, {}).keys())
        if pinned_path in cached_after:
            print("FAIL(b): a page marked ready_to_clear mid-work stayed "
                  "pinned and was NOT evicted by clear_old_cached_pages -- "
                  "unevictable forever, silently shrinking the eviction budget")
            return 1
        print("PASS: a page reset after mid-work is actually evicted "
              "end-to-end by clear_old_cached_pages")
        return 0
    finally:
        pm.max_pages = saved_max


def check_ready_to_clear_key_handler(controller) -> int:
    """ready_to_clear on the key-handler path.

    A real key press that triggers a page change lands the switch between the
    False call and the True call. The old, pressed page must be reset, not
    whatever page the press switched to."""
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

    # Inject the page switch mid-callback. event_callback calls self.update()
    # after mark_page_ready_to_clear(False) and before the matching True call,
    # which is where a press that changes the page lands.
    real_update = key.update
    switched = {"done": False}
    held_mid_callback = {"count": -1}

    def switching_update(*a, **k):
        if not switched["done"]:
            switched["done"] = True
            # Sampled inside the bracket, because the hold must exist while
            # the callback runs. A balance at the end alone is satisfied by a
            # key site that takes no hold at all.
            held_mid_callback["count"] = gl.page_manager.pins.count(pressed_page)
            controller.active_page = switched_to  # the page change the press caused
        return real_update(*a, **k)

    key.update = switching_update
    try:
        # The baseline the bracket must return to, and the floor the sample
        # above must clear. The two samples must be adjacent, because the tick
        # loop brackets the active page once a second and a window straddling
        # only one of them shifts the comparison in either direction.
        holders_before = gl.page_manager.pins.count(pressed_page)
        key.event_callback(press_state=True)  # key DOWN
    finally:
        key.update = real_update

    if not switched["done"]:
        print("FAIL(setup): the mid-callback switch injection never ran")
        return 1
    if held_mid_callback["count"] <= holders_before:
        print(f"FAIL(b-key): mid-callback the pressed page had "
              f"{held_mid_callback['count']} holders against a pre-press "
              f"{holders_before} -- the key handler took NO hold on it, so a "
              f"press that outlives a page switch is evictable mid-gesture")
        return 1
    if not fixtures.wait_until(
            lambda: gl.page_manager.pins.count(pressed_page) <= holders_before):
        print("FAIL(b-key): a key press that switched pages left the OLD "
              "(pressed) page pinned against eviction -- the key-handler "
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
