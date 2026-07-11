"""
Integration scenario (docs/presenter-migration-plan.md §7 "Blocked-plugin
transition", §10 G-B1): the regression test for the v1 blocker -- a
transition (screensaver hide(), which runs load_page() in its phase 3) must
never hold _load_page_lock across a plugin callback. If it did, a slow
ChangePage handler would serialize every other load_page()/hide()/show()
caller behind it -- the exact run_on_main/pulsectl-style deadlock shape this
codebase already froze on once (see docs/presenter-migration-plan.md §10).

SignalManager.trigger_signal() dispatches non-AppQuit signals via
`GLib.idle_add(callback, ...)` -- fire-and-forget, not a synchronous call.
That means a slow handler registered the normal way can never be observed
holding up its *caller* in this headless harness (there is no real GTK main
loop pumping idle callbacks on a schedule tied to the caller's thread, so any
attempt to race a "slow ChangePage handler" against a caller via the real
async path would be a timing coin-flip, not a deterministic regression
check). So this scenario monkeypatches trigger_signal, for ChangePage only,
to call registered handlers synchronously and directly on the caller's own
thread -- reproducing the *shape* of the G-B1 hazard deterministically: a
handler that sleeps for HANDLER_SLEEP now runs on whatever thread called
load_page() (hide()'s phase 3, in this scenario), on that thread's own call
stack, exactly the way a genuinely slow/blocking plugin handler would.

Two independent things are checked:

  1. A THIRD thread that does nothing but try to acquire _load_page_lock
     (bare `.acquire()`, no load_page/hide/show call of its own -- so its
     timing can't be confounded by the handler firing again on its own call
     stack) must succeed almost immediately, even while hide()'s thread is
     still deep inside its post-lock ChangePage dispatch. This is the direct
     G-B1 probe: if hide() ever regressed to calling load_page() from
     *inside* its own _load_page_lock hold, load_page's
     initialize_actions()/trigger_signal() tail would run nested inside that
     outer hold too, and this probe would block for the full HANDLER_SLEEP
     (or hang outright against a real GTK marshal) instead of returning at
     once.
  2. A concurrent, independent load_page() call (from a fourth thread, racing
     hide()'s transition) must still complete and land its own page -- i.e.
     the transition doesn't corrupt or starve unrelated switches, only
     serializes on the (brief) locked section.
"""
import os
import threading
import time

import fixtures
import globals as gl
from src.Signals.Signals import ChangePage

WATCHDOG_SECONDS = 30
HANDLER_SLEEP = 3.0


def _install_synchronous_change_page_dispatch():
    """Returns a trigger_signal replacement that calls ChangePage handlers
    directly (synchronously, on the caller's thread) instead of via
    GLib.idle_add; all other signals keep the real async behavior."""
    signal_manager = gl.signal_manager
    real_trigger_signal = signal_manager.trigger_signal

    def synchronous_trigger_signal(signal, *args, **kwargs):
        if signal is not ChangePage:
            return real_trigger_signal(signal, *args, **kwargs)
        for callback in list(signal_manager.connected_signals.get(signal, [])):
            callback(*args, **kwargs)

    signal_manager.trigger_signal = synchronous_trigger_signal
    return real_trigger_signal


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_blocked_plugin_transition")

    controller = fixtures.make_headless_controller(serial="blocked-1")

    other_path = fixtures.seed_page("Other")
    other_page = gl.page_manager.get_page(other_path, controller)

    real_trigger_signal = _install_synchronous_change_page_dispatch()

    handler_started = threading.Event()

    def slow_change_page_handler(ctrl, old_path, new_path):
        handler_started.set()
        time.sleep(HANDLER_SLEEP)

    gl.signal_manager.connect_signal(ChangePage, slow_change_page_handler)

    try:
        # Put the screensaver into "showing" so hide() has a real transition
        # (phase 3 -> load_page -> initialize_actions/ChangePage) to run.
        controller.screen_saver.show()
        ok = fixtures.wait_until(lambda: controller.screen_saver.showing, timeout=3)
        assert ok, "fixture setup: screensaver never showed"

        hide_elapsed = {}
        concurrent_load_elapsed = {}
        lock_probe_elapsed = {}

        def do_hide():
            t0 = time.monotonic()
            controller.screen_saver.hide()
            hide_elapsed["dt"] = time.monotonic() - t0

        def do_concurrent_load():
            # Give hide() a head start into its transition before racing it.
            time.sleep(0.05)
            t0 = time.monotonic()
            controller.load_page(other_page, allow_reload=True)
            concurrent_load_elapsed["dt"] = time.monotonic() - t0

        def probe_lock():
            # Waits until the handler is definitely mid-sleep, then times a
            # bare lock acquisition -- no load_page/hide/show call of its
            # own, so nothing here can itself trigger the handler again and
            # confound the timing (unlike do_concurrent_load, which
            # legitimately re-triggers the handler on ITS OWN thread as part
            # of its own, unrelated page transition).
            ok = fixtures.wait_until(handler_started.is_set, timeout=10)
            if not ok:
                lock_probe_elapsed["error"] = "handler never started"
                return
            t0 = time.monotonic()
            got = controller._load_page_lock.acquire(timeout=10)
            lock_probe_elapsed["dt"] = time.monotonic() - t0
            lock_probe_elapsed["got"] = got
            if got:
                controller._load_page_lock.release()

        start = time.monotonic()
        t_hide = threading.Thread(target=do_hide, name="HideCaller")
        t_load = threading.Thread(target=do_concurrent_load, name="ConcurrentLoadPage")
        t_probe = threading.Thread(target=probe_lock, name="LockProbe")
        t_hide.start()
        t_load.start()
        t_probe.start()
        t_hide.join(timeout=15)
        t_load.join(timeout=15)
        t_probe.join(timeout=15)
        total = time.monotonic() - start

        assert not t_hide.is_alive(), "screen_saver.hide() did not complete -- possible deadlock"
        assert not t_load.is_alive(), "concurrent load_page() did not complete -- possible deadlock"
        assert not t_probe.is_alive(), "the lock probe never completed -- possible deadlock"
        assert "dt" in hide_elapsed, "hide() thread did not record completion"
        assert "dt" in concurrent_load_elapsed, "concurrent load_page() thread did not record completion"

        # hide()'s own thread is expected to take ~HANDLER_SLEEP: its phase 3
        # calls load_page(), whose plugin-facing tail runs the (synchronously
        # dispatched, for this test) slow ChangePage handler directly on its
        # own call stack -- same as it would on the calling thread in real
        # production if signal dispatch were ever made blocking. That is
        # fine and orthogonal to G-B1. What G-B1 is actually about is
        # checked by the lock probe below.
        assert hide_elapsed["dt"] >= HANDLER_SLEEP * 0.9, (
            "fixture sanity: hide()'s phase-3 load_page() did not appear to "
            "run the synchronously-dispatched handler at all"
        )
        assert "error" not in lock_probe_elapsed, lock_probe_elapsed.get("error")
        assert lock_probe_elapsed.get("got"), "the lock probe never acquired _load_page_lock"

        # The core G-B1 regression assertion: while hide()'s thread is deep
        # inside its post-lock ChangePage dispatch (mid-HANDLER_SLEEP,
        # guaranteed by waiting on handler_started above), a completely
        # unrelated thread must still be able to acquire _load_page_lock
        # almost immediately. If hide() ever regressed to calling load_page()
        # from inside its own lock hold, this would instead block for
        # (close to) the remainder of HANDLER_SLEEP.
        assert lock_probe_elapsed["dt"] < 1.0, (
            f"acquiring _load_page_lock took {lock_probe_elapsed['dt']:.2f}s while "
            f"a ChangePage handler was sleeping -- the transition is holding the "
            f"lock across a plugin callback (G-B1 regression)"
        )
        assert total < HANDLER_SLEEP + 5.0, f"scenario took {total:.2f}s total -- unexpectedly slow"

        # Functional correctness: the concurrent, independent load_page()
        # call must still have landed (not been dropped/corrupted by racing
        # hide()'s transition).
        assert controller.active_page is other_page, (
            "the concurrent load_page() call did not end up as the active page"
        )

    finally:
        gl.signal_manager.trigger_signal = real_trigger_signal
        # disconnect_signal is added by fix/plugin-backend-lifecycle -- an
        # independent branch this one does not stack on. Tolerate its absence
        # so the scenario runs standalone on the deck stack; without the
        # disconnect the sleeping handler stays registered, which is fine for
        # a subprocess-per-scenario harness.
        disconnect = getattr(gl.signal_manager, "disconnect_signal", None)
        if disconnect is not None:
            disconnect(ChangePage, slow_change_page_handler)

    fixtures.teardown(controller)
    print("PASS: scenario_blocked_plugin_transition")


if __name__ == "__main__":
    main()
