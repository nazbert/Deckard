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

import threading
from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S
from src.backend.WindowGrabber.Window import Window

import subprocess
from loguru import logger as log

# Import globals
import globals as gl

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

class X11(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        portal = Xdp.Portal.new()
        self.flatpak = portal.running_under_flatpak()

        self.is_xprop_installed = self.get_is_xprop_installed()
        self.active_window_change_thread: "WatchForActiveWindowChange | None" = None

    @log.catch
    def _run_command(self, command: list[str]) -> subprocess.Popen[bytes] | None:
        if self.flatpak:
            command.insert(0, "flatpak-spawn")
            command.insert(1, "--host")
        try:
            return subprocess.Popen(command, stdout=subprocess.PIPE, cwd="/")
        except Exception as e:
            log.error(f"An error occurred while running {command}: {e}")
            return None

    @log.catch
    def get_is_xprop_installed(self) -> bool:
        try:
            xprop = self._run_command(["xprop", "-version"])
            if xprop is None:
                return False
            out = xprop.communicate()[0].decode("utf-8")
            return out not in ("", None)
        except Exception as e:
            log.error(f"An error occurred while running xprop: {e}")
            return False

    @log.catch
    def start_watching(self) -> None:
        if not self.is_xprop_installed:
            return

        thread = self.active_window_change_thread
        if thread is not None and thread.is_alive():
            return

        # A Thread object cannot be restarted, so each start builds a fresh
        # one -- which also re-primes the "last seen window" against whatever
        # is focused now rather than against a stale pre-stop reading.
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
        # Never join the calling thread to itself: routing a window change
        # can reach a page write, and a page write re-gates.
        if thread is not threading.current_thread():
            thread.join(timeout=WATCHER_STOP_TIMEOUT_S)
        if thread.is_alive():
            # Parked in an xprop read that outlived the timeout. The thread is
            # a daemon and its loop rechecks the stop flag the moment the read
            # returns, so it unwinds on its own; the reference is dropped
            # either way so a later start builds a clean one.
            log.warning("The X11 active window watcher did not stop within the timeout")

    @log.catch
    def get_all_windows(self) -> list[Window]:
        windows: list[Window] = []

        try:
            root = self._run_command(["xprop", "-root", "_NET_CLIENT_LIST"])
            if root is None:
                return []
            stdout, stderr = root.communicate()

            split = stdout.decode().split("#")
            if len(split) < 2:
                return windows
            window_ids = split[1].strip().split(", ")
        except subprocess.CalledProcessError as e:
            log.error(f"An error occurred while running xprop: {e}")
            return windows

        for window_id in window_ids:
            title = self.get_title(window_id)
            class_name = self.get_class(window_id)

            if title is None or class_name is None:
                continue

            windows.append(Window(class_name, title))

        return windows

    @log.catch
    def get_active_window(self) -> Window | None:
        try:
            window_ids = self.parse_window_ids()

            if len(window_ids) == 0:
                return None
            for window_id in window_ids:
                if window_id == "0x0":
                    continue
                title = self.get_title(window_id)
                class_name = self.get_class(window_id)

                if title is None or class_name is None:
                    return None

                return Window(class_name, title)

        except subprocess.CalledProcessError as e:
            log.error(f"An error occurred while running xprop: {e}")
            return None

        return None

    @log.catch
    def parse_window_ids(self) -> list[str]:
        root = self._run_command(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        if root is None:
            return []

        try:
            stdout, stderr = root.communicate()
            window_ids = stdout.decode().strip().split("#")[1].strip().split(", ")
            return window_ids
        except (IndexError, subprocess.CalledProcessError) as e:
            log.error(f"An error occurred while running xprop: {e}")
            return []

    @log.catch
    def get_title(self, window_id: str) -> str | None:
        if window_id == "0x0":
            return None
        try:
            xprop = self._run_command(["xprop", "-id", window_id, "WM_NAME"])
            if xprop is None:
                return None
            title_bytes = xprop.communicate()[0]
            if title_bytes is None:
                return None
            decoded = title_bytes.decode()
            split = decoded.split('"', 1)
            if len(split) < 2:
                return None
            title = split[1].rstrip('"\n')
            return title
        except subprocess.CalledProcessError as e:
            log.error(f"An error occurred while running xprop: {e}")
            return None

    @log.catch
    def get_class(self, window_id: str) -> str | None:
        if window_id == "0x0":
            return None
        try:
            xprop = self._run_command(["xprop", "-id", window_id, "WM_CLASS"])
            if xprop is None:
                return None
            class_bytes = xprop.communicate()[0]
            if class_bytes is None:
                return None
            decoded = class_bytes.decode()
            split = decoded.split('"')
            if len(split) < 4:
                return None
            window_class = split[3]
            return window_class
        except subprocess.CalledProcessError as e:
            log.error(f"An error occurred while running xprop: {e}")
            return None


class WatchForActiveWindowChange(threading.Thread):
    def __init__(self, x11: X11):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.x11 = x11
        self._stop_event = threading.Event()

        self.last_active_window = x11.get_active_window()

    def stop(self) -> None:
        """Asks the loop to end. Returns immediately -- the caller joins."""
        self._stop_event.set()

    @log.catch
    def run(self) -> None:
        while gl.threads_running and not self._stop_event.is_set():
            # Waiting on the stop event rather than sleeping cuts the stop
            # short instead of running out the poll interval. One already-
            # elapsed wait can still dispatch after a stop; harmless, since
            # routing re-reads the rules and finds none.
            if self._stop_event.wait(0.2):
                break
            # Catch per iteration (like the Hyprland integration's socket
            # listener): one failing poll or page-switch must not end this
            # thread -- an exception escaping to @log.catch would kill
            # window-based page switching until app restart.
            try:
                new_active_window = self.x11.get_active_window()
                if new_active_window is None:
                    continue
                if new_active_window == self.last_active_window:
                    continue

                self.last_active_window = new_active_window
                self.x11.window_grabber.on_active_window_changed(new_active_window)
            except Exception as e:
                # Full traceback: this coarse backstop catches errors from
                # deep in the poll plumbing (get_active_window -> xprop /
                # subprocess) that would otherwise be invisible; a bare
                # message is not enough to diagnose them. Matches the
                # per-deck catch in WindowGrabber.on_active_window_changed.
                log.opt(exception=True).error(
                    f"Unexpected error in X11 active window watcher: {e}"
                )
