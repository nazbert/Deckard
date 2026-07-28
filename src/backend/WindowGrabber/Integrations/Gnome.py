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

# Import globals first to get IS_MAC
import globals as gl

from gi.repository import Gio, GLib

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

class Gnome(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        self.proxy = None
        if not gl.IS_MAC:
            self.connect_dbus()

    def install_extension(self) -> None:
        uuid = ["streamcontroller@core447.com"]
        installed_extensions = gl.gnome_extensions.get_installed_extensions()

        if uuid in installed_extensions:
            return
        
        gl.gnome_extensions.request_installation(uuid)


    def connect_dbus(self) -> None:
        if gl.IS_MAC:
            return
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.gnome.Shell",
                "/org/gnome/Shell/Extensions/StreamController",
                "org.gnome.Shell.Extensions.StreamController",
                None
            )
            # A GDBusProxy is constructed happily for a name nobody owns, so
            # without this the extension being absent would read as connected
            # and every window query would raise instead of returning empty.
            if self.proxy.get_name_owner() is None:
                self.proxy = None
                raise RuntimeError("nothing owns org.gnome.Shell on the session bus")
            self.proxy.connect("g-signal", self.on_dbus_signal)
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")
            pass


    def on_dbus_signal(self, proxy, sender_name: str, signal_name: str, parameters) -> None:
        if signal_name != "FocusedWindowChanged":
            return
        self.on_window_changed(parameters.unpack()[0])


    def on_window_changed(self, answer: str) -> None:
        answer = json.loads(answer)
        window = Window(answer.get("wm_class"), answer.get("title"))
        self.window_grabber.on_active_window_changed(window=window)
        
    def get_all_windows(self) -> list[Window]:
        if not self.get_is_connected():
            return []
        
        try:
            answer = json.loads(self.call("GetAllWindows"))
        except (GLib.Error, IndexError, TypeError, json.JSONDecodeError):
            # The extension can be gone or the wrong version, so every step of
            # call() is an assumption about a third-party interface we don't
            # ship: GLib.Error (no such method/interface, or the call failed),
            # IndexError (a reply body with no values -- call() indexes [0]),
            # TypeError (a first value that isn't a string, which json.loads
            # rejects), JSONDecodeError (a string that isn't JSON).
            return []
        windows: list[Window] = []
        
        for window in answer:
            wm_class = window.get("wm_class")
            title = window.get("title")
            windows.append(Window(wm_class, title))

        return windows
    
    def get_active_window (self) -> Window:
        if not self.get_is_connected():
            return None
        try:
            answer = json.loads(self.call("GetFocusedWindow"))
        except (GLib.Error, IndexError, TypeError, json.JSONDecodeError):
            # Same set as get_all_windows() -- see the note there.
            return None
        wm_class = answer.get("wm_class")
        title = answer.get("title")
        return Window(wm_class, title)

    def call(self, method_name: str) -> str:
        return self.proxy.call_sync(method_name, None, Gio.DBusCallFlags.NONE, -1, None).unpack()[0]

    def get_is_connected(self) -> bool:
        return self.proxy is not None