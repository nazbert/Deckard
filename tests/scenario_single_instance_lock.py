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

from gi.repository import Gio, GLib

from src.backend import single_instance


def private_bus() -> Gio.DBusConnection:
    """A fresh connection to the session bus, distinct from every other one.

    Gio.bus_get_sync() hands out a process-wide SHARED connection, which the
    daemon would see as a single owner -- useless for racing claims against
    each other. new_for_address_sync() opens its own, and exit-on-close is
    cleared so the deliberate close_sync() below can never take the test
    process down with it.
    """
    address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
    conn = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )
    conn.set_exit_on_close(False)
    return conn


def close_bus(conn: Gio.DBusConnection) -> None:
    try:
        conn.close_sync(None)
    except Exception:
        pass


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_single_instance_lock")
    try:
        bus_a = private_bus()
        bus_b = private_bus()
    except GLib.Error as e:
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
        threading.Timer(0.5, close_bus, args=(bus_a,)).start()
        start = time.monotonic()
        assert single_instance.claim(app_id, bus=bus_b, wait_seconds=5.0) is True, (
            "claim did not succeed after the owner released the lock"
        )
        assert time.monotonic() - start < 5.0, "claim waited past the release"

        # 5. Genuine concurrency: N fresh connections race the same lock;
        # the daemon must admit exactly one (this is the login autostart +
        # session-restore shape that motivated the fix).
        race_id = f"io.github.nazbert.DeckardLockRace{os.getpid()}"
        results = []
        results_lock = threading.Lock()

        def racer():
            b = private_bus()
            won = single_instance.claim(race_id, bus=b)
            with results_lock:
                results.append((won, b))

        threads = [threading.Thread(target=racer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [won for won, _ in results]
        assert winners.count(True) == 1, (
            f"{winners.count(True)} of 8 concurrent claims won the lock -- "
            "the daemon-serialized exclusion is broken"
        )
        for _, b in results:
            close_bus(b)

        print("PASS: lock is atomic (8-way race: 1 winner), idempotent for the owner, and released on exit")
    finally:
        close_bus(bus_b)

    print("PASS: scenario_single_instance_lock")


if __name__ == "__main__":
    main()
