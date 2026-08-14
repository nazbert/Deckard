"""Scenario for three deck-lifecycle defects.

An opaque key gets its first paint after a page switch. close() joins plugin
teardown hooks with a bound, and cancels an in-flight background load.
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
    """The journal fingerprint the device should receive for key.

    Computed by encoding the current composed image through the exact path
    ControllerKey.update() uses, so a check can assert the new page's color.
    """
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
        # Deterministic tier. Stop the live writer and drive the drain by hand,
        # or the live loop races the assertions. Drain the leftover tasks of the
        # load before arming the check.
        controller.media_player.stop(timeout=3.0)
        controller.media_player.perform_media_player_tasks()

        # Opaque page-color on key 0. The others stay transparent.
        opaque_key = controller.inputs[Input.Key][0]
        opaque_color = [10, 20, 30, 255]
        opaque_key.get_active_state().background_manager.set_page_color(
            opaque_color, update=False)

        # The branch under test only checks background.video for None.
        controller.background.video = object()
        try:
            deck = raw_deck(controller)
            deck.clear_journal()
            # A real page switch delivers new content. The page stays here, so
            # drop the dedup hashes or the repaint is skipped as identical.
            controller._reset_dedup_hashes()
            # Reference hash taken before the paint, because get_current_image
            # is stable for the current color. This is what the device should
            # receive for the opaque key.
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
            print("FAIL(a): the opaque key got no initial device paint on "
                  "a bg-video page -- the device would keep showing the "
                  "previous page's content until a keypress")
            return 1
        if others_written:
            print(f"FAIL(a): non-opaque keys were device-written in the "
                  f"bg-video branch (would fight the video loop): "
                  f"{others_written}")
            return 1
        # Check the content, not just the presence. The write must carry the
        # bytes of the new opaque color, not the previous page's stale content.
        # The journal records _hash_bytes(native) at index 4.
        written_hash = opaque_writes[-1][4]
        if written_hash != expected_hash:
            print(f"FAIL(a): opaque key was painted, but with the WRONG "
                  f"content (journal {written_hash} != expected "
                  f"{expected_hash} for the new opaque color) -- a stale/"
                  f"previous-page frame reached the device")
            return 1
        # Differential check. A different opaque color must yield different
        # bytes, which guards against the check passing vacuously.
        opaque_key.get_active_state().background_manager.set_page_color(
            [200, 120, 40, 255], update=False)
        other_hash = _expected_native_hash(controller, opaque_key)
        if other_hash == expected_hash:
            print("FAIL(a): content hash does not vary with the opaque "
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
        print("FAIL(c): close() did not cancel the in-flight background "
              "future")
        return 1

    # An in-flight load that captured its gen before close must abort instead of
    # attaching a fresh BackgroundVideo after the sweep.
    controller.load_background(page, update=False, gen=gen_before_close)
    if attached:
        print("FAIL(c): a load that predates close() attached a "
              "background AFTER the resource sweep -- leaked until process "
              "exit")
        return 1
    print("PASS: close() invalidates in-flight loads and cancels the "
          "background future")
    return 0


def check_close_load_race() -> int:
    """A load already past its gen gate, parked inside the prebuild.

    The gen bump and future.cancel() in close() do not cover it, so its freshly
    built BackgroundVideo would land on self.background.video after the step-7
    sweep and leak. This drives that interleaving through apply_prebuilt.
    """
    controller = make_headless_controller(serial="trio-15race")
    page = controller.active_page
    background = controller.background

    class _FakeVideo:
        """Stands in for a prebuilt BackgroundVideo.

        close() is what the fix must call on the orphaned payload, which
        mirrors the real cv2 capture release.
        """
        def __init__(self):
            self.closed = False
            self.video_path = "/fake/race.mp4"

        def close(self):
            self.closed = True

    fake_video = _FakeVideo()

    past_gate = threading.Event()   # load has passed the gen-gate, is prebuilding
    release = threading.Event()     # test lets the parked prebuild finish

    def blocking_prebuild(path, fps=30, loop=True, allow_keep=True):
        # Called from set_from_path, which load_background calls after its
        # _page_is_current(gen) gate, so this is past the gate. Park here, as a
        # real multi-second decode would, until the test has run the close()
        # sweep, then hand back a fresh video payload for apply_prebuilt.
        past_gate.set()
        release.wait(timeout=5)
        return ("video", fake_video)

    background.prebuild_from_path = blocking_prebuild

    # set_video would close a previous video, so make sure there is none. Give
    # the real video branch of apply_prebuilt a set_video that records the attach
    # faithfully, because the real one calls update_all_inputs and needs a live
    # deck.
    background.video = None
    attached = {}
    background.set_video = lambda video, update=True: attached.__setitem__("video", video)

    gen = controller._page_load_generation

    def run_load():
        controller.load_background(page, update=False, gen=gen)

    loader = threading.Thread(target=run_load, name="race-load", daemon=True)
    loader.start()
    if not past_gate.wait(timeout=3):
        print("SETUP-FAIL(c-race): loader never reached prebuild past its gate")
        release.set()
        return 1

    # Close the deck while the load is parked past its gate. close() sets
    # _closing, bumps gen, cancels the future, and sweeps the background under
    # _background_load_lock. Once the loader is released, the _closing re-check
    # in apply_prebuilt must suppress the attach and close the orphaned payload.
    closer = threading.Thread(
        target=lambda: controller.close(remove_media=True), name="race-close", daemon=True)
    closer.start()
    # Give close() a beat to set _closing and reach the sweep.
    closer_ready = wait_until(lambda: controller._closing, timeout=3)
    if not closer_ready:
        print("SETUP-FAIL(c-race): close() never set _closing")
        release.set()
        return 1

    release.set()
    loader.join(timeout=3)
    closer.join(timeout=3)

    if attached.get("video") is fake_video:
        print("FAIL(c-race): a load past its gen-gate attached a fresh "
              "BackgroundVideo AFTER close() -- cv2 capture leaks until "
              "process exit (gen-bump + future.cancel do NOT cover the "
              "already-past-the-gate interleaving)")
        return 1
    if background.video is fake_video:
        print("FAIL(c-race): fresh background left attached on the closed "
              "controller")
        return 1
    if not fake_video.closed:
        print("FAIL(c-race): the orphaned prebuilt payload was dropped "
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
        print("FAIL(b): close() stranded behind a wedged teardown hook -- "
              "steps 7-9 never ran and _closing=True makes retry a "
              "permanent no-op (unplug leak)")
        return 1

    # Steps 7 to 9 completed despite the wedge.
    if controller in gl.page_manager.pages:
        print("FAIL(b): controller never deregistered from the page cache")
        wedge.set()
        return 1
    if controller.active_page is not None:
        print("FAIL(b): active_page not released")
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
