"""Integration scenario for the teardown sweep of DeckController.close().

close() is idempotent, the controller becomes collectible after removal, and
a close during a screensaver sweeps and clears the stashed inputs.
"""
import gc
import threading
import time
import weakref

import fixtures
import globals as gl
from gi.repository import GLib


def test_double_close_is_safe() -> None:
    controller = fixtures.make_headless_controller(serial="close-double-1")
    fixtures.wait_until(lambda: controller.active_page is not None, timeout=3)

    controller.close(remove_media=True)
    assert controller._closing is True, "close() must set _closing"

    # The second call must be an immediate no-op. It must not raise and must
    # not redo teardown work over already-None executors.
    t0 = time.monotonic()
    controller.close(remove_media=True)
    elapsed = time.monotonic() - t0
    # Liveness ceiling. The second close() must not redo teardown work, which
    # would incur a real join and a 2 s stop wait. It returns through the
    # _closing guard in milliseconds. 1.5 s stays under the 2 s stop timeout and
    # still gives a loaded CI runner headroom.
    assert elapsed < 1.5, f"second close() call should be an immediate no-op, took {elapsed:.2f}s"

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() called twice is safe")


def test_remove_controller_frees_everything() -> None:
    controller = fixtures.make_headless_controller(serial="close-remove-1")
    deck = fixtures.raw_deck(controller)
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

    assert controller in gl.page_manager.pages, "fixture sanity: controller should have a cached page before teardown"

    # Mirrors DeckManager.remove_controller, without the UI-stack removal, which
    # the null UIPort no-ops away here.
    fixtures.teardown(controller)

    assert controller not in gl.page_manager.pages, "close() must discard the controller's cached pages (step 8)"
    assert controller not in gl.deck_manager.deck_controller

    tick_dead = fixtures.wait_until(lambda: not controller.tick_thread.is_alive(), timeout=2)
    assert tick_dead, "tick thread should have been joined by close() (step 4)"
    media_dead = fixtures.wait_until(lambda: not controller.media_player.is_alive(), timeout=2)
    assert media_dead, "media thread should have been stopped by close() (step 5)"

    assert controller.action_executor is None, "action_executor should be shut down and cleared (step 9)"
    assert controller.load_executor is None, "load_executor should be shut down and cleared (step 9)"

    # The reference graph of the controller must be collectible, not merely
    # closed. Drop every strong reference this scenario holds, then require a
    # plain gc.collect(), which matches the final call of close() step 9.
    ref = weakref.ref(controller)
    del controller
    del deck
    # load_page() always calls GLib.idle_add(self.update_ui_on_page_change). This
    # harness runs no main loop, so the idle source, which PyGObject boxes as a
    # strong ref to the bound method, would pin the controller forever. Drain the
    # default context once, the harness equivalent of one main-loop tick.
    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass
    gc.collect()
    assert ref() is None, "controller should become collectible after close() + gc.collect()"

    print("PASS: remove_controller-style teardown frees the whole controller graph")


class _SpyCloseable:
    """Minimal close()-able stand-in for InputImage and InputVideo.

    It records whether close() ran, so the stash sweep test can tell a real
    close_resources() apart from a dropped stash container.
    """

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_close_sweeps_screensaver_stash() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = fixtures.make_headless_controller(serial="close-stash-1")
    deck = fixtures.raw_deck(controller)
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

    # Plant a spy on the active state of a real pre-screensaver key, which mimics
    # a loaded key_image. ControllerKeyState.close_resources() needs only
    # something with a close() method.
    real_key = controller.inputs[Input.Key][0]
    spy = _SpyCloseable()
    real_key.get_active_state().key_image = spy

    controller.screen_saver.show()
    assert controller.screen_saver.showing is True, "fixture sanity: show() should flip showing"
    assert controller.inputs[Input.Key][0] is not real_key, "fixture sanity: show() should install fresh transient inputs"

    # show() swaps deck_controller.inputs for a fresh transient set and stashes
    # the real one. Confirm by identity that the spy-bearing key reached the
    # stash, but only while the stash is still populated. show() enqueues a
    # media-player task that releases and clears it soon after show() returns.
    stashed_keys = controller.screen_saver.original_inputs.get(Input.Key, [])
    if stashed_keys:
        assert stashed_keys[0] is real_key, "fixture sanity: original_inputs should hold the real (pre-show) key objects"

    # Let the release queued by show() finish before close(), which exercises
    # that release instead of racing it. Wait on the conjunction rather than on
    # spy.closed alone, because the release loop closes every stashed input
    # before its own final clear(), and polling one spy would be flaky.
    released = fixtures.wait_until(
        lambda: spy.closed and controller.screen_saver.original_inputs == {},
        timeout=5,
    )
    assert released, "show() must release the stashed input's resources (mem-plan P2.6)"
    assert real_key.get_active_state().key_image is None, "show()'s release must clear the closed reference"
    assert controller.screen_saver.original_inputs == {}, "show()'s release must clear the stashed input set"

    controller.close(remove_media=True)

    assert spy.closed is True, "close() must call close_resources() on stashed inputs, not just drop the container"
    assert real_key.get_active_state().key_image is None, "close_resources() must clear the closed reference"
    assert controller.screen_saver.original_inputs == {}, "close() must clear the stashed input set"
    assert controller.screen_saver.original_background is None, "close() must release the stashed background"

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() sweeps the screensaver stash while showing")


def test_close_sweeps_stash_unplug_race() -> None:
    """An unplug that races the screensaver leaves the stash populated.

    A record-only _exec_release_stashed_inputs holds the release, so the control
    message drains and the stash stays full. close() step 7 must then be the
    thing that closes the stashed inputs and clears the containers.
    """
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = fixtures.make_headless_controller(serial="close-stash-race-1")
    try:
        deck = fixtures.raw_deck(controller)
        fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

        real_key = controller.inputs[Input.Key][0]

        # Neuter the release before show() enqueues it. The message still drains
        # off the control queue, so nothing piles up, and the stash stays
        # populated for close() to sweep. The rebind is on this instance only.
        release_seen = threading.Event()

        def _record_only_release(msg):
            # This does not close_resources() and does not clear the stash.
            # That is the job of close() in this race, asserted below.
            release_seen.set()

        controller.media_player._exec_release_stashed_inputs = _record_only_release

        controller.screen_saver.show()
        assert controller.screen_saver.showing is True, "fixture sanity: show() should flip showing"

        # The neutered release must have run, which proves show() routed the
        # message and the media thread drained it. Without that check, a
        # refactor that stops enqueuing it would make this test pass for the
        # wrong reason.
        assert release_seen.wait(timeout=5), "show() must enqueue the P2.6 release control message"

        # Precondition for the leg. The stash is still populated, because the
        # record-only release left it untouched. An empty stash makes the leg
        # vacuous.
        stashed = controller.screen_saver.original_inputs
        assert stashed.get(Input.Key), (
            "the stash must still be populated at close() time -- the whole "
            "point of this leg is close() sweeping a non-empty stash"
        )
        assert stashed[Input.Key][0] is real_key, "the stash must hold the real pre-show key object"

        # Plant the spy on the active state of the stashed key now, immediately
        # before close(), so an earlier media-thread paint of the transient
        # screensaver inputs cannot flip its closed state. This is the object
        # the close() stash sweep must call close_resources() on.
        spy = _SpyCloseable()
        real_key.get_active_state().key_image = spy
        assert spy.closed is False, "fixture sanity: the freshly-planted spy starts unclosed"

        controller.close(remove_media=True)

        assert spy.closed is True, (
            "close() must close_resources() the stashed inputs when the P2.6 "
            "release never emptied the stash (unplug-races-screensaver)"
        )
        assert real_key.get_active_state().key_image is None, "close()'s sweep must clear the closed reference"
        assert controller.screen_saver.original_inputs == {}, "close() must clear the populated stash"
    finally:
        # Robust teardown. close() may already have run, but on an early
        # assertion failure the controller, with a live media thread, must still
        # be torn down or the process hangs until the run_all timeout.
        fixtures.teardown(controller)
        if controller in gl.deck_manager.deck_controller:
            gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() sweeps a still-populated screensaver stash (unplug race)")


def main() -> None:
    # A hang in any close-path leg must fail loud and fast rather than parking
    # until the per-scenario subprocess timeout of run_all.py. A live media
    # thread left un-torn-down by a mid-leg failure keeps the process alive.
    fixtures.start_watchdog(60, label="scenario_deck_close")
    test_double_close_is_safe()
    test_remove_controller_frees_everything()
    test_close_sweeps_screensaver_stash()
    test_close_sweeps_stash_unplug_race()
    # test_submit_control_rejected_after_stop lives in
    # scenario_submit_control_reject.py. It is unit-tier and this scenario is
    # integration-tier, and the tier-mixing guard refuses both in one process.
    print("PASS: scenario_deck_close")


if __name__ == "__main__":
    main()
