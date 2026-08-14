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
from src.backend import settings_store

# The app-settings table, its font subtable and its typed view live with the
# surface they describe, next to every other settings surface. These names
# re-export the same objects, because about a hundred call sites and the
# table's own pin import them from here.
from src.backend.settings_store import (  # noqa: F401
    APP_DEFAULTS as DEFAULTS,
    APP_FONT_DEFAULTS as FONT_DEFAULTS,
    AppSettings,
)


class SettingsManager:
    def __init__(self):
        self.font_defaults: dict = {} # Used by the LabelManager to get the default font settings
        self.load_font_defaults()

    @staticmethod
    def load_settings_from_file(file_path: str) -> dict:
        data, _corrupt = SettingsManager.load_settings_reporting_corruption(file_path)
        return data

    @staticmethod
    def load_settings_reporting_corruption(file_path: str) -> tuple[dict, bool]:
        """Load JSON and return (data, corrupt).

        corrupt is True only for a file that exists and does not parse. An
        empty {} and a missing file both read as not corrupt. A caller that
        holds a backup (get_page_data) heals on this flag, not on the
        quarantine rename, because a corrupt file stays corrupt whether or not
        the rename succeeds.

        This forwards to the settings store, which owns the read-with-heal.
        About 30 call sites reach it through this name.
        """
        return settings_store.get().load_file(file_path)

    @staticmethod
    def save_settings_to_file(file_path: str, settings: dict) -> None:
        # This forwards to the store, which writes atomically (a temp file, an
        # fsync and an os.replace, so an interrupted write cannot truncate the
        # settings file) and drops the cached surface at that path. Nothing
        # else drops, because this manager holds no cache and the store drops
        # by the file the write landed on.
        settings_store.get().save_file(file_path, settings)

    def get_deck_settings(self, deck_serial_number: str) -> dict:
        """
        Retrieves the deck settings for a given deck serial number.
        The store caches the settings, drops the cache on a save, and
        deep-copies per call, so a caller can mutate the result freely.

        Args:
            deck_serial_number (str): The serial number of the deck.

        Returns:
            dict: The deck settings loaded from the file.
        """
        return settings_store.get().read(settings_store.DECK, deck_serial_number)

    def save_deck_settings(self, deck_serial_number: str, settings: dict) -> None:
        """
        Saves the settings for a deck.

        The write is atomic and drops this deck's cached copy only. A settings
        write elsewhere in the tree cannot change a deck's file, so it must
        not clear that deck's cache.

        Args:
            deck_serial_number (str): The serial number of the deck.
            settings (dict): The settings to save.

        Returns:
            None
        """
        settings_store.get().write(settings_store.DECK, settings, deck_serial_number)

    def deck(self, deck_serial_number: str) -> settings_store.DeckSettings:
        """Typed view onto one deck's settings, with the schema's defaults
        applied at read and unknown keys refused at write.

        One file read per view. Build it once at the top of a load path and
        destructure it, rather than ask per key.
        """
        return settings_store.DeckSettings(
            self.get_deck_settings(deck_serial_number), deck_serial_number
        )

    def deck_view(self, settings: dict) -> settings_store.DeckSettings:
        """The same view over deck settings the caller already read.

        For a reader that decides whether to read the file at all. The deck
        controller returns an empty dict once its deck is gone, and that
        decision must stay in front of the read.
        """
        return settings_store.DeckSettings(settings)

    def get_app_settings(self) -> dict:
        """The app settings, as the one dict every reader of them holds.

        This hands out the shared dict. The settings dialog rows, the store
        pages and the launch counter read it, write into it and save it back,
        and they must see one object for a write to show before the save. The
        store caches it and drops the cache on a write, so this dict never
        falls behind the disk.
        """
        return settings_store.get().read(settings_store.APP)

    def app(self) -> AppSettings:
        """Typed view onto the shared app-settings dict."""
        return AppSettings(self.get_app_settings())

    def app_snapshot(self) -> AppSettings:
        """Typed view onto a private copy of the app settings, read from disk.

        For an editor that collects several changes against one picture of the
        file and writes the whole picture back at the end, such as the
        settings dialog. It must stay off the shared dict, because every other
        reader would take its unfinished edits as settled.
        """
        return AppSettings(settings_store.get().read_fresh(settings_store.APP))

    def save_app_settings(self, settings: dict) -> None:
        settings_store.get().write(settings_store.APP, settings)

    def get_static_settings(self) -> dict:
        """
        Returns always the same settings, no matter what the data path is set to.

        The store's STATIC surface reads the fixed file that holds the
        data-path override. One reader skips this method, the bootstrap read
        in globals.py, which runs before this module imports and which defines
        the data path.
        """
        return settings_store.get().read(settings_store.STATIC)

    def save_static_settings(self, settings: dict) -> None:
        settings_store.get().write(settings_store.STATIC, settings)

    def load_font_defaults(self) -> None:
        self.font_defaults = self.app().default_font

    def save_font_defaults(self) -> None:
        # Merge into the existing general section. A wholesale replacement
        # destroys every other general setting (hold-time, rolling-labels,
        # app-launches, show-donate-window) on a font default change.
        app = self.app()
        app.default_font = self.font_defaults
        app.save()
