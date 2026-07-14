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
import re
import shutil
import sys

import appinfo

# Automatically detect macOS
IS_MAC = sys.platform == "darwin"

# Autostart entries written under the pre-rename identity. The app-written
# "StreamController.desktop" would relaunch an old-identity build at every
# login; the id-named one is a flatpak portal remnant no code path removed on
# native installs. Removed on every launch (self-healing), since a still-
# installed old build could recreate them after the one-time data migration.
LEGACY_AUTOSTART_NAMES = ("StreamController.desktop", appinfo.OLD_APP_ID + ".desktop")

if not IS_MAC:
    import gi
    gi.require_version("Xdp", "1.0")
    from gi.repository import Xdp

from loguru import logger as log

def is_flatpak():
    return os.path.isfile('/.flatpak-info')

# Orders setup_autostart() calls against the portal's async callback: the
# callback may land long after a NEWER setup_autostart() call already changed
# the on-disk state -- a stale callback must never clobber it (the classic
# case: disable removes the entry synchronously, then the disable/enable
# portal request fails asynchronously and the fallback re-installed a
# flatpak-style entry exec'ing /app/bin/launch.sh, broken on native installs).
#
# No lock: setup_autostart() is called from the GTK main loop (the settings
# switch's notify::active) and request_background_callback is also dispatched
# on the main loop, so the counter is only ever touched by that single thread.
# The generation stamp, not mutual exclusion, is what makes disable
# authoritative over a stale callback.
_autostart_generation = 0


def _current_autostart_generation() -> int:
    return _autostart_generation


def remove_legacy_autostart_entries():
    """Delete any pre-rename autostart entries. Runs on every launch so it
    self-heals if a transient failure or a re-run of an old build leaves one
    behind -- the old data-migration path could only do this once."""
    autostart_dir = os.path.join(os.environ.get("HOME") or os.path.expanduser("~"),
                                 ".config", "autostart")
    for name in LEGACY_AUTOSTART_NAMES:
        path = os.path.join(autostart_dir, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                log.info(f"Removed legacy autostart entry: {path}")
            except OSError as e:
                log.error(f"Failed to remove legacy autostart entry {path}: {e}")


@log.catch
def setup_autostart(enable: bool = True):
    global _autostart_generation
    if IS_MAC:
        return

    remove_legacy_autostart_entries()

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
            "Autostart Deckard",  # reason
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
    AUTOSTART_DESKTOP_PATH = os.path.join(AUTOSTART_DIR, "Deckard.desktop")

    if enable:
        if native:
            _install_desktop_file("autostart-native.desktop", AUTOSTART_DESKTOP_PATH, exec_args="-b")
        else:
            try:
                os.makedirs(os.path.dirname(AUTOSTART_DESKTOP_PATH), exist_ok=True)
                copy_desktop_file(os.path.join(gl.MAIN_PATH, "flatpak", "autostart.desktop"), AUTOSTART_DESKTOP_PATH, True) # flatpak entry: Exec is the sandbox launcher, kept verbatim
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

def ensure_app_desktop_entry():
    """Install/refresh ~/.local/share/applications/<app id>.desktop.

    On Wayland the compositor maps a window's app_id (the GtkApplication id)
    to a desktop file of the same name to find its taskbar/dock icon; on
    source installs nothing else provides one.
    """
    if IS_MAC or is_flatpak():
        return
    target = os.path.join(os.environ.get("HOME") or os.path.expanduser("~"),
                          ".local", "share", "applications", f"{appinfo.APP_ID}.desktop")
    _install_desktop_file("deckard-app.desktop", target)


def _launcher_exec(extra_args: str = "") -> str:
    """Absolute launch command for generated native desktop entries, so they
    work without the optional ~/.local/bin/deckard PATH symlink.

    Prefer the wrapper when it is on PATH (it exports the MALLOC_* vars that
    let main.py skip its self-re-exec); otherwise self-reference the running
    interpreter and main.py -- always resolvable, never a dangling command.
    """
    import globals as gl
    wrapper = shutil.which("deckard")
    cmd = wrapper if wrapper else f"{sys.executable} {os.path.join(gl.MAIN_PATH, 'main.py')}"
    return f"{cmd} {extra_args}".rstrip()


def _install_desktop_file(template_name: str, target: str, exec_args: str = ""):
    """Write a native desktop entry from a flatpak/ template, rewriting Icon=
    to an absolute repo path and Exec= to an absolute launch command, and
    skipping the write when the target is already byte-identical.

    The compare-before-write avoids a needless mtime bump that makes desktop
    environments re-scan their application cache on every launch.
    """
    import globals as gl
    source = os.path.join(gl.MAIN_PATH, "flatpak", template_name)
    icon_path = os.path.join(gl.MAIN_PATH, "Assets", "icons", "hicolor",
                             "512x512", "apps", f"{appinfo.APP_ID}.png")
    try:
        with open(source) as f:
            content = f.read()
    except OSError as e:
        log.error(f"Desktop template missing at {source}: {e}")
        return
    content = content.replace(f"Icon={appinfo.APP_ID}", f"Icon={icon_path}")
    content = re.sub(r"(?m)^Exec=.*$", lambda m: "Exec=" + _launcher_exec(exec_args), content)
    try:
        with open(target) as f:
            if f.read() == content:
                return  # unchanged -- skip the write and the DE cache churn
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(content)
        log.info(f"Desktop entry installed at: {target}")
    except OSError as e:
        log.error(f"Failed to install desktop entry at {target}: {e}")


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