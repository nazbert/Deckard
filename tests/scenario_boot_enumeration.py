"""
Boot-enumeration rescan scenario (issue #106): if no deck is USB-enumerable
when the app starts (autostart racing device init at boot), DeckManager must
re-enumerate with bounded backoff in the background and pick the deck up
when it appears -- exactly once, even when the USB hotplug monitor races the
rescan for the same deck -- and a rescan parked in backoff must stop
promptly on shutdown.

DeckManager is constructed for real, with its environment-touching
collaborators (USBMonitor, Xdp portal, StreamDeck DeviceManager) stubbed at
module level BEFORE construction. Enumeration is scripted: empty at startup,
empty for at least one retry, then one FaultyFakeDeck.
"""
import threading
import time
import types

import fixtures  # must be first: isolates DATA_PATH before `import globals`
import globals as gl

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
    # the rescan's next round: the connect lock + already-loaded check must
    # yield exactly one registration.
    deck = FaultyFakeDeck(serial_number="boot-rescan-1", deck_type="Fake Deck")
    ScriptedDeviceManager.results = [deck]

    hotplug = threading.Thread(target=manager.connect_new_decks, name="hotplug-sim")
    hotplug.start()

    assert fixtures.wait_until(lambda: len(manager.deck_controller) >= 1, timeout=10), \
        "deck was never registered"
    hotplug.join(timeout=10)
    assert not hotplug.is_alive(), "simulated hotplug call did not return"

    # The rescan must observe the non-empty enumeration and stop.
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
    fixtures.start_watchdog(60, "scenario_boot_enumeration")
    fixtures._install_integration_globals()
    fixtures.seed_page("Main")

    # Stub the environment-touching collaborators before any DeckManager is
    # constructed.
    dm_module.USBMonitor = StubUSBMonitor
    dm_module.Xdp = types.SimpleNamespace(Portal=types.SimpleNamespace(new=lambda: StubPortal()))
    dm_module.DeviceManager = ScriptedDeviceManager

    phase_pickup_exactly_once()
    phase_clean_shutdown_during_backoff()

    print("PASS: scenario_boot_enumeration")


if __name__ == "__main__":
    main()
