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
from src.backend.session_info import desktop_components
from src.backend.LockScreenManager.Detectors.Gnome import GnomeLockScreenDetector
from src.backend.LockScreenManager.Detectors.Cinnamon import CinnamonLockScreenDetector
from src.backend.LockScreenManager.Detectors.KDE import KDELockScreenDetector
from src.backend.LockScreenManager.Detectors.Hyprland import HyprlandLockScreenDetector
from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector
from loguru import logger as log

import globals as gl

class LockScreenManager:
    def __init__(self):
        self.locked = False
        self.detector = None

        threading.Thread(target=self.setup, daemon=True).start() # Detector setup can block, so keep it off the caller's thread

    @log.catch
    def setup(self):
        # XDG_CURRENT_DESKTOP holds a colon-separated list ("ubuntu:GNOME",
        # "GNOME-Classic:GNOME"), so match one component. A match on the whole
        # string misses these sessions and lock detection never starts.
        env_components = desktop_components()
        if "gnome" in env_components:
            self.detector = GnomeLockScreenDetector(self)
        elif "x-cinnamon" in env_components:
            self.detector = CinnamonLockScreenDetector(self)
        elif "kde" in env_components:
            self.detector = KDELockScreenDetector(self)
        elif "hyprland" in env_components:
            self.detector = HyprlandLockScreenDetector(self)
        else:
            self.detector = LogindLockScreenDetector(self)

    @log.catch
    def lock(self, active):
        gl.screen_locked = active
        if gl.presence_monitor:
            # Tell the monitor before the screensaver work below reads the
            # lock. Catch here, because this method's @log.catch returns on an
            # exception, which stops the lock from reaching allow_interaction,
            # the screensaver, and self.locked.
            try:
                gl.presence_monitor.on_lock_changed(active)
            except Exception:
                log.opt(exception=True).warning(
                    "LockScreenManager: the presence monitor failed to handle a "
                    "lock change; continuing with the lock itself"
                )

        if active:
            if not gl.settings_manager.app().lock_on_lock_screen:
                return

        if active == self.locked:
            return
        
        log.info(f"Locking screen: {active}")

        for controller in gl.deck_manager.deck_controller:
            controller.allow_interaction = not active
            if active:
                controller.screen_saver.show()
            else:
                controller.screen_saver.hide()

        self.locked = active
