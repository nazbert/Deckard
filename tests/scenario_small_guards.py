"""
Scenario: three small guards (issues #14, #16, #127).

  #14: Page.set_media_fps live-applied to matching inputs on ALL
       controllers -- editing one page's FPS row rebased the playing video
       timeline on another deck showing a DIFFERENT page. Now filtered by
       active page, like update_input.
  #16: mark_page_ready_to_clear dereferenced active_page at call time; a
       page switch during a slow on_tick left the OLD page pinned
       ready_to_clear=False forever (unevictable). The bracket now captures
       the page at the False-call and resets that same object.
  #127: initialize_actions' bare check-then-set on on_ready_called let two
       concurrent load_page(samePage) calls both claim the same action --
       a second concurrent on_ready (duplicate backends). The claim is now
       atomic under a per-page lock. Made deterministic with a
       barrier-in-getter action stub.
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


def check_atomic_ready_claim(controller) -> int:
    page = controller.active_page
    barrier = threading.Barrier(2)

    from src.backend.PluginManager.ActionCore import ActionCore

    class ClaimProbeAction(ActionCore):
        """Subclasses ActionCore (get_all_actions filters on isinstance) but
        skips its __init__. on_ready_called reads rendezvous on a barrier:
        two truly concurrent readers (the pre-fix interleave) both pass;
        under the claim lock the second reader is held until the first has
        set the flag, so its barrier times out and it sees True."""

        def __init__(self):
            # Deliberately NOT calling super().__init__ (needs a full deck
            # wiring irrelevant here). _cleaned_up=True makes the framework
            # teardown skip this probe.
            self.__dict__["_ready"] = False
            self.on_ready_finished = False
            self._cleaned_up = True
            self._cleanup_lock = threading.Lock()

        @property
        def on_ready_called(self):
            # Capture BEFORE the rendezvous: the race under test is two
            # threads both reading False before either sets True. Reading
            # after the barrier would let the faster thread's set land
            # first and mask the pre-fix double claim.
            value = self.__dict__["_ready"]
            try:
                barrier.wait(timeout=2.0)
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

    action = ClaimProbeAction()
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


def main() -> int:
    start_watchdog(40, "small_guards")
    controller_a = make_headless_controller(serial="guards-a", page_name="FpsPageA")
    controller_b = make_headless_controller(serial="guards-b", page_name="FpsPageB")

    try:
        rc = check_fps_bleed(controller_a, controller_b)
        rc |= check_ready_to_clear_repoint(controller_b)
        rc |= check_atomic_ready_claim(controller_a)
    finally:
        fixtures.teardown(controller_a)
        fixtures.teardown(controller_b)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
