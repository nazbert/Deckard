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
import shutil
import sys
import threading

# Automatically detect macOS
IS_MAC = sys.platform == "darwin"

if not IS_MAC:
    import gi
    gi.require_version("Xdp", "1.0")
    from gi.repository import Xdp

from loguru import logger as log

def is_flatpak():
    return os.path.isfile('/.flatpak-info')

# Serializes concurrent setup_autostart() calls: the portal's async callback
# may land long after a NEWER setup_autostart() call already changed the
# on-disk state -- a stale callback must never clobber it (the classic case:
# disable removes the entry synchronously, then the disable/enable portal
# request fails asynchronously and the fallback re-installed a flatpak-style
# entry exec'ing /app/bin/launch.sh, which is broken on native installs).
_autostart_lock = threading.Lock()
_autostart_generation = 0


def _current_autostart_generation() -> int:
    with _autostart_lock:
        return _autostart_generation


@log.catch
def setup_autostart(enable: bool = True):
    global _autostart_generation
    if IS_MAC:
        return

    with _autostart_lock:
        _autostart_generation += 1
        generation = _autostart_generation

    if is_flatpak():
        setup_autostart_flatpak(enable, generation)
        if not enable:
            # Also remove any manual fallback entry a previous failed portal
            # request may have left behind.
            setup_autostart_desktop_entry(False)
    else:
        # Native installs never go through the portal: its async callback was
        # the racing writer that re-installed a flatpak-style entry after the
        # synchronous removal. The native desktop file is the only correct
        # entry here, for enable and disable alike.
        setup_autostart_desktop_entry(enable, native=True)


def setup_autostart_flatpak(enable: bool = True, generation: int = None):
    """
    Use portal to autostart for Flatpak
    Documentation:
    https://libportal.org/method.Portal.request_background.html
    https://libportal.org/method.Portal.request_background_finish.html
    https://docs.flatpak.org/de/latest/portal-api-reference.html#gdbus-org.freedesktop.portal.Background
    """
    def request_background_callback(portal, result, user_data):
        try:
            success = portal.request_background_finish(result)
        except Exception:
            success = False
        log.info(f"request_background success={success}")
        if success:
            return
        if generation is not None and generation != _current_autostart_generation():
            # A newer setup_autostart() call superseded this request; its
            # outcome, not ours, owns the on-disk state now.
            log.info("Skipping stale autostart fallback (superseded request)")
            return
        # Fall back to a manual desktop entry -- honoring the ORIGINAL
        # intent: a failed disable request must remove (never re-create)
        # the entry.
        setup_autostart_desktop_entry(enable)

    xdp = Xdp.Portal.new()

    try:
        flag = Xdp.BackgroundFlags.AUTOSTART if enable else Xdp.BackgroundFlags.ACTIVATABLE

        # Request Autostart
        xdp.request_background(
            None,  # parent
            "Autostart StreamController",  # reason
            ["/app/bin/launch.sh", "-b"],  # commandline
            flag,
            None,  # cancellable
            request_background_callback,
            None,  # user_data
        )
    except Exception:
        log.error(f"request_background failed")
        setup_autostart_desktop_entry(enable)

def setup_autostart_desktop_entry(enable: bool = True, native: bool = False):
    log.info("Setting up autostart using desktop entry")

    import globals as gl

    xdg_config_home = os.path.join(os.environ.get("HOME"), ".config")
    AUTOSTART_DIR = os.path.join(xdg_config_home, "autostart")
    AUTOSTART_DESKTOP_PATH = os.path.join(AUTOSTART_DIR, "StreamController.desktop")

    if enable:
        try:
            os.makedirs(os.path.dirname(AUTOSTART_DESKTOP_PATH), exist_ok=True)
            if native:
                copy_desktop_file(os.path.join(gl.MAIN_PATH, "flatpak", "autostart-native.desktop"), AUTOSTART_DESKTOP_PATH, True) # Why overwrite? In case someone is using the Flatpak and the source version
            else:
                copy_desktop_file(os.path.join(gl.MAIN_PATH, "flatpak", "autostart.desktop"), AUTOSTART_DESKTOP_PATH, True) # Why overwrite? In case someone is using the Flatpak and the source version
            log.info(f"Autostart set up at: {AUTOSTART_DESKTOP_PATH}")
        except Exception as e:
            log.error(f"Failed to set up autostart at: {AUTOSTART_DESKTOP_PATH} with error: {e}")
    else:
        if os.path.exists(AUTOSTART_DESKTOP_PATH):
            try:
                os.remove(AUTOSTART_DESKTOP_PATH)
                log.info(f"Autostart removed from: {AUTOSTART_DESKTOP_PATH}")
            except Exception as e:
                log.error(f"Failed to remove autostart from: {AUTOSTART_DESKTOP_PATH} with error: {e}")

def copy_desktop_file(source: str, target: str, overwrite: bool = False):
    if not overwrite and os.path.exists(target):
        log.info(f"Desktop file already exists at: {target}")
        return
    
    # Check that source exists
    if not os.path.exists(source):
        log.error(f"Desktop file does not exist at: {source}")
        return
    
    try:
        shutil.copyfile(source, target)
        log.info(f"Desktop file copied from: {source} to: {target}")
    except Exception as e:
        log.error(f"Failed to copy desktop file from: {source} to: {target} with error: {e}")