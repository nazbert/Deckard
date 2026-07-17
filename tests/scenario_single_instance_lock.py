"""
Regression test (issue #155): the single-instance launch lock must be ATOMIC.

Field incident 2026-07-16: login autostart + KDE session restore launched two
instances in the same second; both passed quit_running()'s check-then-continue
probe 3 ms apart, and both proceeded to USB-reset and fight over the deck.
src/backend/single_instance.py replaces that gap with RequestName(DO_NOT_QUEUE),
which the D-Bus daemon serializes -- exactly one connection can win.

This scenario simulates the two racing launches with two PRIVATE session-bus
connections in one process (the daemon treats them as distinct owners, same as
two processes). Uses a test-scoped lock name so a concurrently running real
app is never touched. Prints a SKIP line and exits 0 when no session bus is
available (headless CI).
"""
import os
import threading
import time

import fixtures  # noqa: F401  -- sys.path setup for src imports

try:
    import dbus
except ImportError:
    print("SKIP: dbus-python not available")
    print("PASS: scenario_single_instance_lock")
    raise SystemExit(0)

from src.backend import single_instance


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_single_instance_lock")
    try:
        bus_a = dbus.SessionBus(private=True)
        bus_b = dbus.SessionBus(private=True)
    except dbus.exceptions.DBusException as e:
        print(f"SKIP: no session bus available ({e})")
        print("PASS: scenario_single_instance_lock")
        return

    app_id = f"io.github.nazbert.DeckardLockTest{os.getpid()}"
    try:
        # 1. First launch wins.
        assert single_instance.claim(app_id, bus=bus_a) is True, "first claim must win"

        # 2. Re-claim on the same connection is idempotent (ALREADY_OWNER).
        assert single_instance.claim(app_id, bus=bus_a) is True, "re-claim by the owner must succeed"

        # 3. Concurrent second launch loses immediately -- the pre-fix code
        # would have 'passed the probe' here and gone on to reset the decks.
        assert single_instance.claim(app_id, bus=bus_b) is False, (
            "second connection claimed the lock while the first still holds it "
            "-- the launch race is back"
        )

        # 4. --close-running path: with wait_seconds, a claim succeeds once
        # the owner releases (connection close drops the name).
        threading.Timer(0.5, bus_a.close).start()
        start = time.monotonic()
        assert single_instance.claim(app_id, bus=bus_b, wait_seconds=5.0) is True, (
            "claim did not succeed after the owner released the lock"
        )
        assert time.monotonic() - start < 5.0, "claim waited past the release"

        print("PASS: lock is atomic, idempotent for the owner, and released on exit")
    finally:
        try:
            bus_b.close()
        except Exception:
            pass

    print("PASS: scenario_single_instance_lock")


if __name__ == "__main__":
    main()
