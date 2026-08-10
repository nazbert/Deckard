"""
Author: Qalthos
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
import json
from loguru import logger as log

# Import globals
import globals as gl

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

# Import typing
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

class Sway(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        self.command_prefix: list[str] = []
        portal = Xdp.Portal.new()
        if portal.running_under_flatpak():
            self.command_prefix = ["flatpak-spawn", "--host"]

        self.active_window_change_thread: "WatchForActiveWindowChange | None" = None

    @log.catch
    def start_watching(self) -> None:
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
        thread.join(timeout=WATCHER_STOP_TIMEOUT_S)
        if thread.is_alive():
            # Parked in a swaymsg read that outlived the timeout. The thread
            # is a daemon and its loop rechecks the stop flag as soon as the
            # read returns, so it unwinds on its own; the reference is dropped
            # either way so a later start builds a clean one.
            log.warning("The Sway active window watcher did not stop within the timeout")

    def get_all_windows(self) -> list[Window]:
        return [self._parse_window(client) for client in self._get_windows()]

    def get_active_window(self) -> Window | None:
        window_list = self._get_windows()

        for client in window_list:
            if not client["focused"]:
                continue
            return self._parse_window(client)

        return None

    def _walk_tree(self, node, windows: list[dict[str, Any]]):
        if "window_properties" in node or "app_id" in node:
           # Try to only add actual windows
           windows.append(node)

        if "nodes" in node:
           for child in node.get("nodes"):
               self._walk_tree(child, windows)
           for child in node.get("floating_nodes"):
               self._walk_tree(child, windows)

    def _get_windows(self) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        try:
            # Run the swaymsg command and capture the output
            output = subprocess.check_output([*self.command_prefix, "swaymsg", "-t", "get_tree"], text=True, cwd="/").strip()
            # Parse the JSON output into a Python list
            clients = json.loads(output)

            for output in clients.get("nodes", []):
                for workspace in output.get("nodes", []):
                    self._walk_tree(workspace, windows)

        except (subprocess.CalledProcessError, OSError) as e:
            # OSError covers the binary being absent: with the argv list there
            # is no shell to turn that into a 127 CalledProcessError.
            log.error(f"An error occurred while running swaymsg: {e}")
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse JSON: {e}")

        return windows

    def _parse_window(self, client: dict[str, Any]) -> Window:
        if "window_properties" in client:
            # XWindow clients are slightly differently organized
            props = client["window_properties"]
            return Window(props["class"], props["title"])
        else:
            return Window(client.get("app_id", ""), client["name"])

class WatchForActiveWindowChange(threading.Thread):
    def __init__(self, sway: Sway):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.sway = sway
        self._stop_event = threading.Event()

        self.last_active_window = sway.get_active_window()

    def stop(self) -> None:
        """Asks the loop to end. Returns immediately -- the caller joins."""
        self._stop_event.set()

    @log.catch
    def run(self) -> None:
        while gl.threads_running and not self._stop_event.is_set():
            # Waiting on the stop event instead of sleeping is what makes a
            # stop take ~0 ms rather than up to a full poll interval.
            if self._stop_event.wait(0.2):
                break
            new_active_window = self.sway.get_active_window()
            if new_active_window is None:
                continue
            if new_active_window == self.last_active_window:
                continue

            self.last_active_window = new_active_window
            self.sway.window_grabber.on_active_window_changed(new_active_window)
