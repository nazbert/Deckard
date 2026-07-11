"""
Boot-enumeration rescan scenario (issue #106): if no deck is USB-enumerable
when the app starts (autostart racing device init at boot), DeckManager must
re-enumerate with bounded backoff in the background and pick the deck up
when it appears -- exactly once, even when the USB hotplug monitor races the
rescan for the same deck -- and a rescan parked in backoff must stop
promptly on shutdown.

Review round 1 additions:
  * flaky-open phase -- a deck that ENUMERATES but flakes its open
    (TransportError -1, the documented boot-storm failure) must not end the
    rescan behind a success log: the stop condition is "registered", not
    "enumerable", and the pickup path retries opens like the startup path.
  * barrier phase -- two callers inside connect_new_decks' window for the
    SAME deck (synchronized with threading.Barrier so the windows genuinely
    overlap) must register it exactly once; this fails if
    _connect_decks_lock is ever weakened (verified red: with the lock
    replaced by a no-op context manager, duplicates register).

DeckManager is constructed for real, with its environment-touching
collaborators (USBMonitor, Xdp portal, StreamDeck DeviceManager) stubbed at
module level BEFORE construction. Enumeration is scripted.
"""
import threading
import time
import types

import fixtures  # must be first: isolates DATA_PATH before `import globals`
import globals as gl

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

import src.backend.DeckManagement.DeckManager as dm_module


class StubUSBMonitor:
    """usbmonitor.USBMonitor stand-in: no udev, no threads."""

    def __init__(self, *args, **kwargs):
        self.on_connect = None
        self.on_disconnect = None

    def start_monitoring(self, on_connect=None, on_disconnect=None):
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

    def stop_monitoring(self, timeout=None):
        pass


class StubPortal:
    def running_under_flatpak(self):
        return False


class ScriptedDeviceManager:
    """StreamDeck.DeviceManager stand-in: enumerate() returns whatever the
    class-level script currently says (fresh instances share it, mirroring
    how the real code constructs a new DeviceManager per enumeration)."""

    results: list = []
    enumerate_calls: int = 0
    _lock = threading.Lock()

    def enumerate(self):
        with ScriptedDeviceManager._lock:
            ScriptedDeviceManager.enumerate_calls += 1
            return list(ScriptedDeviceManager.results)


class FlakyOpenDeck(FaultyFakeDeck):
    """First `fail_opens` open() calls raise TransportError (the boot-storm
    flake); is_open() reflects the real open state -- FakeDeck's stub always
    answers True, which would let the init path skip the open entirely."""

    def __init__(self, *args, fail_opens: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._remaining_open_failures = fail_opens
        self._really_open = False
        self.open_attempts = 0

    def is_open(self):
        return self._really_open

    def open(self, *args, **kwargs):
        self.open_attempts += 1
        if self._remaining_open_failures > 0:
            self._remaining_open_failures -= 1
            raise TransportError("boot-storm open flake (-1)")
        self._really_open = True
        return super().open(*args, **kwargs)

    def close(self):
        self._really_open = False
        return super().close()


def make_deck_manager() -> "dm_module.DeckManager":
    manager = dm_module.DeckManager()
    # Shrink the backoff so the scenario runs in seconds, not minutes.
    manager.BOOT_RESCAN_DELAYS = (0.1, 0.15, 0.2, 0.3, 0.5)
    return manager


def phase_pickup_exactly_once() -> None:
    gl.deck_manager = manager = make_deck_manager()

    ScriptedDeviceManager.results = []
    ScriptedDeviceManager.enumerate_calls = 0

    manager.load_hardware_decks()  # empty enumeration -> arms the rescan
    assert manager._boot_rescan_thread is not None, "rescan not armed on empty enumeration"
    assert manager._boot_rescan_thread.is_alive(), "rescan thread not running"
    assert len(manager.deck_controller) == 0

    # Let at least one *empty* retry complete (startup call was #1).
    ok = fixtures.wait_until(lambda: ScriptedDeviceManager.enumerate_calls >= 2, timeout=3)
    assert ok, "rescan never re-enumerated"
    assert len(manager.deck_controller) == 0, "controller appeared from an empty enumeration"

    # Deck becomes enumerable now. Race a simulated hotplug event (the USB
    # monitor's on_connect path calls connect_new_decks directly) against
    # the rescan's next round. (The tight same-window race is exercised
    # deterministically in phase_concurrent_callers_exactly_once.)
    deck = FaultyFakeDeck(serial_number="boot-rescan-1", deck_type="Fake Deck")
    ScriptedDeviceManager.results = [deck]

    hotplug = threading.Thread(target=manager.connect_new_decks, name="hotplug-sim")
    hotplug.start()

    assert fixtures.wait_until(lambda: len(manager.deck_controller) >= 1, timeout=10), \
        "deck was never registered"
    hotplug.join(timeout=10)
    assert not hotplug.is_alive(), "simulated hotplug call did not return"

    # The rescan must observe the registered deck and stop.
    assert fixtures.wait_until(lambda: not manager._boot_rescan_thread.is_alive(), timeout=10), \
        "rescan thread did not stop after the deck appeared"

    # Give any (buggy) late duplicate registration a moment to land before
    # asserting exactly-once.
    time.sleep(0.3)
    assert len(manager.deck_controller) == 1, \
        f"expected exactly 1 controller, got {len(manager.deck_controller)} (double-register)"
    assert manager.deck_controller[0].serial_number() == "boot-rescan-1"

    # Re-arming after a deck is present must not re-add it either.
    n_before = ScriptedDeviceManager.enumerate_calls
    manager.start_boot_rescan()
    assert fixtures.wait_until(lambda: not manager._boot_rescan_thread.is_alive(), timeout=10)
    assert ScriptedDeviceManager.enumerate_calls > n_before, "re-armed rescan never enumerated"
    assert len(manager.deck_controller) == 1, "re-armed rescan duplicated the deck"

    for controller in list(manager.deck_controller):
        fixtures.teardown(controller)


def phase_flaky_open_still_registered() -> None:
    """Review round 1 finding 1 (the exact reported failure): deck absent at
    boot -> rescan armed -> deck enumerates but its open flakes with
    TransportError. The rescan must NOT stop on mere enumerability: the
    pickup path retries the open, a fully-failed round leaves the deck
    unregistered so a later round retries it, and the rescan only stops
    once a controller actually exists."""
    gl.deck_manager = manager = make_deck_manager()
    # Enough rounds after the deck appears for a fully-failed pickup round
    # (3 in-round open retries) plus the retry round that succeeds.
    manager.BOOT_RESCAN_DELAYS = (0.1, 0.1, 0.3, 0.3, 0.5, 0.5)

    ScriptedDeviceManager.results = []
    ScriptedDeviceManager.enumerate_calls = 0

    manager.load_hardware_decks()
    assert manager._boot_rescan_thread.is_alive()

    # 3 open failures = the first pickup attempt exhausts its in-round
    # retries (attempts=3) and gives up; only a LATER rescan round can
    # register the deck -- exercising both the in-round retry and the
    # "registered, not enumerable" stop condition.
    deck = FlakyOpenDeck(serial_number="flaky-open-1", deck_type="Fake Deck", fail_opens=3)
    ScriptedDeviceManager.results = [deck]

    assert fixtures.wait_until(lambda: len(manager.deck_controller) == 1, timeout=20), (
        "deck enumerated with a flaky open was never registered -- rescan "
        "stopped on enumerability instead of registration"
    )
    assert manager.deck_controller[0].serial_number() == "flaky-open-1"
    assert deck.open_attempts >= 4, f"expected >=4 open attempts, got {deck.open_attempts}"

    assert fixtures.wait_until(lambda: not manager._boot_rescan_thread.is_alive(), timeout=10), \
        "rescan did not stop after late registration"
    time.sleep(0.3)
    assert len(manager.deck_controller) == 1, "flaky deck registered more than once"

    for controller in list(manager.deck_controller):
        fixtures.teardown(controller)


def phase_concurrent_callers_exactly_once() -> None:
    """Review round 1 finding 2: the raced-thread phase above can settle
    before the two windows ever overlap, so it cannot catch a weakened
    _connect_decks_lock. Here two callers are barrier-synchronized INTO
    connect_new_decks for the same deck across several trials -- with the
    lock elided (no-op context manager) this reliably registers duplicates;
    with the real lock it must always be exactly one."""
    gl.deck_manager = manager = make_deck_manager()

    TRIALS = 8
    for trial in range(TRIALS):
        deck = FaultyFakeDeck(serial_number=f"barrier-{trial}", deck_type="Fake Deck")
        ScriptedDeviceManager.results = [deck]
        before = len(manager.deck_controller)

        barrier = threading.Barrier(2)

        def caller():
            barrier.wait()
            manager.connect_new_decks()

        threads = [
            threading.Thread(target=caller, name=f"barrier-caller-{trial}-{i}")
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert all(not t.is_alive() for t in threads), f"trial {trial}: caller did not return"

        added = len(manager.deck_controller) - before
        assert added == 1, (
            f"trial {trial}: {added} controllers registered for one deck -- "
            f"connect_new_decks is not exactly-once under concurrent callers"
        )

    ScriptedDeviceManager.results = []
    for controller in list(manager.deck_controller):
        fixtures.teardown(controller)


def phase_clean_shutdown_during_backoff() -> None:
    manager = make_deck_manager()
    manager.BOOT_RESCAN_DELAYS = (30.0, 30.0)  # park it deep in backoff

    ScriptedDeviceManager.results = []
    manager.load_hardware_decks()
    assert manager._boot_rescan_thread is not None and manager._boot_rescan_thread.is_alive()

    start = time.monotonic()
    manager.stop_boot_rescan()
    elapsed = time.monotonic() - start
    assert not manager._boot_rescan_thread.is_alive(), \
        "rescan thread still alive after stop_boot_rescan()"
    assert elapsed < 2.5, f"stop_boot_rescan took {elapsed:.2f}s -- backoff sleep not interruptible"

    # Idempotent / safe with no rescan running.
    manager.stop_boot_rescan()


def main() -> None:
    fixtures.start_watchdog(120, "scenario_boot_enumeration")
    fixtures._install_integration_globals()
    fixtures.seed_page("Main")

    # Stub the environment-touching collaborators before any DeckManager is
    # constructed.
    dm_module.USBMonitor = StubUSBMonitor
    dm_module.Xdp = types.SimpleNamespace(Portal=types.SimpleNamespace(new=lambda: StubPortal()))
    dm_module.DeviceManager = ScriptedDeviceManager

    phase_pickup_exactly_once()
    phase_flaky_open_still_registered()
    phase_concurrent_callers_exactly_once()
    phase_clean_shutdown_during_backoff()

    print("PASS: scenario_boot_enumeration")


if __name__ == "__main__":
    main()
