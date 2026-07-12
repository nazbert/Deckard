"""
Regression scenario for #47: tray icon registration must not be one-shot.

`StatusNotifierItemService.register()` used to make a single synchronous
`RegisterStatusNotifierItem` call against `org.kde.StatusNotifierWatcher`:

  * if the watcher appeared late (GNOME's AppIndicator support loading
    after StreamController), the call raised and the icon never appeared;
  * if the watcher restarted (plasmashell/waybar crash), the new watcher
    instance knows nothing about previously registered items -- per the
    SNI spec items must re-register -- so the icon was permanently lost
    until app restart.

This scenario spins up an isolated session bus (Gio.TestDBus /
dbus-daemon), registers the tray icon while NO watcher exists, then:

  1. starts a fake StatusNotifierWatcher -> the item must announce itself;
  2. kills that watcher's connection (simulated shell crash) and starts a
     fresh one -> the item must announce itself again.
"""
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

from src.backend.trayicon import DBusTrayIcon, DBusMenu


WATCHER_NAME = "org.kde.StatusNotifierWatcher"

WATCHER_NODE_INFO = Gio.DBusNodeInfo.new_for_xml("""
<?xml version="1.0" encoding="UTF-8"?>
<node>
    <interface name="org.kde.StatusNotifierWatcher">
        <method name="RegisterStatusNotifierItem">
            <arg type="s" direction="in"/>
        </method>
    </interface>
</node>""")


class FakeWatcher:
    """A minimal StatusNotifierWatcher on its own bus connection, so
    closing the connection mimics the hosting shell crashing."""

    def __init__(self, bus_address: str):
        self.registrations: list[str] = []
        self._name_acquired = False
        self.connection = Gio.DBusConnection.new_for_address_sync(
            bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        self.connection.register_object(
            object_path="/StatusNotifierWatcher",
            interface_info=WATCHER_NODE_INFO.interfaces[0],
            method_call_closure=self._on_method_call,
        )
        self._own_id = Gio.bus_own_name_on_connection(
            self.connection,
            WATCHER_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_name_acquired,
            None,
        )

    def _on_name_acquired(self, connection, name):
        self._name_acquired = True

    def _on_method_call(self, _connection, _sender, _path, _interface_name,
                        method_name, parameters, invocation):
        if method_name == "RegisterStatusNotifierItem":
            self.registrations.append(parameters.unpack()[0])
        invocation.return_value(None)

    def wait_until_owning_name(self, timeout: float = 10.0) -> None:
        pump_until(lambda: self._name_acquired, timeout,
                   "fake watcher never acquired the well-known name")

    def crash(self) -> None:
        """Drop the well-known name the hard way: close the connection,
        like a crashing shell would."""
        self.connection.close_sync(None)


def pump_until(condition, timeout: float, what: str) -> None:
    """Iterate the default GLib main context until `condition()` or timeout."""
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout}s: {what}")


def check_double_register_no_orphan() -> None:
    """#125: DBusService.register() with no intervening unregister() must not
    orphan the previous object registration on the connection. Counted
    against a stub bus -- registered minus unregistered must be exactly 1
    live registration after any number of register() calls."""
    from src.backend.trayicon import DBusService

    class StubBus:
        def __init__(self):
            self.registered: list[int] = []
            self.unregistered: list[int] = []
            self._next_id = 1

        def register_object(self, object_path, interface_info,
                            method_call_closure, get_property_closure):
            reg_id = self._next_id
            self._next_id += 1
            self.registered.append(reg_id)
            return reg_id

        def unregister_object(self, reg_id):
            self.unregistered.append(reg_id)

    class StubInterfaceInfo:
        def cache_build(self):
            pass

        def cache_release(self):
            pass

    bus = StubBus()
    service = DBusService(StubInterfaceInfo(), "/StubPath", bus)
    service.register()
    service.register()  # TrayIcon.initialize() + Settings-panel start()

    live = set(bus.registered) - set(bus.unregistered)
    assert len(live) == 1, (
        f"double register() leaked object registrations: registered "
        f"{bus.registered}, unregistered {bus.unregistered} -- "
        f"{len(live)} left live (expected 1)"
    )
    service.unregister()
    live = set(bus.registered) - set(bus.unregistered)
    assert not live, f"unregister() left registrations live: {live}"
    print("PASS: double register() keeps exactly one live registration")


def main() -> None:
    check_double_register_no_orphan()

    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()  # also exports DBUS_SESSION_BUS_ADDRESS for bus_get_sync
    try:
        run_checks(test_bus.get_bus_address())
    finally:
        test_bus.down()
    print("PASS: scenario_tray_reregister")


def run_checks(bus_address: str) -> None:
    menu = DBusMenu()
    menu.add_menu_item(1, "Quit", callback=lambda: None)
    tray = DBusTrayIcon(menu=menu, app_id="com.example.HarnessTray",
                        title="HarnessTray")

    # 1) Late watcher: registering while no watcher exists must neither
    #    raise nor lose the icon -- the announcement must arrive as soon
    #    as a watcher shows up.
    try:
        tray.register()
    except Exception as e:
        raise AssertionError(
            f"register() must not fail when the StatusNotifierWatcher "
            f"is not (yet) on the bus: {e!r}"
        )

    watcher = FakeWatcher(bus_address)
    watcher.wait_until_owning_name()
    pump_until(lambda: len(watcher.registrations) >= 1, 10.0,
               "item was never announced to a late-appearing watcher")
    item_path = tray.sni_service.dbus_path
    assert watcher.registrations == [item_path], (
        f"expected the item's object path {item_path!r} to be announced, "
        f"got {watcher.registrations}"
    )

    # 2) Watcher restart: a fresh watcher instance knows nothing about
    #    previously registered items; the item must re-announce itself.
    watcher.crash()
    reborn = FakeWatcher(bus_address)
    reborn.wait_until_owning_name()
    pump_until(lambda: len(reborn.registrations) >= 1, 10.0,
               "item was never re-announced after the watcher restarted")
    assert reborn.registrations == [item_path], (
        f"expected re-announcement of {item_path!r} to the restarted "
        f"watcher, got {reborn.registrations}"
    )

    # The tray can still be unregistered cleanly afterwards (Settings
    # toggle / app shutdown path).
    tray.unregister()


if __name__ == "__main__":
    main()
