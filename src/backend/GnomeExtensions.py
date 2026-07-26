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
# Import globals first to get IS_MAC
import globals as gl

from gi.repository import Gio, GLib

from loguru import logger as log

class GnomeExtensions:
    def __init__(self):
        self.proxy = None
        self.connect_dbus()

    def connect_dbus(self) -> None:
        if gl.IS_MAC:
            return
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.gnome.Shell",
                "/org/gnome/Shell",
                "org.gnome.Shell.Extensions",
                None
            )
            # A GDBusProxy is constructed happily for a name nobody owns, so
            # off GNOME the failure would otherwise surface much later, out of
            # the calls below that have no error path. The owner check keeps
            # "not connected" where it used to be.
            if self.proxy.get_name_owner() is None:
                self.proxy = None
                raise RuntimeError("nothing owns org.gnome.Shell on the session bus")
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")
            pass

    def get_is_connected(self) -> bool:
        return self.proxy is not None

    def get_installed_extensions(self) -> list[str]:
        extensions: list[str] = []
        if not self.get_is_connected(): return extensions

        # a{sa{sv}} keyed by uuid; iterating the reply dict yields the uuids,
        # exactly what the dict-like dbus-python reply yielded.
        reply = self.proxy.call_sync("ListExtensions", None, Gio.DBusCallFlags.NONE, -1, None)
        for extension in reply.unpack()[0]:
            extensions.append(extension)
        return extensions

    def request_installation(self, uuid: str) -> bool:
        if not self.get_is_connected(): return False
        # Default timeout on purpose: GNOME Shell only answers this once the
        # user has dismissed its install confirmation dialog.
        reply = self.proxy.call_sync(
            "InstallRemoteExtension", GLib.Variant("(s)", (uuid,)), Gio.DBusCallFlags.NONE, -1, None
        )
        response = reply.unpack()[0]
        return True if response == "successful" else False