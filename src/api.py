"""Deckard DBus API.

The interface at io.github.nazbert.Deckard lets external tools query and
control Deckard.

Top-level object /io/github/nazbert/Deckard: the Controllers, Pages, IconPacks
and ForegroundWindow properties, plus AddPage, RemovePage, ChangePage,
ChangeState, NotifyForegroundWindow and GetIconNames.

Per-controller object /io/github/nazbert/Deckard/controllers/<serial>:
SetActivePage and the ActivePageName property.
"""

import json
import os
import re
from collections import namedtuple
from typing import Any, Tuple
from src.Signals import Signals
from loguru import logger as log

from dasbus.server.interface import dbus_interface
from dasbus.connection import SessionMessageBus
from dasbus.typing import Int, Str, List
from dasbus.error import DBusError
from gi.repository import GLib

import appinfo
import globals as gl
from src.backend import control_plane

WindowInfo = namedtuple("WindowInfo", ["name", "wm_class"])

DBUS_OBJECT_PATH = appinfo.DBUS_OBJECT_PATH
CONTROLLER_BASE_PATH = DBUS_OBJECT_PATH + "/controllers"
TOP_IFACE = appinfo.APP_ID
CTRL_IFACE = f"{appinfo.APP_ID}.Controller"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
ERROR_PAGE_EXISTS = f"{appinfo.APP_ID}.Error.PageExists"


def _emit_properties_changed(object_path: str, interface: str,
                             changed: dict, invalidated: list[str] | None = None):
    """Emit org.freedesktop.DBus.Properties.PropertiesChanged on the bus."""
    if _bus is None:
        return
    try:
        connection = _bus.connection
        body = GLib.Variant("(sa{sv}as)", (
            interface,
            changed,
            invalidated or [],
        ))
        connection.emit_signal(
            None,           # destination (broadcast)
            object_path,
            PROPS_IFACE,
            "PropertiesChanged",
            body,
        )
    except Exception as e:
        log.debug(f"DBus API: Failed to emit PropertiesChanged: {e}")


def _serial_to_dbus_path(serial: str) -> str:
    """Convert a serial number to a valid DBus object path component."""
    # DBus paths only allow [A-Za-z0-9_], so replace anything else with _
    return re.sub(r"[^A-Za-z0-9_]", "_", serial)


# Per-controller API, published at .../controllers/<serial>.

@dbus_interface(CTRL_IFACE)
class ControllerInstanceAPI:
    """DBus interface for a single StreamDeck controller."""

    def __init__(self, controller):
        self._controller = controller
        self._active_page_name: str = ""
        self._object_path: str = ""  # set by _publish_controller

    # Methods

    def SetActivePage(self, name: Str) -> None:
        """Set the active page on this controller.

        The control plane holds the rules, which every transport shares, and a
        request for the active page does nothing. This method returns no value
        and raises nothing. A bad page name appears only in the log.
        """
        serial = self._controller.serial_number()
        log.info(f"DBus API [{serial}]: SetActivePage called – name={name!r}")
        try:
            result = control_plane.get().change_page_on(self._controller, name)
            if result.ok:
                self._active_page_name = name
            else:
                log.warning(f"DBus API [{serial}]: SetActivePage – {result.message}")
        except Exception as e:
            log.error(f"DBus API [{serial}]: SetActivePage error: {e}")

    # Properties

    @property
    def ActivePageName(self) -> Str:
        """The name of the currently active page on this controller."""
        return self._active_page_name

    @ActivePageName.setter
    def ActivePageName(self, value: Str):
        self._active_page_name = value
        log.debug(f"DBus API [{self._controller.serial_number()}]: ActivePageName changed to {value!r}")
        if self._object_path:
            _emit_properties_changed(
                self._object_path, CTRL_IFACE,
                {"ActivePageName": GLib.Variant("s", value)},
            )


# Top-level API, published at /io/github/nazbert/Deckard.

@dbus_interface(TOP_IFACE)
class DeckardAPI:
    """DBus interface for Deckard (top-level)."""

    def __init__(self):
        self._foreground_window: WindowInfo = WindowInfo("", "")

    # Methods

    @property
    def Pages(self) -> List[Str]:
        """Return a list of page names."""
        log.info("DBus API: Pages read")
        try:
            if gl.page_manager is not None:
                return gl.page_manager.get_page_names()
        except Exception as e:
            log.error(f"DBus API: Pages error: {e}")
        return []

    def AddPage(self, name: Str, json_contents: Str) -> None:
        """Add a new page with the given name and JSON contents."""
        log.info(f"DBus API: AddPage called – name={name!r}")
        try:
            page_dict = json.loads(json_contents) if json_contents else {}
            if gl.page_manager is not None:
                path = gl.page_manager.add_page(name, page_dict)
                gl.page_manager.refresh_document(path)
                gl.page_manager.reload_pages_with_path(path)
                gl.signal_manager.trigger_signal(Signals.PageAdd, path)
        except FileExistsError as e:
            raise DBusError(
                ERROR_PAGE_EXISTS,
                f"Page '{name}' already exists"
            )
        except json.JSONDecodeError as e:
            log.error(f"DBus API: AddPage – invalid JSON: {e}")
        except Exception as e:
            log.error(f"DBus API: AddPage error: {e}")

    def RemovePage(self, name: Str) -> None:
        """Remove the page with the given name."""
        log.info(f"DBus API: RemovePage called – name={name!r}")
        try:
            if gl.page_manager is not None:
                page_path = os.path.join(gl.page_manager.PAGE_PATH, f"{name}.json")
                if os.path.exists(page_path):
                    gl.page_manager.remove_page(page_path)
                    gl.signal_manager.trigger_signal(Signals.PageDelete, page_path)
                else:
                    log.warning(f"DBus API: RemovePage – page not found: {name}")
        except Exception as e:
            log.error(f"DBus API: RemovePage error: {e}")

    def ChangePage(self, serial: Str, page: Str) -> Str:
        """Show page, a page name or a page path, on the deck with serial.

        Returns the empty string when the deck applied the request, and the
        reason when it did not. A deck that already shows that page counts as
        applied. The CLI forwards here, so the caller reads the reason in its
        own terminal. Unexpected exceptions propagate, and dasbus turns them
        into an error reply.
        """
        log.info(f"DBus API: ChangePage called – serial={serial!r} page={page!r}")
        result = control_plane.get().change_page(serial, page)
        if result.ok:
            return ""
        log.warning(f"DBus API: ChangePage – {result.message}")
        return result.message

    def ChangeState(self, serial: Str, page: Str, coords: Str, state: Int) -> Str:
        """Set the input at coords on page of the deck with serial to state.

        This loads the page first when it is not the active one. The answer
        matches ChangePage, empty on success and the reason otherwise. coords
        travels as the "x,y" text the caller typed, and the control plane
        parses it once for every transport.
        """
        log.info(f"DBus API: ChangeState called – serial={serial!r} page={page!r} "
                 f"coords={coords!r} state={state!r}")
        result = control_plane.get().change_state(serial, page, coords, state)
        if result.ok:
            log.info(f"DBus API: ChangeState – {result.message}")
            return ""
        log.warning(f"DBus API: ChangeState – {result.message}")
        return result.message

    def NotifyForegroundWindow(self, name: Str, wm_class: Str) -> None:
        """Tell Deckard the current foreground window.

        This lets a test or a development run work without kdotool.
        """
        win = WindowInfo(name, wm_class)
        log.info(f"DBus API: NotifyForegroundWindow called – {win!r}")
        try:
            if gl.window_grabber is not None:
                from src.backend.WindowGrabber.Window import Window
                window = Window(wm_class=win.wm_class, title=win.name)
                gl.window_grabber.on_active_window_changed(window)
        except Exception as e:
            log.error(f"DBus API: NotifyForegroundWindow error: {e}")

    @property
    def IconPacks(self) -> List[Str]:
        """Return a list of icon pack IDs."""
        log.info("DBus API: IconPacks read")
        try:
            if gl.icon_pack_manager is not None:
                # Annotate locally, because IconPackManager.get_icon_packs
                # declares a return type of dir, a typo for dict, not a type.
                packs: dict[str, Any] = gl.icon_pack_manager.get_icon_packs()
                return list(packs.keys())
        except Exception as e:
            log.error(f"DBus API: IconPacks error: {e}")
        return []

    def GetIconNames(self, icon_pack_id: Str) -> List[Str]:
        """Return a list of all icon names in the given icon pack."""
        log.info(f"DBus API: GetIconNames called – icon_pack_id={icon_pack_id!r}")
        try:
            if gl.icon_pack_manager is not None:
                packs: dict[str, Any] = gl.icon_pack_manager.get_icon_packs()
                pack = packs.get(icon_pack_id)
                if pack is None:
                    log.warning(f"DBus API: GetIconNames – pack not found: {icon_pack_id}")
                    return []
                icons = pack.get_icons()
                return [icon.name for icon in icons]
        except Exception as e:
            log.error(f"DBus API: GetIconNames error: {e}")
        return []

    # Properties

    @property
    def DataPath(self) -> Str:
        """Base path for the Deckard data, such as pages and icons.

        A client needs it to compose valid JSON page files.
        """
        return gl.DATA_PATH
    
    @property
    def Controllers(self) -> List[Str]:
        """Serial numbers of the controllers a client can address.

        The value comes from the published object set, not from the deck
        manager list, so each serial here has an object at the path composed
        from it, because _publish_controller lists and publishes in one step.

        Publishing marshals onto the main context, so between a deck
        registering and its publish idle running this property omits a deck the
        app has. An omission costs a client one retry, and an extra name costs
        it an error on a path it composed from this list. GLib runs idles below
        the GDBus dispatch, so an external read can overtake a queued publish,
        and the PropertiesChanged from publishing corrects the value.

        Main context only, like every registration change.
        """
        return list(_controller_instances)

    @property
    def ForegroundWindow(self) -> Tuple[Str, Str]:
        """The current foreground window as (name, wm_class)."""
        return (self._foreground_window.name, self._foreground_window.wm_class)

    @ForegroundWindow.setter
    def ForegroundWindow(self, value: Tuple[Str, Str]):
        self._foreground_window = WindowInfo(*value)
        log.debug(f"DBus API: ForegroundWindow changed to {self._foreground_window!r}")
        _emit_properties_changed(
            DBUS_OBJECT_PATH, TOP_IFACE,
            {"ForegroundWindow": GLib.Variant("(ss)", tuple(self._foreground_window))},
        )


# Helpers that start and stop the service.

_bus = None
_api_instance = None
_controller_instances: dict[str, ControllerInstanceAPI] = {}


def start_dbus_service():
    """Publish the Deckard API on the session bus."""
    global _bus, _api_instance
    try:
        _bus = SessionMessageBus()
        _api_instance = DeckardAPI()
        _bus.publish_object(DBUS_OBJECT_PATH, _api_instance)

        # Sweep the decks that registered before the service existed. Every
        # later arrival publishes itself from the deck lifecycle. This code
        # runs on the main context, so it calls the same worker directly, and
        # one deck that fails to publish does not stop the others.
        if gl.deck_manager is not None:
            for controller in list(gl.deck_manager.deck_controller):
                _publish_on_main(controller)

        log.success(f"DBus API published at {DBUS_OBJECT_PATH}")
    except Exception as e:
        log.error(f"Failed to start DBus API service: {e}")


def publish_controller(controller) -> None:
    """Put a deck controller on the bus, from any registration thread.

    Decks register from the USB monitor thread, the boot re-enumeration thread
    and the main thread. dasbus holds its object registrations in a plain dict
    and dispatches on the GLib main context, so the registration marshals there.

    The bus check comes first and reads nothing from the controller, because a
    boot caller passes a controller that is still under construction. The sweep in
    start_dbus_service covers those decks.
    """
    if _bus is None:
        return
    GLib.idle_add(_publish_on_main, controller)


def unpublish_controller(controller) -> None:
    """Take a deck controller off the bus when the deck goes away.

    This guards and marshals as publish_controller does. A client that holds a
    proxy for the removed deck then gets UnknownObject, and the Controllers
    property stops naming it in the same step, because both read one registry.
    """
    if _bus is None:
        return
    GLib.idle_add(_unpublish_on_main, controller)


def _known_serial(controller) -> str:
    """The controller serial, read without a call to the device.

    A failure path can use it, where the deck itself may be the fault.
    """
    return getattr(controller, "_serial_number", None) or "<unknown>"


def _publish_on_main(controller) -> bool:
    """Idle worker for publish_controller. Main context only."""
    try:
        _publish_controller(controller)
    except Exception as e:
        log.error(
            f"DBus API: failed to publish controller {_known_serial(controller)}: "
            f"{e}. That deck is off the DBus API for this session."
        )
    return GLib.SOURCE_REMOVE


def _unpublish_on_main(controller) -> bool:
    """Idle worker for unpublish_controller. Main context only."""
    try:
        _unpublish_controller(controller)
    except Exception as e:
        log.error(
            f"DBus API: failed to unpublish controller {_known_serial(controller)}: "
            f"{e}. Its object stays on the bus, bound to a deck that is gone."
        )
    return GLib.SOURCE_REMOVE


def _publish_controller(controller) -> None:
    """Publish a ControllerInstanceAPI for a single deck controller.

    Main context only. See publish_controller.
    """
    if _bus is None:
        return  # the service stopped between queuing this and running it
    if gl.deck_manager is None or controller not in gl.deck_manager.deck_controller:
        # The deck went away before this idle ran. A publish now leaves an
        # object that no removal clears.
        return
    serial = controller.serial_number()
    existing = _controller_instances.get(serial)
    if existing is not None:
        if existing._controller is controller or (
                gl.deck_manager is not None
                and existing._controller in gl.deck_manager.deck_controller):
            return  # already published
        # A removed controller still claims the serial. Nothing orders its
        # unpublish against this publish. Removal queues its work outside the
        # deck manager lock, and the registration sites take no lock. Drop the
        # dead object here, so the connected deck reaches the API. The later
        # unpublish looks up its entry by controller identity, finds the serial
        # bound to this new controller, and does nothing.
        log.warning(
            f"DBus API: replacing the stale object for deck {serial} -- its "
            f"controller was removed, and this publish arrived first."
        )
        _bus.unpublish_object(existing._object_path)
        del _controller_instances[serial]
    obj_path = f"{CONTROLLER_BASE_PATH}/{_serial_to_dbus_path(serial)}"
    taken_by = _serial_published_at(obj_path)
    if taken_by is not None:
        log.error(
            f"DBus API: not publishing controller {serial}: {obj_path} already "
            f"belongs to {taken_by}. Two serials that differ only in characters "
            f"a DBus path cannot carry map to one path; that deck stays off the "
            f"API rather than taking over another deck's object."
        )
        return
    instance = ControllerInstanceAPI(controller)
    instance._object_path = obj_path
    # Seed the page that the deck already shows. The boot page loads before
    # this object exists, so a wait for the first switch leaves ActivePageName
    # empty on a deck that has a page.
    active_page = controller.active_page
    instance._active_page_name = "" if active_page is None else active_page.get_name()
    _bus.publish_object(obj_path, instance)
    # Record only after the bus accepts the object, so a failed publish leaves
    # the serial free for another publish.
    _controller_instances[serial] = instance
    log.info(f"DBus API: published controller {serial} at {obj_path}")
    _emit_controllers_changed()


def _unpublish_controller(controller) -> None:
    """Remove a controller's object from the bus.

    Main context only. See unpublish_controller.
    """
    if _bus is None:
        return  # already stopped, which unpublished everything
    serial = _serial_published_for(controller)
    if serial is None:
        # The controller never published, or it already unpublished, or a fast
        # replug gave the serial to a new controller whose object stays up.
        # This call matches on controller identity, not on the serial.
        return
    instance = _controller_instances.pop(serial)
    _bus.unpublish_object(instance._object_path)
    log.info(f"DBus API: unpublished controller {serial} from {instance._object_path}")
    _emit_controllers_changed()


def _serial_published_at(obj_path: str) -> str | None:
    """The serial already published at obj_path, if there is one."""
    for serial, instance in _controller_instances.items():
        if instance._object_path == obj_path:
            return serial
    return None


def _serial_published_for(controller) -> str | None:
    """The serial this exact controller is published under, if any."""
    for serial, instance in _controller_instances.items():
        if instance._controller is controller:
            return serial
    return None


def _emit_controllers_changed() -> None:
    """Tell the clients that the deck inventory changed.

    The publish and unpublish workers send this after they update the registry,
    so the payload carries the current object set and corrects a client that
    read the property early. The payload comes from the property itself, so it
    never names a deck that no client can address.
    """
    if _api_instance is None:
        return
    _emit_properties_changed(
        DBUS_OBJECT_PATH, TOP_IFACE,
        {"Controllers": GLib.Variant("as", _api_instance.Controllers)},
    )


def stop_dbus_service():
    """Disconnect from the session bus."""
    global _bus
    try:
        if _bus is not None:
            _bus.disconnect()
            _bus = None
            _controller_instances.clear()
            log.info("DBus API service stopped")
    except Exception as e:
        log.error(f"Failed to stop DBus API service: {e}")


def get_api_instance() -> DeckardAPI | None:
    """Return the active top-level API instance, or None if not started."""
    return _api_instance


def get_controller_instance(serial: str) -> ControllerInstanceAPI | None:
    """Return the API instance for a specific controller, or None."""
    return _controller_instances.get(serial)


def notify_active_page_changed(serial: str, page_name: str) -> None:
    """Update the ActivePageName for a controller's API object.

    Call this from DeckController.load_page() so that DBus clients
    see the new active page name.
    """
    instance = _controller_instances.get(serial)
    if instance is not None:
        instance.ActivePageName = page_name


def notify_foreground_window_changed(name: str, wm_class: str) -> None:
    """Update ForegroundWindow on the top-level API object.

    WindowGrabber.on_active_window_changed() calls this, so DBus clients see
    the foreground window change. That watcher runs only while a page holds a
    window auto-change rule, so the property tracks the desktop only then, and
    otherwise keeps its empty value. A constant feed would poll the desktop for
    the property alone. NotifyForegroundWindow can still set it.
    """
    if _api_instance is not None:
        _api_instance.ForegroundWindow = WindowInfo(name, wm_class)