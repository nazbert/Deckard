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
from collections.abc import Callable
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

from gi.repository import Gio

from loguru import logger as log

class LockScreenDetector:
    def __init__(self, lock_screen_manager: "LockScreenManager"):
        self.lock_screen_manager: "LockScreenManager" = lock_screen_manager
        # Stays None whenever the bus connection fails below.
        self.bus: Gio.DBusConnection | None = None

    def subscribe_to_screen_saver(self, bus_name: str | None, object_path: str, interface: str, callback: Callable[..., Any]) -> None:
        """Listen for the ScreenSaver ActiveChanged signal on the session bus.

        bus_name is the sender to match. The desktop detectors pass None and
        accept any sender.
        """
        try:
            # Keep the connection referenced. The subscription below lives
            # exactly as long as the connection does.
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            # setup() runs on the manager's daemon thread, which has no
            # thread-default main context, so GDBus dispatches the callback on
            # the global default one, which is the GTK main loop.
            self.bus.signal_subscribe(
                bus_name,
                interface,
                "ActiveChanged",
                object_path,
                None,
                Gio.DBusSignalFlags.NONE,
                callback
            )
        except Exception as e:
            log.error(f"Failed to connect to D-Bus: {e}")