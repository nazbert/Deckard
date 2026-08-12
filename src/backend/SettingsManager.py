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
# Import own modules
import globals as gl
from src.backend import settings_store

# The app-settings table, its font subtable and its typed view live with the
# surface they describe, next to every other settings surface. Re-exported
# under their long-standing names -- the same objects, not copies -- because
# roughly a hundred call sites and the table's own pin import them from here.
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
        """Load JSON, returning ``(data, corrupt)``.

        ``corrupt`` is True *only* when the file was present but unparseable
        -- NOT for a legitimately empty ``{}`` and NOT for a missing file.
        Callers that hold a backup (get_page_data) heal on this flag rather
        than on the quarantine side-effect having removed the primary: a
        corrupt file is corrupt whether or not it could be moved aside, so
        the recovery must not depend on the rename succeeding.

        A forward, not a second implementation: the read-with-heal lives in
        the settings store, and the ~30 call sites that reach it through this
        name are the reason the name stays.
        """
        return settings_store.get().load_file(file_path)

    @staticmethod
    def save_settings_to_file(file_path: str, settings: dict) -> None:
        # A forward: the store writes atomically (tmp file + fsync +
        # os.replace, so an interrupted write can't truncate the settings
        # file) and drops any cached surface living at that path.
        # Nothing else is swept: this manager holds no cache of its own, and a
        # store surface is dropped by the file the write landed on rather than
        # by a sweep of every cache within reach of a writer.
        settings_store.get().save_file(file_path, settings)

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
        return settings_store.get().read(settings_store.DECK, deck_serial_number)

    def save_deck_settings(self, deck_serial_number: str, settings: dict) -> None:
        """
        Saves the settings for a deck.

        The write is atomic and invalidates this deck's cached copy -- and
        only this deck's: a settings write elsewhere in the tree cannot change
        what is in a deck's file, so it has no business clearing it.

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

        One file read per view: build it once at the top of a load path and
        destructure it, rather than asking per key.
        """
        return settings_store.DeckSettings(
            self.get_deck_settings(deck_serial_number), deck_serial_number
        )

    def deck_view(self, settings: dict) -> settings_store.DeckSettings:
        """The same view over deck settings the caller already read.

        For readers that decide for themselves whether the file is read at all
        -- the deck controller short-circuits to an empty dict once its deck is
        gone, and that decision must stay in front of the read.
        """
        return settings_store.DeckSettings(settings)

    def get_app_settings(self) -> dict:
        """The app settings, as the one dict every reader of them holds.

        Not a copy: the settings dialog's rows, the store pages and the launch
        counter all read this, write into it, and save it back, and they have
        to be looking at the same object for a write to be visible before the
        save. Cached by the store and dropped the moment the file is written,
        so the copy handed out here is never behind the disk.
        """
        return settings_store.get().read(settings_store.APP)

    def app(self) -> AppSettings:
        """Typed view onto the shared app-settings dict."""
        return AppSettings(self.get_app_settings())

    def app_snapshot(self) -> AppSettings:
        """Typed view onto a PRIVATE copy of the app settings, read from disk.

        For an editor that collects several changes against one picture of the
        file and writes the whole picture back at the end -- the settings
        dialog. It must not join the shared dict: its unfinished edits would
        read as settled to everyone else.
        """
        return AppSettings(settings_store.get().read_fresh(settings_store.APP))

    def save_app_settings(self, settings: dict) -> None:
        settings_store.get().write(settings_store.APP, settings)

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
