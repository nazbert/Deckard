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


class _StubBus:
    """Counts object registrations so a double-register leak (a registration
    that is never unregistered) is observable without a real D-Bus daemon."""

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

    def emit_signal(self, **kwargs):
        # DBusMenuService.set_items() -> LayoutUpdate() emits a signal on
        # construction; the double-register accounting doesn't care.
        pass

    @property
    def live(self) -> set:
        return set(self.registered) - set(self.unregistered)


class _StubInterfaceInfo:
    def cache_build(self):
        pass

    def cache_release(self):
        pass


def check_base_double_register_no_orphan() -> None:
    """#125 (base class): DBusService.register() with no intervening
    unregister() must not orphan the previous object registration on the
    connection -- exactly one live registration after any number of
    register() calls, and none after unregister()."""
    from src.backend.trayicon import DBusService

    bus = _StubBus()
    service = DBusService(_StubInterfaceInfo(), "/StubPath", bus)
    service.register()
    service.register()  # double-register with no stop()

    assert len(bus.live) == 1, (
        f"double register() leaked object registrations: registered "
        f"{bus.registered}, unregistered {bus.unregistered} -- "
        f"{len(bus.live)} left live (expected 1)"
    )
    service.unregister()
    assert not bus.live, f"unregister() left registrations live: {bus.live}"
    print("PASS: base DBusService double register() keeps exactly one live "
          "registration")


def check_sni_double_register_keeps_menu_live() -> None:
    """#125 (real TrayIcon path -- regression guard): the actual
    double-register path is TrayIcon.initialize() + the Settings-panel
    start(), which goes through StatusNotifierItemService.register(), NOT
    the bare DBusService. That override registers BOTH the SNI object and
    a nested menu object, and (from !21) overrides unregister() to cascade
    self._menu.unregister().

    An unregister-then-reregister remedy in the base register() dispatches
    virtually to the SNI unregister() override, tears the menu object down,
    and never re-registers it -- leaving the tray menu dead on the bus while
    the base only re-registers the SNI object. So after a second register()
    BOTH sni.registration_id AND sni._menu.registration_id must stay live,
    with no orphaned/leaked prior registration.

    Driven against a stub bus so the register/unregister accounting is exact
    and deterministic. StatusNotifierItemService.register() also watches
    org.kde.StatusNotifierWatcher via Gio.bus_watch_name_on_connection (which
    type-checks its first arg against a real connection); that name-watch is
    orthogonal to the object-registration leak under test, so it is stubbed
    out here.

    Red-test: against the unregister-then-reregister remedy this FAILS
    (menu.registration_id is None, and the SNI + menu ids churn); with the
    early-return guard it passes."""
    import src.backend.trayicon as trayicon_mod
    from src.backend.trayicon import StatusNotifierItemService

    orig_watch = trayicon_mod.Gio.bus_watch_name_on_connection
    orig_unwatch = trayicon_mod.Gio.bus_unwatch_name
    trayicon_mod.Gio.bus_watch_name_on_connection = (
        lambda *a, **k: 12345  # a plausible watch id; never a real watch
    )
    trayicon_mod.Gio.bus_unwatch_name = lambda *a, **k: None
    try:
        bus = _StubBus()
        sni = StatusNotifierItemService(session_bus=bus, menu_items=[])

        sni.register()                       # TrayIcon.initialize()
        sni_id = sni.registration_id
        menu_id = sni._menu.registration_id
        assert sni_id is not None, "SNI object failed to register"
        assert menu_id is not None, "menu object failed to register"

        sni.register()                       # Settings-panel start(), no stop()

        assert sni.registration_id is not None, (
            "SNI object registration lost after double register()"
        )
        assert sni._menu.registration_id is not None, (
            "double register() left the tray MENU object unregistered "
            f"(menu.registration_id={sni._menu.registration_id!r}); the base "
            "register() must not tear the menu down via the SNI unregister() "
            "override -- that cascades self._menu.unregister() and the base "
            "only re-registers the SNI object, leaving the menu dead. "
            f"registered={bus.registered} unregistered={bus.unregistered}"
        )
        # Exactly two live registrations: the SNI item + its menu, no orphans.
        assert len(bus.live) == 2, (
            f"double register() must keep exactly the SNI + menu objects live "
            f"(no leak, no teardown): registered={bus.registered}, "
            f"unregistered={bus.unregistered}, live={bus.live} (expected 2)"
        )
        # No-op double-register: each object keeps its original registration
        # id -- nothing was unregistered-then-reregistered (which would both
        # churn the id and, on this path, kill the menu).
        assert sni.registration_id == sni_id, (
            f"SNI object id churned on double register(): {sni_id} -> "
            f"{sni.registration_id}; register() must be a no-op when already "
            "registered, not unregister-then-reregister"
        )
        assert sni._menu.registration_id == menu_id, (
            f"menu object id churned on double register(): {menu_id} -> "
            f"{sni._menu.registration_id}"
        )

        # A legitimate stop()/start() cycle must still re-register cleanly.
        sni.unregister()
        assert not bus.live, f"unregister() left registrations live: {bus.live}"
        sni.register()
        assert sni.registration_id is not None and sni._menu.registration_id is not None, (
            "stop()/start() cycle failed to re-register SNI + menu"
        )
        assert len(bus.live) == 2, (
            f"stop()/start() cycle leaked registrations: live={bus.live} "
            f"(expected 2)"
        )
        sni.unregister()
    finally:
        trayicon_mod.Gio.bus_watch_name_on_connection = orig_watch
        trayicon_mod.Gio.bus_unwatch_name = orig_unwatch
    print("PASS: StatusNotifierItemService double register() keeps both the "
          "SNI and menu objects live (no leak, no menu teardown)")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_tray_reregister")
    check_base_double_register_no_orphan()
    check_sni_double_register_keeps_menu_live()

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
