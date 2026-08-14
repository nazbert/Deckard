"""D-Bus mechanics of the logind detector, driven through the bus seam.

A fake connection stands in, so no real system bus is touched. The session
path resolves, Lock and Unlock flip the manager, and a GLib.Error stays inert.
"""
import os

import fixtures  # must be first; isolates DATA_PATH

from gi.repository import GLib

LOGIN1 = "org.freedesktop.login1"


class RecordingManager:
    """Stands in for LockScreenManager. The detector only calls .lock()."""

    def __init__(self):
        self.lock_calls = []

    def lock(self, active):
        self.lock_calls.append(active)


class FakeBus:
    """Duck-types the two Gio.DBusConnection methods the detector uses.

    The signatures match positionally.
    """

    def __init__(self, session_path="/org/freedesktop/login1/session/_31",
                 fail=False):
        self.session_path = session_path
        self.fail = fail
        self.calls = []
        self.subscriptions = []

    def call_sync(self, bus_name, object_path, interface_name, method_name,
                  parameters, reply_type, flags, timeout_msec, cancellable):
        self.calls.append((bus_name, object_path, interface_name,
                           method_name, parameters.unpack()))
        if self.fail:
            raise GLib.Error("fake logind is down")
        return GLib.Variant("(o)", (self.session_path,))

    def signal_subscribe(self, sender, interface_name, member, object_path,
                         arg0, flags, callback):
        self.subscriptions.append(
            (sender, interface_name, member, object_path, callback)
        )
        return len(self.subscriptions)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_logind_signals")

    from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector

    # 1. Resolution through GetSession(XDG_SESSION_ID), and the subscription.
    os.environ["XDG_SESSION_ID"] = "7"
    manager = RecordingManager()
    bus = FakeBus()
    LogindLockScreenDetector(manager, bus=bus)

    assert len(bus.calls) == 1, bus.calls
    _, resolver_path, resolver_iface, method, args = bus.calls[0]
    assert resolver_path == "/org/freedesktop/login1", resolver_path
    assert resolver_iface == f"{LOGIN1}.Manager", resolver_iface
    assert method == "GetSession", method
    assert args == ("7",), args

    assert len(bus.subscriptions) == 1, bus.subscriptions
    sender, iface, member, path, callback = bus.subscriptions[0]
    assert sender == LOGIN1, sender
    assert iface == f"{LOGIN1}.Session", iface
    assert member is None, f"member must be None (Lock AND Unlock), got {member!r}"
    assert path == bus.session_path, path
    print("PASS: GetSession(XDG_SESSION_ID) resolves the subscription path")

    # 2. Lock and Unlock flip manager.lock(). Other members are ignored.
    callback(bus, LOGIN1, path, iface, "Lock", None)
    assert manager.lock_calls == [True], manager.lock_calls
    callback(bus, LOGIN1, path, iface, "Unlock", None)
    assert manager.lock_calls == [True, False], manager.lock_calls
    callback(bus, LOGIN1, path, iface, "PauseDevice", None)
    assert manager.lock_calls == [True, False], (
        f"non-Lock/Unlock member must be ignored, got {manager.lock_calls}"
    )
    print("PASS: Lock/Unlock flip manager.lock(); other members ignored")

    # 3. With no XDG_SESSION_ID the resolver falls back to GetSessionByPID.
    os.environ.pop("XDG_SESSION_ID", None)
    bus = FakeBus()
    LogindLockScreenDetector(RecordingManager(), bus=bus)
    _, _, _, method, args = bus.calls[0]
    assert method == "GetSessionByPID", method
    assert args == (os.getpid(),), args
    print("PASS: falls back to GetSessionByPID when XDG_SESSION_ID is unset")

    # 4. A GLib.Error is contained, so the detector stays inert.
    bus = FakeBus(fail=True)
    LogindLockScreenDetector(RecordingManager(), bus=bus)  # must not raise
    assert bus.subscriptions == [], (
        f"failed resolution must leave the detector inert, "
        f"got {bus.subscriptions}"
    )
    print("PASS: GLib.Error leaves the detector inert without raising")

    print("PASS: scenario_logind_signals")


if __name__ == "__main__":
    main()
