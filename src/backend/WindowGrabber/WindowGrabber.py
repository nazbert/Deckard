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

import re
from loguru import logger as log

import globals as gl

from src.backend.session_info import desktop_components, session_type
from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.Integration import Integration
from src.backend.WindowGrabber.Integrations.Hyprland import Hyprland
from src.backend.WindowGrabber.Integrations.Gnome import Gnome
from src.backend.WindowGrabber.Integrations.Sway import Sway
from src.backend.WindowGrabber.Integrations.X11 import X11
from src.backend.WindowGrabber.Integrations.KDE import KDE
from src.api import notify_foreground_window_changed


def select_integration_class(environment_components: list[str], server: str | None) -> type[Integration] | None:
    """The integration that can grab windows in this session, or None when
    none can.

    `environment_components` are the XDG_CURRENT_DESKTOP components, matched
    one by one: the variable is a colon-separated list ("ubuntu:GNOME",
    "sway:wlroots:swayfx"), so comparing it as a single string leaves stock
    distro sessions with no integration and automatic page switching dead.

    The order is load-bearing. The X11 session check sits above the KDE
    component, so a KDE session on Xorg reads windows through xprop rather
    than kdotool; it sits below the Wayland-only compositors, which have no
    X11 fallback to be demoted to.
    """
    if "hyprland" in environment_components:
        return Hyprland
    if "gnome" in environment_components:
        return Gnome
    # Sway forks name themselves in the same component ("swayfx") and speak
    # the same IPC.
    if any("sway" in component for component in environment_components):
        return Sway
    if server == "x11":
        return X11
    if "kde" in environment_components:
        return KDE
    return None


class WindowGrabber:
    def __init__(self):
        self.environment_components: list[str] = []
        self.server: str | None = None

        self.integration: Integration | None = None
        self.init_integration()

    @log.catch
    def init_integration(self) -> None:
        self.environment_components = desktop_components()
        self.server = session_type()

        integration_class = select_integration_class(self.environment_components, self.server)
        if integration_class is None:
            log.error(f"Unsupported environment: {self.environment_components} with server: {self.server} for window grabber.")
            return

        log.info(f"Initializing window grabber for environment: {self.environment_components} under server: {self.server}")
        self.integration = integration_class(self)

    @log.catch
    def get_all_windows(self) -> list[Window]:
        """
        returns a list of [wm_class, title] lists
        """
        if self.integration is None:
            return []

        return self.integration.get_all_windows()

    def get_all_matching_windows(self, class_regex: str, title_regex: str) -> list[Window]:
        all_windows = self.get_all_windows()

        matching_windows: list[Window] = []
        for window in all_windows:
            if self.get_is_window_matching(window, class_regex, title_regex):
                matching_windows.append(window)

        return matching_windows

    def get_is_window_matching(self, window: Window, class_regex: str | None, title_regex: str | None) -> bool:
        wm_class = window.wm_class
        title = window.title
        if wm_class is None or title is None or class_regex is None or title_regex is None:
            return False
        try:
            class_match = re.search(class_regex, wm_class, re.IGNORECASE)
            title_match = re.search(title_regex, title, re.IGNORECASE)
        except re.error:
            return False
        return bool(class_match and title_match)

    def on_active_window_changed(self, window: Window) -> None:
        # log.info(f"Active window changed to: {window}")

        # Notify DBus API of the foreground window change
        notify_foreground_window_changed(window.title, window.wm_class)

        if gl.deck_manager is None:
            return

        for deck_controller in gl.deck_manager.deck_controller:
            # A closed/disabled deck must only be skipped -- this used to
            # `return`, aborting auto page switching for every remaining
            # deck as soon as one disabled deck's page regex matched.
            if deck_controller is None or not deck_controller.deck.is_open():
                continue

            try:
                self._apply_auto_change(deck_controller, window)
            except Exception:
                # One deck failing mid-switch (e.g. torn down concurrently:
                # close() flips is_open() after the check above already
                # passed) must not abort auto-switching for the remaining
                # decks -- and since the watcher threads wrap their loops in
                # @log.catch, an exception escaping here would kill
                # auto-switching entirely until restart.
                log.opt(exception=True).warning(
                    "Auto page switch failed for one deck; continuing with the others"
                )

    def _apply_auto_change(self, deck_controller, window: Window) -> None:
        """Applies the auto-change page rules to a single deck for the given
        foreground window. May raise if the deck is torn down mid-call; the
        caller isolates that per deck."""
        page_manager = gl.page_manager
        if page_manager is None:
            return

        if deck_controller.active_page is None:
            # A deck that is still starting up or being hotplugged has no
            # page yet: there is nothing to compare against or restore,
            # so skip it instead of dereferencing active_page.json_path.
            return

        found_page = False
        for page_path in page_manager.get_pages():
            info = page_manager.get_auto_change_settings(page_path)
            wm_regex = info.get("wm-class")
            title_regex = info.get("title")
            enabled = info.get("enable", False)
            decks = info.get("decks", [])
            if not enabled:
                continue

            if self.get_is_window_matching(window, wm_regex, title_regex):
                if deck_controller.serial_number() not in decks:
                    continue

                if deck_controller.active_page.json_path != page_path:
                    log.debug(f"Auto changing page: {page_path} on deck {deck_controller.deck.get_serial_number()}")
                    page = page_manager.get_page(page_path, deck_controller)
                    if not deck_controller.page_auto_loaded:
                        deck_controller.last_manual_loaded_page_path = deck_controller.active_page.json_path
                    deck_controller.load_page(page)
                deck_controller.page_auto_loaded = True
                found_page = True
                break

        if not found_page:
            if not hasattr(deck_controller, "page_auto_loaded"):
                return

            if deck_controller.page_auto_loaded:
                active_page_change_info = page_manager.get_auto_change_settings(deck_controller.active_page.json_path)
                if active_page_change_info.get("stay-on-page", True):
                    return
                deck_controller.page_auto_loaded = False
                if deck_controller.last_manual_loaded_page_path is None:
                    return
                page = page_manager.get_page(deck_controller.last_manual_loaded_page_path, deck_controller)
                deck_controller.load_page(page, allow_reload=False)
