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

import os
import socket
import threading
from src.backend.WindowGrabber.Integration import Integration, WATCHER_STOP_TIMEOUT_S
from src.backend.WindowGrabber.Window import Window

import subprocess
import json
from loguru import logger as log

import globals as gl

import gi

gi.require_version("Xdp", "1.0")
from gi.repository import Xdp

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

class Hyprland(Integration):
    def __init__(self, window_grabber: "WindowGrabber"):
        super().__init__(window_grabber=window_grabber)

        self.command_prefix: list[str] = []
        portal = Xdp.Portal.new()
        if portal.running_under_flatpak():
            self.command_prefix = ["flatpak-spawn", "--host"]

        self._socket_path = self._find_socket2_path()
        self.active_window_change_thread: "WatchForActiveWindowChange | None" = None

    def _find_socket2_path(self) -> str | None:
        """Find the Hyprland IPC event socket (socket2).

        Returns the path to the socket, or None if not found.
        """
        his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

        if his:
            path = os.path.join(runtime_dir, "hypr", his, ".socket2.sock")
            if os.path.exists(path):
                return path

        # Fall back to a scan of the hypr directory for the first valid socket.
        hypr_dir = os.path.join(runtime_dir, "hypr")
        if os.path.isdir(hypr_dir):
            for entry in os.listdir(hypr_dir):
                path = os.path.join(hypr_dir, entry, ".socket2.sock")
                if os.path.exists(path):
                    return path

        return None

    @log.catch
    def start_watching(self) -> None:
        thread = self.active_window_change_thread
        if thread is not None and thread.is_alive():
            return

        # A Thread object cannot restart, so each start builds a fresh one,
        # which connects the event socket again.
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
            # The thread is a daemon and stop() shuts its socket down, so a
            # listener parked in recv unwinds on its own. The reference drops
            # either way, so a later start builds a clean thread.
            log.warning("The Hyprland active window watcher did not stop within the timeout")

    def get_all_windows(self) -> list[Window]:
        windows: list[Window] = []
        try:
            output = subprocess.check_output([*self.command_prefix, "hyprctl", "clients", "-j"], text=True, cwd="/").strip()
            clients = json.loads(output)

            for client in clients:
                if "class" in client and "title" in client:
                    windows.append(Window(client["class"], client["title"]))

        except (subprocess.CalledProcessError, OSError) as e:
            # OSError covers a missing binary. The argv list runs no shell,
            # which would turn that into a 127 CalledProcessError.
            log.error(f"An error occurred while running hyprctl: {e}")
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse JSON: {e}")

        return windows

    def get_active_window(self) -> Window | None:
        try:
            output = subprocess.check_output([*self.command_prefix, "hyprctl", "activewindow", "-j"], text=True, cwd="/").strip()
            client = json.loads(output)

            if "class" in client and "title" in client:
                return Window(client["class"], client["title"])
        except (subprocess.CalledProcessError, OSError) as e:
            # OSError covers a missing binary. The argv list runs no shell,
            # which would turn that into a 127 CalledProcessError.
            log.error(f"An error occurred while running hyprctl: {e}")
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse JSON: {e}")

        return None


class WatchForActiveWindowChange(threading.Thread):
    """Watch for active window changes on Hyprland's IPC event socket.

    This thread connects to socket2 and listens for activewindow>> events. A
    poll of hyprctl activewindow every 200 ms starts a process each time, plus
    flatpak-spawn and xdg-dbus-proxy under Flatpak. The socket costs I/O wait
    only, and no CPU while the active window stays.

    It falls back to the poll when the socket is unavailable, which happens
    outside Hyprland and with an unresolvable socket path.
    """

    def __init__(self, hyprland: Hyprland):
        super().__init__(name="WatchForActiveWindowChange", daemon=True)
        self.hyprland = hyprland
        self._stop_event = threading.Event()
        # Published for stop(), which breaks a listener parked in recv. This
        # thread assigns it; stop() reads it.
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        """Asks the loop to end. Returns at once, and the caller joins.

        The event alone leaves a listener parked in recv for up to the socket
        timeout, so this shuts the connection down too. The pending recv then
        returns and the loop reaches its next stop check. A shutdown, and not
        a close, keeps the listener's own close() well-defined.
        """
        self._stop_event.set()
        sock = self._sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            # The socket is closed, or it never connected. Nothing to wake.
            pass

    @log.catch
    def run(self) -> None:
        socket_path = self.hyprland._socket_path

        if socket_path and os.path.exists(socket_path):
            log.info(f"Using Hyprland IPC socket for window change events: {socket_path}")
            self._run_socket(socket_path)
        else:
            log.warning("Hyprland IPC socket not found, falling back to polling")
            self._run_polling()

    def _run_socket(self, socket_path: str) -> None:
        """Listen on Hyprland's socket2 for activewindow>> events."""
        while gl.threads_running and not self._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect(socket_path)
                self._sock = sock

                buffer = ""
                while gl.threads_running and not self._stop_event.is_set():
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        # The compositor closed the socket. Connect again.
                        break

                    buffer += data.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.startswith("activewindow>>"):
                            # Format: activewindow>>CLASS,TITLE
                            payload = line[len("activewindow>>"):]
                            # The class holds no comma, but the title can.
                            parts = payload.split(",", 1)
                            if len(parts) == 2:
                                wm_class, title = parts
                                window = Window(wm_class, title)
                                self.hyprland.window_grabber.on_active_window_changed(window)

                sock.close()
            except OSError as e:
                log.warning(f"Hyprland socket error: {e}, retrying in 2s")
            except Exception as e:
                log.error(f"Unexpected error in Hyprland socket listener: {e}")
            finally:
                self._sock = None

            # Wait before the next connection. Wait on the stop event, so a
            # stop mid-backoff does not hold for the full delay.
            if self._stop_event.wait(2):
                break

    def _run_polling(self) -> None:
        """The fallback, which polls hyprctl every 200 ms."""
        last_active_window = self.hyprland.get_active_window()
        while gl.threads_running and not self._stop_event.is_set():
            # Wait on the stop event instead of a sleep, so a stop ends the
            # loop before the poll interval runs out. A wait that already
            # elapsed can dispatch once after a stop; routing then re-reads
            # the rules and finds none.
            if self._stop_event.wait(0.2):
                break
            new_active_window = self.hyprland.get_active_window()
            if new_active_window is None:
                continue
            if new_active_window == last_active_window:
                continue

            last_active_window = new_active_window
            self.hyprland.window_grabber.on_active_window_changed(new_active_window)

