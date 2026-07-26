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
from src.backend.LockScreenManager.LockScreenDetector import LockScreenDetector

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

# Import globals first to get IS_MAC
import globals as gl

from gi.repository import Gio

from loguru import logger as log

class KDELockScreenDetector(LockScreenDetector):
    def __init__(self, lock_screen_manager: "LockScreenManager"):
        self.lock_screen_manager: "LockScreenManager" = lock_screen_manager

        self.bus = None
        self.setup_dbus()

    def screen_saver_active_changed(self, active):
        active = True if active == 1 else False

        self.lock_screen_manager.lock(active)

    def on_dbus_signal(self, connection, sender_name, object_path, interface_name, signal_name, parameters):
        self.screen_saver_active_changed(parameters.unpack()[0])

    def setup_dbus(self):
        if gl.IS_MAC:
            return
        try:
            # Connect to the Session Bus. Kept referenced: the subscription
            # below lives exactly as long as the connection does.
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            # Define the signal to listen to. setup() runs on the manager's
            # daemon thread, which has no thread-default main context, so
            # GDBus dispatches the callback on the global default one -- the
            # GTK main loop, same as the old glib mainloop integration.
            self.bus.signal_subscribe(
                None,
                "org.freedesktop.ScreenSaver",
                "ActiveChanged",
                "/org/freedesktop/ScreenSaver",
                None,
                Gio.DBusSignalFlags.NONE,
                self.on_dbus_signal
            )
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")
