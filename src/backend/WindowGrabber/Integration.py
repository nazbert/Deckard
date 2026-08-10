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

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

# How long a stop may wait for a watcher to notice and unwind. Stopping runs
# on the thread that edited the rule -- the GTK main thread for a page-editor
# edit -- so it has to be bounded: a watcher parked in a subprocess read is
# joined up to this long and then abandoned to its own next stop check. Every
# watcher is a daemon thread, so an abandoned one can never hold up quit.
WATCHER_STOP_TIMEOUT_S = 2.0


class Integration:
    """One desktop's window source.

    Construction must stay side-effect-light -- it may happen purely to
    answer a one-shot window query -- and must NOT begin watching: watching
    is what costs (a polling thread and its subprocesses, an IPC socket, a
    D-Bus subscription), and it is gated on a page actually having a window
    auto-change rule.
    """

    def __init__(self, window_grabber: "WindowGrabber") -> None:
        self.window_grabber = window_grabber

    def start_watching(self) -> None:
        """Begins reporting active-window changes to the grabber.

        Idempotent: a call while the watcher already runs is a no-op, and a
        call after stop_watching() starts a fresh watcher.
        """

    def stop_watching(self) -> None:
        """Stops reporting and reaps what the watcher holds, within
        WATCHER_STOP_TIMEOUT_S. Idempotent.

        One-shot queries (get_all_windows/get_active_window) must keep
        working afterwards -- only the watcher stops, not the integration.
        """

    def get_all_windows(self) -> list[Window]:
        return []

    def get_active_window(self) -> Window | None:
        return None