"""
Integration scenario (docs/memory-footprint-impl-plan.md P1.3):
DeckController.close()/DeckManager.remove_controller's teardown sweep.

Three checks, each a small self-contained sub-test:

  (a) close() called twice is safe (idempotent, second call is an
      immediate no-op -- the `_closing` guard).
  (b) After a remove_controller-style teardown: the controller has no
      entry left in gl.page_manager.pages, its tick + media threads have
      actually exited, both per-deck thread pools are shut down, and the
      whole controller becomes collectible (a weakref to it dies once every
      other strong reference is dropped and gc.collect() runs) -- proving
      close() actually breaks the controller's reference cycles instead of
      just flipping flags.
  (c) close() while the screensaver is showing sweeps the stash: a
      ControllerKeyState's media stashed in screen_saver.original_inputs
      (the real page's inputs, swapped out by show()) gets close_resources()
      called on it -- not just discarded -- and the stash containers end up
      empty/cleared.

(A fourth check -- submit_control() rejecting messages after the terminal
ClearAndCloseMsg, bug 12 -- lived here but was unit-tier; it moved to
scenario_submit_control_reject.py so this integration-tier scenario doesn't
mix tiers, which the #69 tier-mixing guard now refuses.)
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

    # Second call must be an immediate no-op, not raise and not redo any of
    # the (now-invalid, e.g. already-None executors) teardown work.
    t0 = time.monotonic()
    controller.close(remove_media=True)
    elapsed = time.monotonic() - t0
    # Liveness ceiling: the second close() must not redo teardown work (which
    # would incur a real join / 2s stop wait) -- it returns via the _closing
    # guard almost instantly (~ms). 1.5s stays cleanly below the 2s stop
    # timeout (so it still catches "the guard didn't fire and it re-ran a
    # bounded join") while giving a loaded CI runner 3x the original 0.5s
    # headroom (#69 flake hardening).
    assert elapsed < 1.5, f"second close() call should be an immediate no-op, took {elapsed:.2f}s"

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() called twice is safe")


def test_remove_controller_frees_everything() -> None:
    controller = fixtures.make_headless_controller(serial="close-remove-1")
    deck = fixtures.raw_deck(controller)
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

    assert controller in gl.page_manager.pages, "fixture sanity: controller should have a cached page before teardown"

    # Mirrors DeckManager.remove_controller (minus the UI-stack removal,
    # which is recursive_hasattr-guarded out anyway -- see fixtures.py).
    fixtures.teardown(controller)

    assert controller not in gl.page_manager.pages, "close() must discard the controller's cached pages (step 8)"
    assert controller not in gl.deck_manager.deck_controller

    tick_dead = fixtures.wait_until(lambda: not controller.tick_thread.is_alive(), timeout=2)
    assert tick_dead, "tick thread should have been joined by close() (step 4)"
    media_dead = fixtures.wait_until(lambda: not controller.media_player.is_alive(), timeout=2)
    assert media_dead, "media thread should have been stopped by close() (step 5)"

    assert controller.action_executor is None, "action_executor should be shut down and cleared (step 9)"
    assert controller.load_executor is None, "load_executor should be shut down and cleared (step 9)"

    # The real test: the controller's reference graph must actually be
    # collectible, not just superficially "closed". Drop every strong
    # reference this scenario itself holds, then require a plain
    # gc.collect() (matching close() step 9's own final call) to reclaim it.
    ref = weakref.ref(controller)
    del controller
    del deck
    # load_page() unconditionally does GLib.idle_add(self.update_ui_on_page_
    # change) -- in the real app the GTK main loop drains that within a
    # frame; this headless harness never runs one, so the idle source (a
    # non-Python, opaque GLib registration PyGObject boxes as a strong ref to
    # the bound method) would otherwise pin the controller forever. Draining
    # the default context once is the harness-side equivalent of "let the
    # main loop tick", not a workaround for anything close() itself does
    # wrong -- production's ever-running main loop makes this a non-issue.
    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass
    gc.collect()
    assert ref() is None, "controller should become collectible after close() + gc.collect()"

    print("PASS: remove_controller-style teardown frees the whole controller graph")


class _SpyCloseable:
    """Minimal close()-able stand-in for InputImage/InputVideo: records
    whether it was actually close()d, so the stash sweep test can tell
    "close_resources() was called on the stashed input" apart from "the
    stash container was merely dropped/cleared"."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_close_sweeps_screensaver_stash() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = fixtures.make_headless_controller(serial="close-stash-1")
    deck = fixtures.raw_deck(controller)
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

    # Plant a spy on a real (pre-screensaver) key's active state, mimicking a
    # loaded key_image/key_video -- ControllerKeyState.close_resources() just
    # needs something with a close() method to call.
    real_key = controller.inputs[Input.Key][0]
    spy = _SpyCloseable()
    real_key.get_active_state().key_image = spy

    controller.screen_saver.show()
    assert controller.screen_saver.showing is True, "fixture sanity: show() should flip showing"
    assert controller.inputs[Input.Key][0] is not real_key, "fixture sanity: show() should install fresh transient inputs"

    # show() swaps deck_controller.inputs for a fresh transient set and
    # stashes the real one -- confirm our spy-bearing key ended up in the
    # stash (identity, not just an equal-looking copy), IF it's still
    # observable: mem-plan P2.6 has show() enqueue a media-player task that
    # releases + clears this same stash shortly after show() returns, so by
    # the time this line runs the dict may already be empty again. Racing
    # the exact interleaving here would make this fixture-sanity check
    # itself flaky (see scenario_screensaver_entry.py's docstring for the
    # same reasoning) -- check it only when still populated; the real
    # assertions below hold regardless of which path did the releasing.
    stashed_keys = controller.screen_saver.original_inputs.get(Input.Key, [])
    if stashed_keys:
        assert stashed_keys[0] is real_key, "fixture sanity: original_inputs should hold the real (pre-show) key objects"

    # Let P2.6's own release (a media-player task queued by show()) actually
    # run to completion before driving close() -- this is what exercises
    # show()'s release rather than racing it; close()'s own stash sweep
    # (P1.3) must then be a safe, idempotent no-op over the same objects.
    #
    # Wait on the CONJUNCTION, not just spy.closed: the release loop closes
    # every stashed input (possibly several keys/dials/the touchscreen)
    # before its own final `stashed_inputs.clear()` -- polling spy.closed
    # alone can observe the moment right after OUR spy'd key was closed but
    # before the release has finished closing the rest and clearing the
    # dict, which would make this assertion flaky under load rather than
    # testing anything real.
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


def test_close_sweeps_populated_stash_unplug_race() -> None:
    """#71 (a): the unplug-races-screensaver case the scenario name implies.

    test_close_sweeps_screensaver_stash above deliberately WAITS for show()'s
    P2.6 release (a ReleaseStashedInputsMsg on the media-player control queue)
    to empty the stash BEFORE calling close() -- so close()'s own stash sweep
    only ever runs over an already-empty dict there, and could regress to a
    no-op without that test noticing.

    Here we deterministically hold the P2.6 release so the stash is STILL
    populated when close() runs -- exactly what happens if the deck is
    unplugged (remove_controller -> close()) in the window after show()
    stashed the real inputs but before the media thread drained the release.
    close()'s sweep (DeckController.close step 7) must then be the thing that
    closes the stashed inputs and clears the containers.

    The hold is a test seam, not a sleep: we monkeypatch the media player's
    _exec_release_stashed_inputs to a record-only no-op, so the control
    message drains (queue stays bounded) but never touches the stash. That
    leaves close() as the sole releaser, which is the property under test.
    """
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = fixtures.make_headless_controller(serial="close-stash-race-1")
    try:
        deck = fixtures.raw_deck(controller)
        fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)

        real_key = controller.inputs[Input.Key][0]

        # Neuter P2.6's release BEFORE show() enqueues it: the message still
        # drains off the control queue (so nothing piles up), but the stash is
        # left fully populated for close() to sweep. Bound method rebind on the
        # instance only -- no other controller/media player is affected.
        release_seen = threading.Event()

        def _record_only_release(msg):
            # Deliberately does NOT close_resources() or clear the stash: that
            # is exactly close()'s job in this race, and what we assert below.
            release_seen.set()

        controller.media_player._exec_release_stashed_inputs = _record_only_release

        controller.screen_saver.show()
        assert controller.screen_saver.showing is True, "fixture sanity: show() should flip showing"

        # The neutered release must have actually run (proving show() did route
        # the P2.6 message and the media thread drained it) -- otherwise a
        # future refactor that stops enqueuing it would make this test pass
        # vacuously for the wrong reason.
        assert release_seen.wait(timeout=5), "show() must enqueue the P2.6 release control message"

        # Precondition for the whole point of this leg: the stash is STILL
        # populated (our record-only release left it untouched). If this ever
        # came up empty the leg would be vacuous.
        stashed = controller.screen_saver.original_inputs
        assert stashed.get(Input.Key), (
            "the stash must still be populated at close() time -- the whole "
            "point of this leg is close() sweeping a non-empty stash"
        )
        assert stashed[Input.Key][0] is real_key, "the stash must hold the real pre-show key object"

        # Plant the spy on the stashed key's active state NOW, immediately
        # before close(), so its closed-state is controlled by us and can't be
        # flipped by an earlier media-thread paint of the transient screensaver
        # inputs (which races show()'s input swap under load). This is the
        # object close()'s stash sweep must call close_resources() on.
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
        # Robust teardown: close() may already have run, but on any early
        # assertion failure the controller (with a live media thread) must
        # still be torn down or the process would hang to the run_all timeout.
        fixtures.teardown(controller)
        if controller in gl.deck_manager.deck_controller:
            gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() sweeps a still-populated screensaver stash (unplug race)")


def main() -> None:
    # A deadlock/hang in any close-path leg must fail loud and fast rather
    # than parking until run_all.py's per-scenario subprocess timeout (a live
    # media thread left un-torn-down by a mid-leg failure would otherwise
    # keep the process alive).
    fixtures.start_watchdog(60, label="scenario_deck_close")
    test_double_close_is_safe()
    test_remove_controller_frees_everything()
    test_close_sweeps_screensaver_stash()
    test_close_sweeps_populated_stash_unplug_race()
    # test_submit_control_rejected_after_stop moved to
    # scenario_submit_control_reject.py: it is unit-tier and this scenario is
    # integration-tier -- mixing the two in one process is now refused by the
    # tier-mixing guard (#69).
    print("PASS: scenario_deck_close")


if __name__ == "__main__":
    main()
