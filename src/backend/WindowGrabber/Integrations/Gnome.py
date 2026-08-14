"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from src.backend.WindowGrabber.Integration import Integration
from src.backend.WindowGrabber.Window import Window

import json
from loguru import logger as log

import globals as gl

from gi.repository import Gio, GLib

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

class Gnome(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        self.proxy: Gio.DBusProxy | None = None
        # 0 is GObject's "no handler" id, so it also means "not watching".
        # This integration owns no thread, and the FocusedWindowChanged
        # subscription is the watch.
        self._signal_handler_id: int = 0
        self.connect_dbus()

    def install_extension(self) -> None:
        # Pass a bare uuid string, like OnboardingWindow.on_install_button_click
        # and like the InstallRemoteExtension "(s)" signature that
        # GnomeExtensions marshals it into. A uuid inside a list matches no
        # entry from get_installed_extensions, which kills the check below.
        uuid = "streamcontroller@core447.com"
        installed_extensions = gl.gnome_extensions.get_installed_extensions()

        if uuid in installed_extensions:
            return

        gl.gnome_extensions.request_installation(uuid)


    def connect_dbus(self) -> None:
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                # The extension exports no properties worth a cache, and
                # auto-start would D-Bus-activate org.gnome.Shell itself when
                # the extension is absent.
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None,
                "org.gnome.Shell",
                "/org/gnome/Shell/Extensions/StreamController",
                "org.gnome.Shell.Extensions.StreamController",
                None
            )
            # Gio builds a proxy for a name that nobody owns. Without this
            # check an absent extension reads as connected, and every window
            # query raises instead of returning an empty result.
            if self.proxy.get_name_owner() is None:
                self.proxy = None
                raise RuntimeError("nothing owns org.gnome.Shell on the session bus")
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")
            pass

    @log.catch
    def start_watching(self) -> None:
        proxy = self.proxy
        if proxy is None or self._signal_handler_id:
            return
        self._signal_handler_id = proxy.connect("g-signal", self.on_dbus_signal)

    @log.catch
    def stop_watching(self) -> None:
        """Drops the shell's window-change subscription.

        This returns at once, because there is no thread to join. The proxy
        stays, because the page editor queries its matching-window list
        through it while no rule is enabled.

        This has a known limitation. A built GDBusProxy keeps its match rule
        on the session bus, so the shell still delivers FocusedWindowChanged to this
        process and this method drops it. That costs a message on an open
        connection, and no process spawn or poll. It applies only after
        something builds the proxy, which a session with no rules never does.
        """
        proxy = self.proxy
        handler_id = self._signal_handler_id
        self._signal_handler_id = 0
        if proxy is None or not handler_id:
            return
        proxy.disconnect(handler_id)


    def on_dbus_signal(self, proxy, sender_name: str, signal_name: str, parameters) -> None:
        if signal_name != "FocusedWindowChanged":
            return
        self.on_window_changed(parameters.unpack()[0])


    def on_window_changed(self, answer: str) -> None:
        parsed = json.loads(answer)
        window = Window(parsed.get("wm_class"), parsed.get("title"))
        self.window_grabber.on_active_window_changed(window=window)
        
    def get_all_windows(self) -> list[Window]:
        if not self.get_is_connected():
            return []
        
        try:
            answer = json.loads(self.call("GetAllWindows"))
        except (GLib.Error, IndexError, TypeError, json.JSONDecodeError):
            # The extension can be absent or the wrong version, and this app
            # does not ship it. Each step of call() can fail, with a
            # GLib.Error (no such method or interface, or the call failed),
            # an IndexError (a
            # reply body with no values, and call() indexes [0]), a TypeError
            # (a first value that json.loads refuses), and a JSONDecodeError.
            return []
        windows: list[Window] = []
        
        for window in answer:
            wm_class = window.get("wm_class")
            title = window.get("title")
            windows.append(Window(wm_class, title))

        return windows
    
    def get_active_window (self) -> Window | None:
        if not self.get_is_connected():
            return None
        try:
            answer = json.loads(self.call("GetFocusedWindow"))
        except (GLib.Error, IndexError, TypeError, json.JSONDecodeError):
            # The same set as get_all_windows(). See the note there.
            return None
        wm_class = answer.get("wm_class")
        title = answer.get("title")
        return Window(wm_class, title)

    def call(self, method_name: str) -> str:
        proxy = self.proxy
        if proxy is None:
            # Reachable only when the proxy goes away between the caller's
            # get_is_connected() and here. Raise GLib.Error, which both
            # callers read as "the call failed". An AttributeError on None
            # would escape their except clause.
            raise GLib.Error("no D-Bus proxy for the GNOME extension")
        return proxy.call_sync(method_name, None, Gio.DBusCallFlags.NONE, -1, None).unpack()[0]

    def get_is_connected(self) -> bool:
        # Check the live owner, not only that a proxy exists. GDBusProxy
        # tracks the name owner, so this turns False once the Shell or the
        # extension's exporter leaves the bus.
        return self.proxy is not None and self.proxy.get_name_owner() is not None