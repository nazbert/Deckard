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

from src.backend.WindowGrabber.Window import Window

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

# How long a stop waits for a watcher to notice and unwind. A page-editor edit
# stops the watcher on the GTK main thread, so the join must be bounded. A
# watcher parked in a subprocess read is abandoned to its own next stop check.
# Every watcher is a daemon thread and cannot hold up quit.
WATCHER_STOP_TIMEOUT_S = 2.0


class Integration:
    """One desktop's window source.

    A one-shot window query can construct this, so the constructor keeps its
    side effects light and does not start a watcher. A watcher costs a polling
    thread, an IPC socket, or a D-Bus subscription; a page rule gates it.
    """

    def __init__(self, window_grabber: "WindowGrabber") -> None:
        self.window_grabber = window_grabber

    def start_watching(self) -> None:
        """Begins reporting active-window changes to the grabber.

        Idempotent. A call while the watcher already runs does nothing, and a
        call after stop_watching() starts a fresh watcher.
        """

    def stop_watching(self) -> None:
        """Stops reporting and reaps what the watcher holds, within
        WATCHER_STOP_TIMEOUT_S. Idempotent. Only the watcher stops, so
        get_all_windows and get_active_window keep working.
        """

    def get_all_windows(self) -> list[Window]:
        return []

    def get_active_window(self) -> Window | None:
        return None