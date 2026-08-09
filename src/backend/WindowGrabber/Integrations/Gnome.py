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

        self.proxy: Gio.DBusProxy | None = None
        if not gl.IS_MAC:
            self.connect_dbus()

    def install_extension(self) -> None:
        # A bare uuid string, like the live onboarding path
        # (OnboardingWindow.on_install_button_click) and like the
        # InstallRemoteExtension "(s)" signature GnomeExtensions marshals it
        # into. Wrapped in a list it could never match get_installed_
        # extensions' uuids either, so the "already installed" short-circuit
        # was dead too (#185).
        uuid = "streamcontroller@core447.com"
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
                # The extension exports no properties worth caching, and
                # auto-start would try to D-Bus-activate org.gnome.Shell
                # itself when the extension is absent.
                Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_AUTO_START,
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
        parsed = json.loads(answer)
        window = Window(parsed.get("wm_class"), parsed.get("title"))
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
    
    def get_active_window (self) -> Window | None:
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
        proxy = self.proxy
        if proxy is None:
            # Only reachable if the proxy vanished between the caller's
            # get_is_connected() and here. Raised as a GLib.Error because that
            # is what both callers already treat as "the call failed"; an
            # AttributeError on None would escape their except clause.
            raise GLib.Error("no D-Bus proxy for the GNOME extension")
        return proxy.call_sync(method_name, None, Gio.DBusCallFlags.NONE, -1, None).unpack()[0]

    def get_is_connected(self) -> bool:
        # Live owner check, not just "a proxy was built once": GDBusProxy
        # tracks the name owner, so this goes False if the Shell (or the
        # extension's exporter) drops off the bus after connect time.
        return self.proxy is not None and self.proxy.get_name_owner() is not None