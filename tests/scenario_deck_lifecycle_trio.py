"""
Scenario: three deck-lifecycle defects (issues #11, #12, #15).

  #11: on a background-video page, update_all_inputs skipped ALL key device
       writes (the per-frame video loop paints keys) -- but the video loop
       deliberately never repaints fully-opaque keys, so those never got
       their FIRST paint after a page switch: the device kept showing the
       previous page's content until a keypress. Opaque keys now get their
       initial update() (their tile hides the video; the write cannot
       disturb it).
  #12: close() step 6 ran plugin teardown hooks unbounded on the DeckClose
       thread -- a wedged hook stranded steps 7-9 forever (unplug leak
       reintroduced) and _closing=True made retry a permanent no-op. Now a
       bounded join; on timeout the hook thread is abandoned and teardown
       completes.
  #15: close() neither bumped _page_load_generation nor cancelled
       _bg_future, so an in-flight load_background could attach a fresh
       BackgroundVideo AFTER step 7's resource sweep -- leaked until process
       exit. close() now invalidates the generation and cancels the future.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import concurrent.futures
import threading

import globals as gl
from fixtures import make_headless_controller, raw_deck, start_watchdog, wait_until

from src.backend.DeckManagement.InputIdentifier import Input


def check_opaque_initial_paint() -> int:
    controller = make_headless_controller(serial="trio-11")
    try:
        # Deterministic tier: stop the live writer and drive the drain by
        # hand (the pattern scenario_touchscreen_write_cap established) --
        # otherwise the live loop races the assertions. Drain the load's
        # leftover tasks before arming the check.
        controller.media_player.stop(timeout=3.0)
        controller.media_player.perform_media_player_tasks()

        # Opaque page-color on key 0; the others stay transparent.
        opaque_key = controller.inputs[Input.Key][0]
        opaque_key.get_active_state().background_manager.set_page_color(
            [10, 20, 30, 255], update=False)

        # The branch under test only checks `background.video is not None`.
        controller.background.video = object()
        try:
            deck = raw_deck(controller)
            deck.clear_journal()
            # A real page switch delivers NEW content; here the page stays,
            # so drop the dedup hashes or the repaint is (correctly) skipped
            # as identical.
            controller._reset_dedup_hashes()
            controller.update_all_inputs()
            controller.media_player.perform_media_player_tasks()

            writes = deck.ops_by_name("set_key_image")
        finally:
            controller.background.video = None

        opaque_slot = "key:0"  # key 0 at rotation 0
        opaque_written = any(e[3] == opaque_slot for e in writes)
        others_written = [e for e in writes if e[3] != opaque_slot]
        if not opaque_written:
            print("FAIL(#11): the opaque key got no initial device paint on "
                  "a bg-video page -- the device would keep showing the "
                  "previous page's content until a keypress")
            return 1
        if others_written:
            print(f"FAIL(#11): non-opaque keys were device-written in the "
                  f"bg-video branch (would fight the video loop): "
                  f"{others_written}")
            return 1
        print("PASS: opaque keys get their initial paint; video keys stay "
              "with the loop")
        return 0
    finally:
        fixtures.teardown(controller)


def check_close_gen_invalidation() -> int:
    controller = make_headless_controller(serial="trio-15")
    page = controller.active_page

    attached = []
    controller.background.set_from_path = lambda *a, **k: attached.append(k)

    fut = concurrent.futures.Future()
    controller._bg_future = fut

    gen_before_close = controller._page_load_generation

    fixtures.teardown(controller)  # drives the real close()

    if not fut.cancelled():
        print("FAIL(#15): close() did not cancel the in-flight background "
              "future")
        return 1

    # An in-flight load that captured its gen BEFORE close must now abort
    # instead of attaching a fresh BackgroundVideo post-sweep.
    controller.load_background(page, update=False, gen=gen_before_close)
    if attached:
        print("FAIL(#15): a load that predates close() attached a "
              "background AFTER the resource sweep -- leaked until process "
              "exit")
        return 1
    print("PASS: close() invalidates in-flight loads and cancels the "
          "background future")
    return 0


def check_bounded_teardown() -> int:
    controller = make_headless_controller(serial="trio-12")
    type(controller).TEARDOWN_JOIN_TIMEOUT_S = 0.5

    wedge = threading.Event()
    controller._teardown_actions = lambda: wedge.wait(timeout=30)

    done = threading.Event()

    def run_close():
        controller.close(remove_media=True)
        done.set()

    closer = threading.Thread(target=run_close, daemon=True)
    closer.start()
    if not done.wait(timeout=8):
        wedge.set()
        print("FAIL(#12): close() stranded behind a wedged teardown hook -- "
              "steps 7-9 never ran and _closing=True makes retry a "
              "permanent no-op (unplug leak)")
        return 1

    # Steps 7-9 actually completed despite the wedge.
    if controller in gl.page_manager.pages:
        print("FAIL(#12): controller never deregistered from the page cache")
        wedge.set()
        return 1
    if controller.active_page is not None:
        print("FAIL(#12): active_page not released")
        wedge.set()
        return 1
    wedge.set()
    print("PASS: close() completes past a wedged teardown hook (bounded join)")
    return 0


def main() -> int:
    start_watchdog(60, "deck_lifecycle_trio")
    rc = check_opaque_initial_paint()
    rc |= check_close_gen_invalidation()
    rc |= check_bounded_teardown()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
