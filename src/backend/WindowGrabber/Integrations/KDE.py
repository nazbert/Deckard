"""
Author: flifloo
Year: 2025

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import threading
from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S
from src.backend.WindowGrabber.Window import Window

from subprocess import Popen, CalledProcessError, PIPE
from loguru import logger as log

import globals as gl

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber


class KDE(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        portal = Xdp.Portal.new()
        self.flatpak = portal.running_under_flatpak()

        self.is_kdotool_installed = self.get_is_kdotool_installed()
        self.active_window_change_thread: "WatchForActiveWindowChange | None" = None

    @log.catch
    def _run_command(self, command: list[str]) -> Optional[Popen[bytes]]:
        if self.flatpak:
            command.insert(0, "flatpak-spawn")
            command.insert(1, "--host")
        try:
            return Popen(command, stdout=PIPE, cwd="/")
        except Exception as e:
            log.error(f"An error occurred while running {command}: {e}")
            return None

    @log.catch
    def get_is_kdotool_installed(self) -> bool:
        try:
            kdotool = self._run_command(["kdotool", "--version"])
            if kdotool is None:
                return False
            out = kdotool.communicate()[0].decode("utf-8")
            return out not in ("", None)
        except Exception as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return False

    @log.catch
    def start_watching(self) -> None:
        if not self.is_kdotool_installed:
            return

        thread = self.active_window_change_thread
        if thread is not None and thread.is_alive():
            return

        # A Thread object cannot restart, so each start builds a fresh one.
        # The new thread also primes its "last seen window" from the window
        # focused now, not from a reading taken before the stop.
        thread = WatchForActiveWindowChange(self)
        self.active_window_change_thread = thread
        thread.start()

    @log.catch
    def stop_watching(self) -> None:
        thread = self.active_window_change_thread
        self.active_window_change_thread = None
        if thread is None:
            return

        thread.stop()
        if thread is threading.current_thread():
            # Never join the calling thread to itself. A window change can
            # reach a page write, and a page write re-gates. The loop ends at
            # its next stop check. The return also keeps the timeout warning
            # below for a real timeout, not for a skipped join.
            return

        thread.join(timeout=WATCHER_STOP_TIMEOUT_S)
        if thread.is_alive():
            # The thread is parked in a kdotool read past the timeout. It is a
            # daemon, and its loop rechecks the stop flag once the read
            # returns, so it unwinds on its own. The reference drops either
            # way, so a later start builds a clean thread.
            log.warning("The KDE active window watcher did not stop within the timeout")

    @log.catch
    def get_all_windows(self) -> list[Window]:
        windows: list[Window] = []

        try:
            root = self._run_command(["kdotool", "search", "."])
            if root is None:
                return []
            stdout, _ = root.communicate()

            window_ids = stdout.decode().strip().split("\n")
            if len(window_ids) < 2:
                return windows
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return windows

        for window_id in window_ids:
            window = self.get_window(window_id)
            if window is not None:
                windows.append(window)

        return windows

    @log.catch
    def get_active_window_id(self) -> Optional[str]:
        try:
            kdotool = self._run_command(["kdotool", "getactivewindow"])
            if kdotool is None:
                return None
            stdout, _ = kdotool.communicate()
            window_id = stdout.decode().strip()
            if len(window_id) == 0:
                return None
            return window_id
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return None

    @log.catch
    def get_active_window(self) -> Optional[Window]:
        window_id = self.get_active_window_id()
        if window_id is None:
            return None
        return self.get_window(window_id)

    @log.catch
    def get_window(self, window_id: str) -> Optional[Window]:
        title = self.get_title(window_id)
        class_name = self.get_class(window_id)
        if title is None or class_name is None:
            return None
        return Window(class_name, title)

    @log.catch
    def get_title(self, window_id: str) -> Optional[str]:
        try:
            kdotool = self._run_command(["kdotool", "getwindowname", window_id])
            if kdotool is None:
                return None
            title = kdotool.communicate()[0].decode().strip()
            if title is None or len(title) < 2:
                return None
            return title
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return None

    @log.catch
    def get_class(self, window_id: str) -> Optional[str]:
        try:
            kdotool = self._run_command(["kdotool", "getwindowclassname", window_id])
            if kdotool is None:
                return None
            window_class = kdotool.communicate()[0].decode().strip()
            if window_class is None or len(window_class) < 4:
                return None
            return window_class
        except CalledProcessError as e:
            log.error(f"An error occurred while running kdotool: {e}")
            return None


class WatchForActiveWindowChange(threading.Thread):
    def __init__(self, kde: KDE):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.kde = kde
        self._stop_event = threading.Event()

        self.last_window_id: Optional[str] = None
        self.last_active_window: Optional[Window] = None

        window_id = self.kde.get_active_window_id()
        if window_id is not None:
            self.last_window_id = window_id
            self.last_active_window = self.kde.get_window(window_id)

    def stop(self) -> None:
        """Asks the loop to end. Returns at once, and the caller joins."""
        self._stop_event.set()

    @log.catch
    def run(self) -> None:
        while gl.threads_running and not self._stop_event.is_set():
            # Wait on the stop event instead of a sleep, so a stop ends the
            # loop before the poll interval runs out. A wait that already
            # elapsed can dispatch once after a stop; routing then re-reads
            # the rules and finds none.
            if self._stop_event.wait(0.2):
                break
            window_id = self.kde.get_active_window_id()
            if window_id is None:
                continue
            if window_id == self.last_window_id:
                continue

            self.last_window_id = window_id
            new_active_window = self.kde.get_window(window_id)
            if new_active_window is None:
                continue
            if new_active_window == self.last_active_window:
                continue

            self.last_active_window = new_active_window
            self.kde.window_grabber.on_active_window_changed(new_active_window)
