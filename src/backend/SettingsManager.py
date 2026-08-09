"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
# Import Python modules
import os, json, copy
from loguru import logger as log
from functools import lru_cache

# Import own modules
import globals as gl
from src.backend.atomic_json import atomic_write_json, prune_corrupt_sidecars, quarantine_corrupt_file


def _fallback_font() -> str:
    # Resolved lazily: gl.fallback_font runs a system font scan on first
    # access (globals.py __getattr__), so it must not be evaluated while
    # this module is being imported.
    return gl.fallback_font


# Every app-settings default, defined exactly once. Callers read through
# AppSettings instead of repeating `.get(section, {}).get(key, default)`.
DEFAULTS: dict[str, dict] = {
    "general": {
        "hold-time": 0.5,
        "rolling-labels": True,
        "app-launches": 0,
        "show-donate-window": True,
        "default-font": {},
    },
    "ui": {
        "tray-icon": True,
        "allow-white-mode": False,
        "show-notifications": True,
        "auto-open-action-config": True,
    },
    "key-grid": {
        "emulate-at-double-click": True,
    },
    "warnings": {
        "enable-fps-warnings": True,
    },
    "system": {
        # Tri-state: None means "never asked", which is what makes
        # mainWindow.on_close raise the KeepRunningDialog.
        "keep-running": None,
        "autostart": True,
        "lock-on-lock-screen": True,
    },
    "performance": {
        "n-cached-pages": 3,
        "cache-videos": True,
        # Quiescence gating. "screensaver" is today's behavior
        # exactly (the deck screensaver's own transition already releases the
        # underlying page's media, so nothing extra engages); "system-idle"
        # also pauses deck animations while the session is idle or locked.
        "animation-pause-mode": "screensaver",
        "animation-idle-minutes": 5,
    },
    "store": {
        "auto-update": True,
        "responsibility-notes-agreed": False,
        "enable-custom-stores": False,
        "enable-custom-plugins": False,
        "custom-stores": [],
        "custom-plugins": [],
    },
    "dev": {
        "n-fake-decks": 0,
        "n-remote-decks": 0,
    },
}

# general.default-font subkeys. These use `or` fallback semantics (a falsy
# stored value falls back too), unlike the section defaults above.
FONT_DEFAULTS: dict = {
    "font-family": _fallback_font,
    "font-size": 15,
    "font-weight": 400,
    "font-style": "normal",
    "font-color": (255, 255, 255, 255),
    # 255, not 1: this feeds color_values_to_gdk (0-255 on all four channels)
    # and the render fallback is (0,0,0,255) -- the old value 1 only looked
    # opaque because an earlier clamp rounded any alpha >=1 up.
    "outline-color": (0, 0, 0, 255),
    "outline-width": 2,
}


class AppSettings:
    """Typed accessor over an app-settings dict.

    Wraps *any* settings mapping -- the shared cached dict returned by
    ``SettingsManager.get_app_settings()`` or the Settings dialog's own
    snapshot -- and reads/writes straight through it. Nothing is copied, so
    existing raw writers stay valid and the dialog keeps its batch-save
    semantics.
    """

    def __init__(self, data: dict):
        self.data: dict = data

    def get(self, section: str, key: str):
        try:
            return self.data[section][key]
        except (KeyError, TypeError):
            default = DEFAULTS[section][key]
            if isinstance(default, (dict, list)):
                # Never hand out the table's own container.
                return copy.deepcopy(default)
            return default

    def set(self, section: str, key: str, value) -> None:
        # The same typo tripwire get() has: DEFAULTS is the schema, and a
        # misspelled key would otherwise be written silently and never read
        # back.
        if key not in DEFAULTS[section]:
            raise KeyError(f"{section}.{key} is not in the app-settings DEFAULTS table")
        self.data.setdefault(section, {})
        self.data[section][key] = value

    def save(self) -> None:
        gl.settings_manager.save_app_settings(self.data)

    # -- general -------------------------------------------------------
    @property
    def hold_time(self) -> float:
        return self.get("general", "hold-time")

    @hold_time.setter
    def hold_time(self, value: float) -> None:
        self.set("general", "hold-time", value)

    @property
    def rolling_labels(self) -> bool:
        return self.get("general", "rolling-labels")

    @rolling_labels.setter
    def rolling_labels(self, value: bool) -> None:
        self.set("general", "rolling-labels", value)

    @property
    def app_launches(self) -> int:
        return self.get("general", "app-launches")

    @app_launches.setter
    def app_launches(self, value: int) -> None:
        self.set("general", "app-launches", value)

    @property
    def show_donate_window(self) -> bool:
        return self.get("general", "show-donate-window")

    @show_donate_window.setter
    def show_donate_window(self, value: bool) -> None:
        self.set("general", "show-donate-window", value)

    @property
    def default_font(self) -> dict:
        return self.get("general", "default-font")

    @default_font.setter
    def default_font(self, value: dict) -> None:
        self.set("general", "default-font", value)

    def font_default(self, key: str):
        """A general.default-font subkey, with its `or` fallback applied."""
        default = FONT_DEFAULTS[key]
        if callable(default):
            default = default()
        return self.default_font.get(key) or default

    # -- ui ------------------------------------------------------------
    @property
    def tray_icon(self) -> bool:
        return self.get("ui", "tray-icon")

    @tray_icon.setter
    def tray_icon(self, value: bool) -> None:
        self.set("ui", "tray-icon", value)

    @property
    def allow_white_mode(self) -> bool:
        return self.get("ui", "allow-white-mode")

    @allow_white_mode.setter
    def allow_white_mode(self, value: bool) -> None:
        self.set("ui", "allow-white-mode", value)

    @property
    def show_notifications(self) -> bool:
        return self.get("ui", "show-notifications")

    @show_notifications.setter
    def show_notifications(self, value: bool) -> None:
        self.set("ui", "show-notifications", value)

    @property
    def auto_open_action_config(self) -> bool:
        return self.get("ui", "auto-open-action-config")

    @auto_open_action_config.setter
    def auto_open_action_config(self, value: bool) -> None:
        self.set("ui", "auto-open-action-config", value)

    # -- key-grid ------------------------------------------------------
    @property
    def emulate_at_double_click(self) -> bool:
        return self.get("key-grid", "emulate-at-double-click")

    @emulate_at_double_click.setter
    def emulate_at_double_click(self, value: bool) -> None:
        self.set("key-grid", "emulate-at-double-click", value)

    # -- warnings ------------------------------------------------------
    @property
    def enable_fps_warnings(self) -> bool:
        return self.get("warnings", "enable-fps-warnings")

    @enable_fps_warnings.setter
    def enable_fps_warnings(self, value: bool) -> None:
        self.set("warnings", "enable-fps-warnings", value)

    # -- system --------------------------------------------------------
    @property
    def keep_running(self) -> bool | None:
        return self.get("system", "keep-running")

    @keep_running.setter
    def keep_running(self, value: bool | None) -> None:
        self.set("system", "keep-running", value)

    @property
    def autostart(self) -> bool:
        return self.get("system", "autostart")

    @autostart.setter
    def autostart(self, value: bool) -> None:
        self.set("system", "autostart", value)

    @property
    def lock_on_lock_screen(self) -> bool:
        return self.get("system", "lock-on-lock-screen")

    @lock_on_lock_screen.setter
    def lock_on_lock_screen(self, value: bool) -> None:
        self.set("system", "lock-on-lock-screen", value)

    # -- performance ---------------------------------------------------
    @property
    def n_cached_pages(self) -> int:
        return self.get("performance", "n-cached-pages")

    @n_cached_pages.setter
    def n_cached_pages(self, value: int) -> None:
        self.set("performance", "n-cached-pages", value)

    @property
    def cache_videos(self) -> bool:
        return self.get("performance", "cache-videos")

    @cache_videos.setter
    def cache_videos(self, value: bool) -> None:
        self.set("performance", "cache-videos", value)

    @property
    def animation_pause_mode(self) -> str:
        return self.get("performance", "animation-pause-mode")

    @animation_pause_mode.setter
    def animation_pause_mode(self, value: str) -> None:
        self.set("performance", "animation-pause-mode", value)

    @property
    def animation_idle_minutes(self) -> int:
        return self.get("performance", "animation-idle-minutes")

    @animation_idle_minutes.setter
    def animation_idle_minutes(self, value: int) -> None:
        self.set("performance", "animation-idle-minutes", value)

    # -- store ---------------------------------------------------------
    @property
    def auto_update(self) -> bool:
        return self.get("store", "auto-update")

    @auto_update.setter
    def auto_update(self, value: bool) -> None:
        self.set("store", "auto-update", value)

    @property
    def responsibility_notes_agreed(self) -> bool:
        return self.get("store", "responsibility-notes-agreed")

    @responsibility_notes_agreed.setter
    def responsibility_notes_agreed(self, value: bool) -> None:
        self.set("store", "responsibility-notes-agreed", value)

    @property
    def enable_custom_stores(self) -> bool:
        return self.get("store", "enable-custom-stores")

    @enable_custom_stores.setter
    def enable_custom_stores(self, value: bool) -> None:
        self.set("store", "enable-custom-stores", value)

    @property
    def enable_custom_plugins(self) -> bool:
        return self.get("store", "enable-custom-plugins")

    @enable_custom_plugins.setter
    def enable_custom_plugins(self, value: bool) -> None:
        self.set("store", "enable-custom-plugins", value)

    @property
    def custom_stores(self) -> list:
        return self.get("store", "custom-stores")

    @custom_stores.setter
    def custom_stores(self, value: list) -> None:
        self.set("store", "custom-stores", value)

    @property
    def custom_plugins(self) -> list:
        return self.get("store", "custom-plugins")

    @custom_plugins.setter
    def custom_plugins(self, value: list) -> None:
        self.set("store", "custom-plugins", value)

    # -- dev -----------------------------------------------------------
    @property
    def n_fake_decks(self) -> int:
        return self.get("dev", "n-fake-decks")

    @n_fake_decks.setter
    def n_fake_decks(self, value: int) -> None:
        self.set("dev", "n-fake-decks", value)

    @property
    def n_remote_decks(self) -> int:
        return self.get("dev", "n-remote-decks")

    @n_remote_decks.setter
    def n_remote_decks(self, value: int) -> None:
        self.set("dev", "n-remote-decks", value)


class SettingsManager:
    def __init__(self):
        self.font_defaults: dict = {} # Used by the LabelManager to get the default font settings
        self.load_font_defaults()

    def invalidate_all_caches(self):
        for name in dir(self):
            attr = getattr(self, name)
            if hasattr(attr, "cache_clear"):
                attr.cache_clear()

    @staticmethod
    def load_settings_from_file(file_path: str) -> dict:
        data, _corrupt = SettingsManager.load_settings_reporting_corruption(file_path)
        return data

    @staticmethod
    def load_settings_reporting_corruption(file_path: str) -> tuple[dict, bool]:
        """Load JSON, returning ``(data, corrupt)``.

        ``corrupt`` is True *only* when the file was present but unparseable
        -- NOT for a legitimately empty ``{}`` and NOT for a missing file.
        Callers that hold a backup (get_page_data) heal on this flag rather
        than on the quarantine side-effect having removed the primary: a
        corrupt file is corrupt whether or not it could be moved aside, so
        the recovery must not depend on the rename succeeding.
        """
        if not os.path.exists(file_path):
            return {}, False
        try:
            with open(file_path) as f:
                return json.load(f), False
        except FileNotFoundError:
            # Raced a concurrent quarantine between the exists() check and
            # the open.
            return {}, False
        except ValueError as e:
            # ValueError, not JSONDecodeError: garbage bytes raise
            # UnicodeDecodeError while decoding, which is a ValueError but not
            # a JSON error -- it used to escape this handler and propagate out
            # of every page/settings load. JSONDecodeError is itself a
            # ValueError subclass, so one clause covers both.
            # Quarantine instead of leaving the corrupt file in place: the
            # caller gets {} either way, but the next save would overwrite
            # the only remaining copy of the user's data. Renamed aside it
            # stays recoverable (a prior .corrupt is never clobbered), and
            # page loads heal from their backup (get_page_data) off the
            # returned corrupt=True flag regardless of whether this rename
            # succeeded.
            moved, dest = quarantine_corrupt_file(file_path)
            if moved:
                log.error(f"Invalid json in {file_path}: {e} -- preserved at {dest}, loading empty")
                # Bounded retention, scoped to the file that just gained a
                # sidecar -- never a startup-wide sweep. Covers pages
                # and deck/app settings alike: PageManagerBackend routes its
                # corrupt-read handling through this loader.
                for pruned in prune_corrupt_sidecars(file_path, protect=dest):
                    log.info(f"Pruned old quarantined copy {pruned}")
            else:
                log.error(
                    f"Invalid json in {file_path}: {e} -- could NOT move it aside "
                    f"(left in place); callers with a backup will heal, loading empty"
                )
            return {}, True

    @staticmethod
    def save_settings_to_file(file_path: str, settings: dict) -> None:
        # Atomic write (tmp file + fsync + os.replace) so an interrupted
        # write can't truncate the settings file; also creates parent dirs.
        atomic_write_json(file_path, settings)

        gl.settings_manager.invalidate_all_caches()

    @lru_cache
    def _load_deck_settings_cached(self, deck_serial_number: str) -> dict:
        path = os.path.join(gl.DATA_PATH, "settings", "decks", f"{deck_serial_number}.json")
        return self.load_settings_from_file(path)

    def get_deck_settings(self, deck_serial_number: str) -> dict:
        """
        Retrieves the deck settings for a given deck serial number.
        Cached (invalidated on save) and deep-copied per call, so callers can
        still freely mutate the result without touching the cache.

        Args:
            deck_serial_number (str): The serial number of the deck.

        Returns:
            dict: The deck settings loaded from the file.
        """
        return copy.deepcopy(self._load_deck_settings_cached(deck_serial_number))
    
    def save_deck_settings(self, deck_serial_number: str, settings: dict) -> None:
        """
        Saves the settings for a deck.
        This is just a wrapper around save_settings_to_file()

        Args:
            deck_serial_number (str): The serial number of the deck.
            settings (dict): The settings to save.

        Returns:
            None
        """
        path = os.path.join(gl.DATA_PATH, "settings", "decks", f"{deck_serial_number}.json")
        self.save_settings_to_file(path, settings)

        self.invalidate_all_caches()

    @lru_cache
    def get_app_settings(self) -> dict:
        path = os.path.join(gl.DATA_PATH, "settings", "settings.json")
        settings =  self.load_settings_from_file(path)
        if settings is None:
            settings = {}
            self.save_settings_to_file(path, settings)
        return settings
    
    def app(self) -> AppSettings:
        """Typed view onto the shared app-settings dict."""
        return AppSettings(self.get_app_settings())

    def save_app_settings(self, settings: dict) -> None:
        path = os.path.join(gl.DATA_PATH, "settings", "settings.json")
        self.save_settings_to_file(path, settings)

        # Invalidate get_app_settings cache
        self.get_app_settings.cache_clear()

    def get_static_settings(self) -> dict:
        """
        Returns always the same settings, no matter what the data path is set to
        """
        return self.load_settings_from_file(gl.STATIC_SETTINGS_FILE_PATH)
    
    def save_static_settings(self, settings: dict) -> None:
        self.save_settings_to_file(gl.STATIC_SETTINGS_FILE_PATH, settings)

    def load_font_defaults(self) -> None:
        self.font_defaults = self.app().default_font

    def save_font_defaults(self) -> None:
        # Merge into the existing general section -- replacing it wholesale
        # silently destroyed every other general.* setting (hold-time,
        # rolling-labels, app-launches, show-donate-window) whenever a font
        # default was changed.
        app = self.app()
        app.default_font = self.font_defaults
        app.save()
