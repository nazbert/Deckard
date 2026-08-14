"""Integration scenario for the blocked-plugin transition.

A transition must never hold _load_page_lock across a plugin callback. The
scenario patches ChangePage dispatch to run handlers on the caller's thread.
"""
import threading
import time

import fixtures
import globals as gl
from src.Signals.Signals import ChangePage

WATCHDOG_SECONDS = 30
HANDLER_SLEEP = 3.0


def _install_sync_change_page_dispatch():
    """Return a trigger_signal replacement that calls ChangePage handlers direct.

    The handlers run synchronously on the caller's thread. Every other signal
    keeps the real async behavior.
    """
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

    real_trigger_signal = _install_sync_change_page_dispatch()

    handler_started = threading.Event()

    def slow_change_page_handler(ctrl, old_path, new_path):
        handler_started.set()
        time.sleep(HANDLER_SLEEP)

    gl.signal_manager.connect_signal(ChangePage, slow_change_page_handler)

    try:
        # Put the screensaver into showing, so hide() has a real transition to
        # run through phase 3, load_page and the ChangePage tail.
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
            # Wait until the handler is mid-sleep, then time a bare lock
            # acquisition. This thread makes no load_page, hide or show call of
            # its own, so nothing here re-triggers the handler and confounds the
            # timing, unlike do_concurrent_load.
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

        # The hide() thread is expected to take about HANDLER_SLEEP. Its phase 3
        # calls load_page(), whose plugin-facing tail runs the slow ChangePage
        # handler on its own call stack under the synchronous dispatch this
        # scenario installs. The lock probe below is the real assertion.
        assert hide_elapsed["dt"] >= HANDLER_SLEEP * 0.9, (
            "fixture sanity: hide()'s phase-3 load_page() did not appear to "
            "run the synchronously-dispatched handler at all"
        )
        assert "error" not in lock_probe_elapsed, lock_probe_elapsed.get("error")
        assert lock_probe_elapsed.get("got"), "the lock probe never acquired _load_page_lock"

        # The core regression assertion. While the hide() thread sits deep inside
        # its post-lock ChangePage dispatch, an unrelated thread must still
        # acquire _load_page_lock almost at once. A load_page() call from inside
        # the lock hold would block this probe for the rest of HANDLER_SLEEP.
        assert lock_probe_elapsed["dt"] < 1.0, (
            f"acquiring _load_page_lock took {lock_probe_elapsed['dt']:.2f}s while "
            f"a ChangePage handler was sleeping -- the transition is holding the "
            f"lock across a plugin callback (G-B1 regression)"
        )
        assert total < HANDLER_SLEEP + 5.0, f"scenario took {total:.2f}s total -- unexpectedly slow"

        # The concurrent, independent load_page() call must still have landed.
        # The racing hide() transition must not drop or corrupt it.
        assert controller.active_page is other_page, (
            "the concurrent load_page() call did not end up as the active page"
        )

    finally:
        gl.signal_manager.trigger_signal = real_trigger_signal
        # Tolerate a missing disconnect_signal, so the scenario runs standalone
        # on the deck stack. Without the disconnect the sleeping handler stays
        # registered, which is harmless in a subprocess-per-scenario harness.
        disconnect = getattr(gl.signal_manager, "disconnect_signal", None)
        if disconnect is not None:
            disconnect(ChangePage, slow_change_page_handler)

    fixtures.teardown(controller)
    print("PASS: scenario_blocked_plugin_transition")


if __name__ == "__main__":
    main()
