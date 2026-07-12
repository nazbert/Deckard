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
import hashlib
import threading

from PIL import Image

import globals as gl
from fixtures import make_headless_controller, raw_deck, start_watchdog, wait_until

from src.backend.DeckManagement.DeckController import encode_native_key
from src.backend.DeckManagement.InputIdentifier import Input


def _expected_native_hash(controller, key) -> str:
    """The journal fingerprint (faulty_fake_deck._hash_bytes: sha1[:12]) the
    device SHOULD receive for `key`, computed by encoding its current
    composed image through the exact path ControllerKey.update() uses
    (RGBA->RGB paste, rotate, encode_native_key). Lets the #11 check assert
    the opaque key's write carried the NEW page's opaque color, not the
    previous page's stale content."""
    image = key.get_current_image()
    if image.mode == "RGBA":
        rgb = Image.new("RGB", image.size, (0, 0, 0))
        rgb.paste(image, (0, 0), image)
        rgb = rgb.rotate(controller.deck.get_rotation())
    else:
        rgb = image.convert("RGB").rotate(controller.deck.get_rotation())
    native = encode_native_key(controller.deck, rgb)
    return hashlib.sha1(bytes(native)).hexdigest()[:12]


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
        opaque_color = [10, 20, 30, 255]
        opaque_key.get_active_state().background_manager.set_page_color(
            opaque_color, update=False)

        # The branch under test only checks `background.video is not None`.
        controller.background.video = object()
        try:
            deck = raw_deck(controller)
            deck.clear_journal()
            # A real page switch delivers NEW content; here the page stays,
            # so drop the dedup hashes or the repaint is (correctly) skipped
            # as identical.
            controller._reset_dedup_hashes()
            # Reference hash BEFORE the paint (get_current_image is stable for
            # the current color) -- what the device SHOULD receive for the
            # opaque key.
            expected_hash = _expected_native_hash(controller, opaque_key)
            controller.update_all_inputs()
            controller.media_player.perform_media_player_tasks()

            writes = deck.ops_by_name("set_key_image")
        finally:
            controller.background.video = None

        opaque_slot = "key:0"  # key 0 at rotation 0
        opaque_writes = [e for e in writes if e[3] == opaque_slot]
        others_written = [e for e in writes if e[3] != opaque_slot]
        if not opaque_writes:
            print("FAIL(#11): the opaque key got no initial device paint on "
                  "a bg-video page -- the device would keep showing the "
                  "previous page's content until a keypress")
            return 1
        if others_written:
            print(f"FAIL(#11): non-opaque keys were device-written in the "
                  f"bg-video branch (would fight the video loop): "
                  f"{others_written}")
            return 1
        # CONTENT, not just presence (#11 review r1): the write must carry the
        # NEW opaque color's bytes, not the previous page's stale content. The
        # journal records _hash_bytes(native) at index 4.
        written_hash = opaque_writes[-1][4]
        if written_hash != expected_hash:
            print(f"FAIL(#11): opaque key was painted, but with the WRONG "
                  f"content (journal {written_hash} != expected "
                  f"{expected_hash} for the new opaque color) -- a stale/"
                  f"previous-page frame reached the device")
            return 1
        # Differential: a DIFFERENT opaque color must yield DIFFERENT bytes --
        # guards against the check passing vacuously (e.g. if expected_hash
        # were computed from a constant).
        opaque_key.get_active_state().background_manager.set_page_color(
            [200, 120, 40, 255], update=False)
        other_hash = _expected_native_hash(controller, opaque_key)
        if other_hash == expected_hash:
            print("FAIL(#11): content hash does not vary with the opaque "
                  "color -- the content assertion is vacuous")
            return 1
        print("PASS: opaque keys get their initial paint WITH the new page's "
              "content; video keys stay with the loop")
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


def check_close_load_race() -> int:
    """Residual #15 window (review r1): the gen-bump + future.cancel() in
    close() close the window for a load that has NOT yet reached its
    _page_is_current(gen) gate. They do NOT cover a load already PAST that
    gate, parked inside the seconds-long prebuild, when close()'s step-7 sweep
    runs -- its freshly built BackgroundVideo (cv2 capture) would land on
    self.background.video AFTER the sweep and leak until process exit. This
    drives exactly that interleaving through the REAL apply_prebuilt path."""
    controller = make_headless_controller(serial="trio-15race")
    page = controller.active_page
    background = controller.background

    class _FakeVideo:
        """Stands in for a prebuilt BackgroundVideo: `close()` is what the
        residual-window fix must call on the orphaned payload (mirrors the
        real cv2-capture release)."""
        def __init__(self):
            self.closed = False
            self.video_path = "/fake/race.mp4"

        def close(self):
            self.closed = True

    fake_video = _FakeVideo()

    past_gate = threading.Event()   # load has passed the gen-gate, is prebuilding
    release = threading.Event()     # test lets the parked prebuild finish

    def blocking_prebuild(path, fps=30, loop=True, allow_keep=True):
        # We are called from set_from_path, which load_background calls AFTER
        # its _page_is_current(gen) gate -- i.e. we are past the gate. Park
        # here (as a real multi-second decode would) until the test has run
        # close()'s sweep, then hand back a fresh "video" payload for
        # apply_prebuilt to (try to) attach.
        past_gate.set()
        release.wait(timeout=5)
        return ("video", fake_video)

    background.prebuild_from_path = blocking_prebuild

    # set_video would close a previous video; make sure there is none, and
    # give apply_prebuilt's real "video" branch a set_video that records the
    # attach faithfully (the real one calls update_all_inputs, which needs a
    # live deck -- keep the observable effect, drop the fan-out).
    background.video = None
    attached = {}
    background.set_video = lambda video, update=True: attached.__setitem__("video", video)

    gen = controller._page_load_generation

    def run_load():
        controller.load_background(page, update=False, gen=gen)

    loader = threading.Thread(target=run_load, name="race-load", daemon=True)
    loader.start()
    if not past_gate.wait(timeout=3):
        print("SETUP-FAIL(#15race): loader never reached prebuild past its gate")
        release.set()
        return 1

    # Close the deck while the load is parked past its gate. close() sets
    # _closing, bumps gen, cancels the future, and (step 7) sweeps the
    # background under _background_load_lock -- which blocks on the loader if
    # it already holds the lock, or runs first if it does not. Either way,
    # once we release the loader, apply_prebuilt's _closing re-check must
    # suppress the attach and discard (close) the orphaned payload.
    closer = threading.Thread(
        target=lambda: controller.close(remove_media=True), name="race-close", daemon=True)
    closer.start()
    # Give close() a beat to set _closing and reach/enter the sweep.
    closer_ready = wait_until(lambda: controller._closing, timeout=3)
    if not closer_ready:
        print("SETUP-FAIL(#15race): close() never set _closing")
        release.set()
        return 1

    release.set()
    loader.join(timeout=3)
    closer.join(timeout=3)

    if attached.get("video") is fake_video:
        print("FAIL(#15race): a load past its gen-gate attached a fresh "
              "BackgroundVideo AFTER close() -- cv2 capture leaks until "
              "process exit (gen-bump + future.cancel do NOT cover the "
              "already-past-the-gate interleaving)")
        return 1
    if background.video is fake_video:
        print("FAIL(#15race): fresh background left attached on the closed "
              "controller")
        return 1
    if not fake_video.closed:
        print("FAIL(#15race): the orphaned prebuilt payload was dropped "
              "without close() -- its cv2 capture leaks")
        return 1
    print("PASS: a load past its gate cannot attach a background after "
          "close(); the orphaned payload is released")
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
    rc |= check_close_load_race()
    rc |= check_bounded_teardown()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
