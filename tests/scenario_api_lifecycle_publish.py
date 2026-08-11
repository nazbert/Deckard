"""
Pins the D-Bus API's deck lifecycle: a deck is on the bus for exactly as long
as it is registered with the deck manager, whichever thread registered or
removed it.

Publishing used to be a one-shot sweep inside start_dbus_service. Everything
that arrives afterwards -- USB hotplug, the boot re-enumeration that exists
precisely because autostart races device init, a runtime-added fake or remote
deck -- was invisible to the API forever, and nothing ever unpublished, so an
unplugged deck kept an object on the bus bound to a controller being torn down
while the live-derived Controllers property already said it was gone.

Everything here is asserted the way an external client sees it: a SECOND,
private connection to an isolated dbus-daemon of this scenario's own, calling
methods and reading properties over the wire. The service dispatches on this
process's GLib main context, so the calls are async and the context is pumped
-- a blocking call from this thread would deadlock against the very loop that
has to answer it.

Legs:
  1. The boot sweep still publishes decks registered before the service
     started (load_hardware_deck's hook is a no-op with no bus, by design).
  2. A deck arriving through connect_new_decks() ON A WORKER THREAD appears on
     the bus. This is the headline: red before lifecycle publishing existed,
     where the deck registers, works, and is simply never published. The
     remote-deck registration path is checked here too, since it is the third
     site that registers a controller.
  3. ActivePageName is seeded at publish time, so a deck reports the page it
     is actually showing instead of an empty string until something switches.
  4. Removal unpublishes: the old path stops answering, Controllers no longer
     lists the deck, and the second connection sees PropertiesChanged.
  5. Replug reuses the object path with a FRESH instance bound to the fresh
     controller, and that object works.
  6. Stopping the service takes every object off the bus, and publish/
     unpublish calls afterwards are silent no-ops that dereference nothing --
     the same first-line guard that lets the boot path call them before any
     bus exists.
  7. A replug whose publish is queued BEFORE the removed deck's unpublish
     still ends up on the bus. Nothing orders those two enqueues, and losing
     that race used to leave a plugged-in, working deck with no object for
     the rest of the session.
  8. Which way the Controllers property is allowed to be wrong. It is read
     from the published set, so it can lag the decks the app has -- and can
     never name one a client cannot address, which is the failure that costs
     the client an error rather than a retry.

Every publish and unpublish is recorded with the thread that made it, and
asserted to be the main context: the marshalling is the whole point of the
design and is otherwise invisible to a passing test.

Between every leg, the property and the object set are asked -- both from the
outside, over the wire -- whether they still name the same decks. That
agreement is a property of every observed state, not of one moment, so it is
checked as one rather than restated leg by leg.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import ctypes  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import globals as gl  # noqa: E402

import src.api as api  # noqa: E402
import src.backend.DeckManagement.DeckManager as dm_module  # noqa: E402

from faulty_fake_deck import FaultyFakeDeck  # noqa: E402

WATCHDOG_SECONDS = 80

PROPS_IFACE = "org.freedesktop.DBus.Properties"

SERIAL_BOOT = "api-boot-1"
SERIAL_HOT = "api-hotplug-1"
SERIAL_REMOTE = "remote-deck-api-1"
SERIAL_LATE = "api-late-1"

# Hardcoded rather than run through _serial_to_dbus_path, so the sanitization
# a client depends on to compose a path from a serial is pinned here too.
PATH_BOOT = f"{api.CONTROLLER_BASE_PATH}/api_boot_1"
PATH_HOT = f"{api.CONTROLLER_BASE_PATH}/api_hotplug_1"
PATH_REMOTE = f"{api.CONTROLLER_BASE_PATH}/remote_deck_api_1"
PATH_LATE = f"{api.CONTROLLER_BASE_PATH}/api_late_1"

PAGE = "Main"


# ===================================================================== #
# The real DeckManager, with its environment-touching collaborators
# stubbed at module level before construction (as in the boot-enumeration
# scenario -- same kit, so both drive the same real lifecycle code).
# ===================================================================== #

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
    class-level script currently says."""

    results: list = []
    _lock = threading.Lock()

    def enumerate(self):
        with ScriptedDeviceManager._lock:
            return list(ScriptedDeviceManager.results)


# ===================================================================== #
# An isolated session bus
# ===================================================================== #

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


PR_SET_PDEATHSIG = 1
# Resolved BEFORE the fork on purpose: the child runs _die_with_parent between
# fork and exec, where loading a library would be a real hazard.
_LIBC = ctypes.CDLL("libc.so.6", use_errno=True)


def _die_with_parent() -> None:
    """Ask the kernel to SIGKILL this child when its parent dies.

    The scenario kills the daemon in a finally, but not every exit runs one:
    the harness watchdog ends the process with os._exit, and a hard failure
    can too. Without this the daemon outlives the run, one orphan per crash.
    """
    _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


def start_private_bus() -> tuple[subprocess.Popen, str]:
    """Run a dbus-daemon of this scenario's own and point the process at it.

    Gio.TestDBus does the same job, but its teardown additionally waits for
    the shared session connection to be finalized, and GDBus holds a reference
    on that connection for every method call it has dispatched -- so the wait
    spends its full 30-second timeout on a scenario that, like this one, calls
    methods on the API it publishes. Owning the daemon keeps the isolation and
    drops the wait.
    """
    assert shutil.which("dbus-daemon") is not None, (
        "dbus-daemon is not on PATH, so this scenario cannot start an isolated "
        "session bus and would prove nothing about the DBus API. Install it "
        "(the 'dbus' package) and re-run -- it is a runtime dependency of the "
        "app itself, not just of this test."
    )
    config_path = os.path.join(gl.DATA_PATH, "harness-session-bus.conf")
    with open(config_path, "w") as f:
        f.write(BUS_CONFIG)
    proc = subprocess.Popen(
        ["dbus-daemon", "--nofork", "--print-address", "--config-file", config_path],
        stdout=subprocess.PIPE, text=True, preexec_fn=_die_with_parent,
    )
    address = (proc.stdout.readline() or "").strip()
    assert address, "dbus-daemon started without printing a bus address"
    # Read before any bus connection is made anywhere in this process.
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
    return proc, address


def stop_private_bus(proc: subprocess.Popen) -> None:
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    # GDBus ends the process when its bus connection closes unless told
    # otherwise, and the daemon is about to go away.
    connection.set_exit_on_close(False)
    proc.terminate()
    proc.wait(timeout=10)


# ===================================================================== #
# Main-context pumping + the observing connection
# ===================================================================== #

def pump(seconds: float = 0.0) -> None:
    """Run the default main context for `seconds`. This is where the queued
    publish/unpublish work runs -- the lifecycle marshals to exactly this
    context, and nothing in this scenario runs a GLib main loop."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while True:
        while context.iteration(False):
            pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.005)


def pump_until(condition, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        pump(0.01)
        if condition():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s: {what}")


class Observer:
    """A private bus connection standing in for an external client.

    Separate from the connection the API publishes on, so nothing here can
    pass by talking to the service in-process."""

    def __init__(self, bus_address: str, destination: str):
        self.connection = Gio.DBusConnection.new_for_address_sync(
            bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        self.destination = destination
        self.property_changes: list[tuple[str, str, dict]] = []
        self.connection.signal_subscribe(
            None, PROPS_IFACE, "PropertiesChanged", None, None,
            Gio.DBusSignalFlags.NONE, self._on_properties_changed,
        )

    def _on_properties_changed(self, _connection, _sender, object_path,
                               _interface, _signal, parameters, *_user_data):
        interface, changed, _invalidated = parameters.unpack()
        self.property_changes.append((object_path, interface, changed))

    def controller_changes(self) -> list[list[str]]:
        """Every Controllers value the top-level object has announced."""
        return [
            changed["Controllers"]
            for path, interface, changed in self.property_changes
            if path == api.DBUS_OBJECT_PATH and interface == api.TOP_IFACE
            and "Controllers" in changed
        ]

    def call(self, object_path: str, interface: str, method: str,
             params=None, timeout: float = 5.0):
        """Call a method and pump until the reply lands.

        Asynchronous deliberately: the service answers on this process's main
        context, so a synchronous call from this thread would wait for a reply
        that only this thread can produce."""
        box: dict = {}

        def on_done(source, result, *_user_data):
            try:
                box["value"] = source.call_finish(result)
            except GLib.Error as e:
                box["error"] = e

        self.connection.call(
            self.destination, object_path, interface, method, params, None,
            Gio.DBusCallFlags.NONE, int(timeout * 1000), None, on_done, None,
        )
        pump_until(lambda: bool(box), timeout + 5,
                   f"no reply to {interface}.{method} at {object_path}")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def get_property(self, object_path: str, interface: str, name: str):
        reply = self.call(object_path, PROPS_IFACE, "Get",
                          GLib.Variant("(ss)", (interface, name)))
        return reply.unpack()[0]

    def controllers(self) -> list[str]:
        return self.get_property(api.DBUS_OBJECT_PATH, api.TOP_IFACE,
                                 "Controllers")

    def published_paths(self) -> list[str]:
        """Every controller object that is actually there, asked of the bus.

        Introspecting the node the controller objects hang under is how an
        outsider enumerates them: GDBus answers it out of its own registration
        table, so this is the object set as the bus holds it rather than
        anything this process says about it.
        """
        xml = self.call(api.CONTROLLER_BASE_PATH,
                        "org.freedesktop.DBus.Introspectable",
                        "Introspect", None).unpack()[0]
        return sorted(f"{api.CONTROLLER_BASE_PATH}/{name}"
                      for name in re.findall(r'<node\s+name="([^"]+)"', xml))

    def active_page(self, object_path: str) -> str:
        return self.get_property(object_path, api.CTRL_IFACE, "ActivePageName")


def assert_agreement(observer: Observer, where: str) -> None:
    """The property and the addressable object set name the same decks.

    Both read from outside, over the wire, because that is the only place the
    disagreement ever mattered: a client reads Controllers, composes a path per
    serial, and calls it. A serial the property gives that the bus does not
    have is that client's next call failing on a deck it was just told about.

    The composition uses the app's own sanitizer rather than a second copy of
    the rule -- the legs below pin the sanitized paths literally, which is
    where that rule is actually held.
    """
    listed = sorted(api._serial_to_dbus_path(serial)
                    for serial in observer.controllers())
    published = sorted(os.path.basename(path)
                       for path in observer.published_paths())
    assert listed == published, (
        f"{where}: Controllers names {listed} while the bus carries objects "
        f"for {published} -- a client composing a path from this property "
        f"reaches something that is not there"
    )


def wait_for_object(observer: Observer, object_path: str,
                    timeout: float = 15.0) -> str:
    """Poll the bus until the controller object answers, pumping throughout.
    Returns its ActivePageName."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while True:
        try:
            return observer.active_page(object_path)
        except GLib.Error as e:
            last = e
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{object_path} never appeared on the bus within {timeout}s "
                f"(last reply: {last})"
            )
        pump(0.05)


def expect_gone(observer: Observer, object_path: str) -> str:
    """Assert the object no longer answers, and return the remote error."""
    try:
        observer.active_page(object_path)
    except GLib.Error as e:
        remote = Gio.DBusError.get_remote_error(e) or ""
        assert "Unknown" in remote or "unknown" in str(e), (
            f"{object_path} failed with {remote or e}, which is not the "
            f"'this object is not here' answer a client must get"
        )
        return remote or str(e)
    raise AssertionError(
        f"{object_path} still answers calls -- the object was never taken off "
        f"the bus, so a client keeps driving a controller that is gone"
    )


# ===================================================================== #
# Which thread mutates the bus
# ===================================================================== #
#
# dasbus keeps its object registrations in a plain dict and GDBus dispatches
# them on the GLib main context, so every registration mutation has to happen
# there. Nothing else in the suite notices if the marshalling is dropped --
# replacing the idle_add with a direct call leaves the whole suite green -- so
# the calls are recorded with the thread that made them and checked below.

MAIN_IDENT = threading.main_thread().ident
BUS_CALLS: list[tuple[str, str, int]] = []


def record_bus_threads() -> None:
    """Wrap the live bus so every publish/unpublish records its thread."""
    bus = api._bus
    real_publish = bus.publish_object
    real_unpublish = bus.unpublish_object

    def publish_object(object_path, obj, *args, **kwargs):
        BUS_CALLS.append(("publish", object_path, threading.get_ident()))
        return real_publish(object_path, obj, *args, **kwargs)

    def unpublish_object(object_path):
        BUS_CALLS.append(("unpublish", object_path, threading.get_ident()))
        return real_unpublish(object_path)

    bus.publish_object = publish_object
    bus.unpublish_object = unpublish_object


def assert_on_main(op: str, object_path: str) -> None:
    calls = [c for c in BUS_CALLS if c[0] == op and c[1] == object_path]
    assert calls, f"nothing ever ran {op} for {object_path}"
    for _op, _path, ident in calls:
        assert ident == MAIN_IDENT, (
            f"{op} of {object_path} ran on thread {ident}, not the main "
            f"context ({MAIN_IDENT}) -- dasbus's registration bookkeeping is "
            f"unlocked and GDBus dispatches on that context, so this work is "
            f"racing the dispatch it is registering with"
        )


# ===================================================================== #
# Deck registration helpers
# ===================================================================== #

class Exploding:
    """Fails any attribute access. Passed to publish/unpublish with no
    service running to prove the bus guard is the FIRST statement: the boot
    path calls these with controllers that are still being built."""

    def __getattr__(self, name):
        raise AssertionError(
            f"publish/unpublish dereferenced {name!r} before checking whether "
            f"a bus exists"
        )


def make_deck_manager() -> "dm_module.DeckManager":
    manager = dm_module.DeckManager()
    manager.BOOT_RESCAN_DELAYS = (0.1, 0.15, 0.2)
    return manager


def controller_for(manager, serial: str):
    for controller in manager.deck_controller:
        if controller.serial_number() == serial:
            return controller
    return None


def connect_deck_on_worker(manager, serial: str) -> None:
    """Register a deck the way a hotplug does: connect_new_decks() from a
    thread that is not the main one."""
    deck = FaultyFakeDeck(serial_number=serial, deck_type="Fake Deck")
    ScriptedDeviceManager.results = [deck]
    thread = threading.Thread(target=manager.connect_new_decks,
                              name=f"hotplug-{serial}")
    thread.start()
    pump_until(lambda: controller_for(manager, serial) is not None, 20.0,
               f"{serial} was never registered with the deck manager")
    thread.join(timeout=20)
    assert not thread.is_alive(), f"connect_new_decks for {serial} did not return"
    ScriptedDeviceManager.results = []


# ===================================================================== #
# Legs
# ===================================================================== #

def leg_boot_sweep(manager, bus_address: str) -> Observer:
    """1: decks registered before the service exists are published by the
    sweep in start_dbus_service, not lost between the two."""
    deck = FaultyFakeDeck(serial_number=SERIAL_BOOT, deck_type="Fake Deck")
    manager.load_hardware_deck(deck)
    assert controller_for(manager, SERIAL_BOOT) is not None, \
        "the pre-service deck never registered -- nothing below is meaningful"
    assert api.get_controller_instance(SERIAL_BOOT) is None, \
        "a controller was published with no service running"

    # Nothing to dereference, and nothing queued: the guard returns first.
    api.publish_controller(Exploding())
    api.unpublish_controller(Exploding())
    pump(0.05)

    api.start_dbus_service()
    assert api._bus is not None, "the DBus service did not start"
    record_bus_threads()

    observer = Observer(bus_address, api._bus.connection.get_unique_name())
    assert wait_for_object(observer, PATH_BOOT) == PAGE, \
        "the pre-service deck is on the bus but not showing its loaded page"
    assert observer.controllers() == [SERIAL_BOOT], \
        f"Controllers disagrees with the published objects: {observer.controllers()}"
    print("  PASS: the boot sweep publishes decks registered before the service")
    return observer


def leg_worker_thread_arrival(manager, observer: Observer) -> None:
    """2 (the headline): a deck that arrives after the service is up, on a
    worker thread, must appear on the bus. Before lifecycle publishing this
    deck registered, worked, and stayed invisible to every D-Bus client for
    the rest of the session -- the autostart case exactly."""
    observer.property_changes.clear()
    connect_deck_on_worker(manager, SERIAL_HOT)

    assert wait_for_object(observer, PATH_HOT) == PAGE, (
        "a deck registered by a worker thread never reached the bus -- "
        "publishing is still the one-shot boot sweep"
    )
    pump_until(lambda: SERIAL_HOT in observer.controllers(), 10.0,
               "Controllers never listed the newly arrived deck")
    pump_until(
        lambda: any(SERIAL_HOT in value
                    for value in observer.controller_changes()),
        10.0,
        "no PropertiesChanged for Controllers reached the second connection "
        "when a deck arrived",
    )
    # Registered on the hotplug thread, published on the main context.
    assert_on_main("publish", PATH_HOT)
    print("  PASS: a deck arriving on a worker thread is published, on main")

    # The third registration site: remote decks. Same call, driven with a
    # stubbed remote manager so the append path runs for real.
    remote_deck = FaultyFakeDeck(serial_number=SERIAL_REMOTE, deck_type="Fake Deck")
    remote_controller = manager._init_deck_controller_with_retry(remote_deck)
    assert remote_controller is not None, "the remote controller failed to build"
    manager.remote_deck_manager = types.SimpleNamespace(
        start=lambda: None,
        deck_controllers=[remote_controller],
        stop=lambda: None,
    )
    manager.load_remote_decks()
    assert wait_for_object(observer, PATH_REMOTE) == PAGE, \
        "a remote deck registered at runtime never reached the bus"
    print("  PASS: a remote deck registered at runtime is published")


def leg_active_page_seeded(manager, observer: Observer) -> None:
    """3: the page a deck is showing is read from the controller at publish
    time. The boot page loads before the object exists, so waiting for the
    first switch left ActivePageName empty on a deck that plainly had a page."""
    for serial, path in ((SERIAL_BOOT, PATH_BOOT), (SERIAL_HOT, PATH_HOT)):
        controller = controller_for(manager, serial)
        assert controller.active_page is not None, \
            f"{serial} has no active page -- this leg would prove nothing"
        expected = controller.active_page.get_name()
        seen = observer.active_page(path)
        assert seen == expected, (
            f"{serial} reports ActivePageName {seen!r} over DBus while it is "
            f"showing {expected!r}"
        )
        assert seen != "", f"{serial} published with an empty ActivePageName"
    print("  PASS: ActivePageName is seeded from the controller at publish")


def leg_removal_unpublishes(manager, observer: Observer):
    """4: removal takes the object off the bus, so the object set and the
    Controllers property can no longer disagree. Returns the removed
    controller, which the next legs use as the stale one."""
    observer.property_changes.clear()
    controller = controller_for(manager, SERIAL_HOT)

    # Removal reaches DeckManager from the USB monitor thread, the Flatpak
    # poll thread and media error paths -- never only from main.
    remover = threading.Thread(target=manager.remove_controller,
                               args=(controller,), name="unplug-sim")
    remover.start()
    remover.join(timeout=20)
    assert not remover.is_alive(), "remove_controller did not return"

    pump_until(lambda: api.get_controller_instance(SERIAL_HOT) is None, 10.0,
               "the removed deck's API object was never dropped")
    remote_error = expect_gone(observer, PATH_HOT)

    # Several threads race to remove the same controller, so a second removal
    # -- and the unpublish behind it -- must be a no-op rather than an error.
    manager.remove_controller(controller)
    api.unpublish_controller(controller)
    pump(0.2)

    controllers = observer.controllers()
    assert SERIAL_HOT not in controllers, \
        f"Controllers still lists the removed deck: {controllers}"
    assert SERIAL_BOOT in controllers, \
        f"removing one deck unpublished another: {controllers}"
    assert wait_for_object(observer, PATH_BOOT) == PAGE, \
        "the surviving deck's object stopped answering when another was removed"

    announced = observer.controller_changes()
    assert announced, ("no PropertiesChanged for Controllers reached the "
                       "second connection when a deck was removed")
    assert all(SERIAL_HOT not in value for value in announced), (
        f"the removal was announced with the removed deck still listed: "
        f"{announced}"
    )
    # Removed on the unplug thread, unpublished on the main context.
    assert_on_main("unpublish", PATH_HOT)
    print(f"  PASS: removal unpublishes the deck, on main ({remote_error})")
    return controller


def leg_replug(manager, observer: Observer, stale) -> None:
    """5: the object path is a pure function of the serial, so a replugged
    deck comes back at the same path -- with a fresh instance bound to the
    fresh controller, not the closed one."""
    connect_deck_on_worker(manager, SERIAL_HOT)
    fresh = controller_for(manager, SERIAL_HOT)

    assert wait_for_object(observer, PATH_HOT) == PAGE, \
        "a replugged deck never came back on its object path"
    instance = api.get_controller_instance(SERIAL_HOT)
    assert instance is not None, "the replugged deck has no API instance"
    assert instance._controller is fresh, (
        "the replugged deck's object is still bound to the closed controller"
    )
    assert instance._object_path == PATH_HOT, (
        f"the replugged deck moved to {instance._object_path}, so clients that "
        f"cache paths by serial break"
    )

    # And it works: a method call reaches the fresh controller.
    observer.call(PATH_HOT, api.CTRL_IFACE, "SetActivePage",
                  GLib.Variant("(s)", (PAGE,)))

    # A late unpublish for the deck that WAS at this serial must not take the
    # replugged deck's object down with it -- which is why the removal path
    # looks its entry up by controller identity rather than by serial.
    api.unpublish_controller(stale)
    pump(0.2)
    assert wait_for_object(observer, PATH_HOT, timeout=5) == PAGE, (
        "a late unpublish for the PREVIOUS controller at this serial took the "
        "replugged deck's object off the bus"
    )
    print("  PASS: a replugged deck returns at the same path, freshly bound")


def leg_inverted_replug_order(manager, observer: Observer) -> None:
    """7: a replug whose publish is queued BEFORE the old controller's
    unpublish must still leave the fresh deck on the bus.

    Removal queues its unpublish after releasing the deck manager's lock, and
    the registration sites take no lock at all, so nothing orders the two
    enqueues against each other -- FIFO dispatch only preserves whatever order
    they arrived in. Removals from the Flatpak poll thread and additions from
    the USB monitor or the boot rescan are exactly that shape. Driven here by
    running the two workers in the losing order, which is what that enqueue
    order produces at dispatch: publish first (it finds the serial still
    claimed by the dead controller), then the stale unpublish."""
    stale = controller_for(manager, SERIAL_HOT)
    assert api.get_controller_instance(SERIAL_HOT) is not None, \
        "nothing is published at this serial -- the race below cannot happen"

    manager.remove_controller(stale)  # queues an unpublish; deliberately unpumped

    deck = FaultyFakeDeck(serial_number=SERIAL_HOT, deck_type="Fake Deck")
    fresh = manager._init_deck_controller_with_retry(deck)
    assert fresh is not None, "the replacement controller failed to build"
    manager.deck_controller.append(fresh)

    api._publish_on_main(fresh)
    api._unpublish_on_main(stale)
    pump(0.2)  # and the queued unpublish lands too

    assert wait_for_object(observer, PATH_HOT, timeout=5) == PAGE, (
        "the replugged deck is registered and working but has no object on "
        "the bus: its publish found the serial still claimed by the removed "
        "controller and gave up, and the unpublish behind it then dropped "
        "that claim -- nothing publishes the deck again for the rest of the "
        "session"
    )
    instance = api.get_controller_instance(SERIAL_HOT)
    assert instance is not None and instance._controller is fresh, \
        "the surviving object is not the fresh controller's"
    assert SERIAL_HOT in observer.controllers(), \
        "Controllers lost the replugged deck"
    print("  PASS: a publish queued before the old deck's unpublish survives it")


def serials_registered(manager) -> list[str]:
    """What the deck manager has, which is what the property used to report."""
    return [controller.serial_number() for controller in manager.deck_controller]


def leg_publish_lag_direction(manager, observer: Observer) -> None:
    """8: which way the property is allowed to be wrong.

    Publishing is marshalled onto the main context, so a deck is registered
    with the deck manager for a moment before its object exists. The property
    disagrees with SOMETHING during that moment whichever source it is read
    from, and the direction is the whole of it:

      * read from the deck manager, it names a deck that has no object, and
        the client that composes a path from that name gets UnknownObject for
        a deck that is plainly plugged in;
      * read from the published set, it does not yet name a deck the app
        already has, and the client that retries -- or simply listens to the
        PropertiesChanged that publishing emits -- finds it.

    Nothing orders an external read against a queued publish, and this does not
    pretend otherwise: GLib runs idles below GDBus's own dispatch, so a read
    that arrives after a publish was queued can still be answered first. That
    is precisely the window driven here, deliberately, by leaving the context
    un-iterated.

    Which is also why the absence half is asserted in-process: reading over the
    bus means pumping this context, and pumping it dispatches the very publish
    the window is made of.
    """
    deck = FaultyFakeDeck(serial_number=SERIAL_LATE, deck_type="Fake Deck")
    controller = manager._init_deck_controller_with_retry(deck)
    assert controller is not None, "the late controller failed to build"
    manager.deck_controller.append(controller)

    observer.property_changes.clear()
    api.publish_controller(controller)  # queued, and deliberately unpumped

    top = api.get_api_instance()
    assert SERIAL_LATE in serials_registered(manager), \
        "the deck never registered, so there is no disagreement to have"
    assert api.get_controller_instance(SERIAL_LATE) is None, \
        "the publish ran anyway -- this leg needs the queued, undispatched one"
    assert SERIAL_LATE not in top.Controllers, (
        f"the property named a deck with no object on the bus: read from the "
        f"deck manager it announces decks a client cannot address, which is "
        f"the one direction this must never fail in ({top.Controllers})"
    )

    pump_until(lambda: api.get_controller_instance(SERIAL_LATE) is not None,
               10.0, "the queued publish never ran")
    assert SERIAL_LATE in top.Controllers, \
        "the deck is published and the property still does not name it"
    assert wait_for_object(observer, PATH_LATE) == PAGE, \
        "the late deck's object never answered"
    assert_agreement(observer, "after a queued publish dispatched")

    healed = [value for value in observer.controller_changes()
              if SERIAL_LATE in value]
    assert healed, (
        "nothing announced the new deck, so a client that read the property "
        "in the window above has no way to learn it was wrong except by "
        "polling -- the signal is what closes a window that cannot be ordered "
        "away"
    )

    # The removal side of the same window, which fails the other way round: the
    # deck leaves the manager first and its object goes when the queued work
    # runs. The property must stay with the OBJECT here too -- a client told
    # the deck is gone while its object still answers is being lied to just as
    # surely, only in the other direction.
    observer.property_changes.clear()
    manager.remove_controller(controller)  # queues the unpublish
    assert SERIAL_LATE not in serials_registered(manager), \
        "the removal did not happen, so there is no window to be in"
    assert SERIAL_LATE in top.Controllers, (
        f"the property dropped a deck whose object is still on the bus: read "
        f"from the deck manager it stops naming a deck a client can still "
        f"drive ({top.Controllers})"
    )

    pump_until(lambda: api.get_controller_instance(SERIAL_LATE) is None, 10.0,
               "the queued unpublish never ran")
    expect_gone(observer, PATH_LATE)
    assert_agreement(observer, "after a queued unpublish dispatched")
    print("  PASS: the property lags the decks, never the objects")


def leg_stop_service(manager, observer: Observer) -> None:
    """6: stopping takes everything off the bus, and the lifecycle calls that
    keep arriving afterwards are silent no-ops."""
    # Queued while the service was up, dispatched after it stopped: quit runs
    # on this same context, so the two serialize here exactly as they do in
    # the app, and the worker has to find the bus gone and return quietly.
    survivor = controller_for(manager, SERIAL_BOOT)
    api.unpublish_controller(survivor)
    api.stop_dbus_service()
    assert api._bus is None, "stop_dbus_service left the bus in place"
    pump(0.2)

    expect_gone(observer, PATH_BOOT)
    expect_gone(observer, PATH_HOT)
    assert api.get_controller_instance(SERIAL_BOOT) is None, \
        "a published instance survived the service stopping"

    # Late arrivals and removals: no bus, so nothing to dereference and
    # nothing to publish.
    api.publish_controller(survivor)
    api.unpublish_controller(survivor)
    api.publish_controller(Exploding())
    api.unpublish_controller(Exploding())
    pump(0.2)
    assert api.get_controller_instance(SERIAL_BOOT) is None, (
        "publish_controller published against a stopped service"
    )

    # Whole-run sweep: not one registration mutation ran off the main context.
    off_main = [c for c in BUS_CALLS if c[2] != MAIN_IDENT]
    assert not off_main, (
        f"registrations were mutated off the main context: {off_main}"
    )
    print("  PASS: stopping unpublishes everything; later calls are no-ops")


def run_legs(bus_address: str) -> None:
    gl.deck_manager = manager = make_deck_manager()
    observer = None
    try:
        observer = leg_boot_sweep(manager, bus_address)
        assert_agreement(observer, "after the boot sweep")
        leg_worker_thread_arrival(manager, observer)
        assert_agreement(observer, "after a deck arrived on a worker thread")
        leg_active_page_seeded(manager, observer)
        stale = leg_removal_unpublishes(manager, observer)
        assert_agreement(observer, "after a deck was removed")
        leg_replug(manager, observer, stale)
        assert_agreement(observer, "after a replug")
        leg_inverted_replug_order(manager, observer)
        assert_agreement(observer, "after a publish and unpublish crossed")
        leg_publish_lag_direction(manager, observer)
        leg_stop_service(manager, observer)
    finally:
        api.stop_dbus_service()
        for controller in list(manager.deck_controller):
            fixtures.teardown(controller)
        if observer is not None:
            observer.connection.close_sync(None)


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, "scenario_api_lifecycle_publish")
    fixtures._install_integration_globals()
    fixtures.seed_page(PAGE)

    dm_module.USBMonitor = StubUSBMonitor
    dm_module.Xdp = types.SimpleNamespace(
        Portal=types.SimpleNamespace(new=lambda: StubPortal()))
    dm_module.DeviceManager = ScriptedDeviceManager

    bus_proc, bus_address = start_private_bus()
    try:
        run_legs(bus_address)
    finally:
        stop_private_bus(bus_proc)

    print("PASS: scenario_api_lifecycle_publish")


if __name__ == "__main__":
    main()
