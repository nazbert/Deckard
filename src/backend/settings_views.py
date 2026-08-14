"""The typed views over the settings surfaces.

A SchemaView is a settings dict read through its schema. An absent key reads
as the schema's default, a write of an unknown key raises, and the storage
stays sparse, so a load path persists no default. DeckSettings, AppSettings
and PluginSettings build on it and add the named accessors and the envelope
handling for one surface each.

Why this module exists

These views belong next to the surfaces they read, in settings_store, because
they describe the same files. They sit in their own module because the store
and its views together grew past a reviewable size, and they split cleanly.
The views depend on the store, on the surface specs and the get() singleton,
and the store does not depend on them at import time. settings_store
re-imports these four names at the foot of its own body, so every external
importer still reaches AppSettings, DeckSettings, PluginSettings and
SchemaView at settings_store. This module is the store's back half, and no
second entry point, so nothing imports it directly.
"""
from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import Any

from loguru import logger as log

from src.backend.settings_store import (
    APP,
    APP_DEFAULTS,
    APP_FONT_DEFAULTS,
    DECK,
    DECK_DEFAULTS,
    PLUGIN,
    PLUGIN_FILE_VERSION,
    get,
)


def _copied(value: Any) -> Any:
    """A value safe to hand out. This copies a container and returns a
    scalar as it is.

    Nothing may receive the schema's own container. The holder of
    font_defaults mutates it in place, and one shared default then corrupts
    every later read of that key.
    """
    return copy.deepcopy(value) if isinstance(value, (dict, list)) else value


class SchemaView:
    """A settings dict read through its schema.

    This wraps a mapping that somebody already read, and copies nothing, so a
    writer that still holds the same dict stays valid. An absent key reads as
    the schema's default, and a read fills nothing in, because a filled key
    becomes a persisted key on the next save (see the module docstring). A
    write of a key the schema does not describe raises, so a misspelled key
    never lands on disk unread.

    A view is one read. Build it once at the top of a load path and
    destructure it, rather than ask the store per key.

    What a read hands back

    A stored value comes back as a copy here, which matches the surfaces whose
    store reads are copies. The caller mutates what it got and saves it back,
    and two callers must not reach each other's half-made edits. An aliasing
    view, with shared=True, hands the stored value back by reference, because
    it reads one object that every holder writes into (see AppSettings). The
    sharing must hold at every level. A view that handed out the top dict by
    reference and a list inside it by copy would take
    view.custom_stores.append(x), and a save would persist nothing.

    A default comes back as a copy either way. Nothing stored exists to alias,
    and a hand-out of the schema's own container lets one holder corrupt every
    later reader of that key.
    """

    def __init__(self, data: dict, schema: Mapping[str, Any], shared: bool = False):
        self.data: dict = data
        self.schema: Mapping[str, Any] = schema
        #: Hand stored values back by reference rather than as copies.
        self.shared: bool = shared

    # Reads

    def get(self, name: str, key: str | None = None) -> Any:
        """One setting, either what sits in storage or the schema's default.

        key names a setting inside the section name. Omit key for a top-level
        setting. A name the schema does not describe raises here, rather than
        read as None for the rest of the run.
        """
        if key is None:
            default = self._top_level(name)
            return self._stored_value(self.data[name]) if name in self.data else _copied(default)
        defaults = self._section_defaults(name)
        if key not in defaults:
            raise KeyError(f"{name}.{key} is not in this schema")
        stored = self._stored_section(name)
        return self._stored_value(stored[key]) if key in stored else _copied(defaults[key])

    def section(self, name: str) -> dict[str, Any]:
        """Section name, with every absent key filled from the schema.

        This is the shape a load path destructures. It returns a copy, on an
        aliasing view as much as on a copying one, because it is a
        destructuring shape and no handle on the settings. A mutation of it
        reaches neither the schema nor the stored settings, and a save of it
        persists the defaults it just filled in. A stored key that the schema
        does not describe stays, because this fills gaps and prunes no file it
        did not write.
        """
        merged = {k: _copied(v) for k, v in self._section_defaults(name).items()}
        merged.update(copy.deepcopy(dict(self._stored_section(name))))
        return merged

    # Writes

    def set(self, name: str, key: str, value: Any) -> None:
        """Store one setting inside section name. This writes nothing else,
        so every key still absent keeps following the schema."""
        if key not in self._section_defaults(name):
            raise KeyError(f"{name}.{key} is not in this schema")
        section = self.data.get(name)
        if not isinstance(section, dict):
            # The section is absent, or a hand edit left a scalar there. No
            # section exists to add to, and the caller chose this value.
            section = {}
            self.data[name] = section
        section[key] = value

    def set_value(self, name: str, value: Any) -> None:
        """Store one top-level setting, which the file holds as a bare
        value."""
        self._top_level(name)
        self.data[name] = value

    # Internals

    def _stored_value(self, value: Any) -> Any:
        """A stored value on its way out. An aliasing view returns it by
        reference, and a copying view returns a copy (see the class
        docstring)."""
        return value if self.shared else _copied(value)

    def _section_defaults(self, name: str) -> Mapping[str, Any]:
        try:
            section = self.schema[name]
        except KeyError:
            raise KeyError(f"{name} is not in this schema") from None
        if not isinstance(section, Mapping):
            raise KeyError(f"{name} is a top-level setting in this schema, not a section")
        return section

    def _top_level(self, name: str) -> Any:
        try:
            default = self.schema[name]
        except KeyError:
            raise KeyError(f"{name} is not in this schema") from None
        if isinstance(default, Mapping):
            raise KeyError(f"{name} is a section in this schema: name a key inside it")
        return default

    def _stored_section(self, name: str) -> Mapping[str, Any]:
        stored = self.data.get(name)
        # A hand-edited or half-written file can leave a scalar where a
        # section belongs. Answer from the schema then.
        return stored if isinstance(stored, Mapping) else {}


class DeckSettings(SchemaView):
    """One deck's settings, read through DECK_DEFAULTS.

    The settings-manager facade builds it. deck(serial) reads the file and can
    save back to it. deck_view(settings) wraps settings a caller already holds
    and cannot save.

    This is a copying view. The deck surface hands out a deep copy per store
    read, so the dict under this view already belongs to the caller, and this
    view copies what it hands back for the same reason. One caller's edits
    stay its own until it saves them.
    """

    def __init__(self, data: dict, serial: str | None = None):
        super().__init__(data, DECK_DEFAULTS)
        self.serial: str | None = serial

    def save(self) -> None:
        """Persist what a caller set through this view. This is the write
        that save_deck_settings of the settings manager performs. It writes
        atomically and drops this deck's cached copy."""
        if self.serial is None:
            raise ValueError(
                "this deck-settings view wraps a dict, not a deck: it has no file to save to"
            )
        get().write(DECK, self.data, self.serial)


class AppSettings(SchemaView):
    """The app's own settings, read through APP_DEFAULTS.

    This wraps any app-settings mapping and copies nothing. It takes the
    shared dict that the settings manager hands out, or the settings dialog's
    private snapshot. The mapping it wraps decides who sees a write before the
    save, and the caller makes that decision.

    This view always aliases. A stored list or dict comes back by reference,
    so settings.custom_stores.append(entry) plus a save persists the entry,
    and the font-defaults dict that the label engine holds is the one inside
    the settings rather than a snapshot. A copy would accept both calls and
    keep neither. An absent key still reads as a copy of the table's default,
    because nothing stored exists to alias, and the table must survive the
    first holder that mutates what it read.

    The named accessors, one per setting, replace raw keys, because one key
    carried a different inline default in every module that read it.
    """

    def __init__(self, data: dict):
        super().__init__(data, APP_DEFAULTS, shared=True)

    def save(self) -> None:
        """Persist the whole wrapped dict atomically, and drop the shared
        copy, so the next reader loads what this write left."""
        get().write(APP, self.data)

    # General settings
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

    def font_default(self, key: str) -> Any:
        """A general.default-font subkey. A falsy stored value falls back to
        the default too."""
        default = APP_FONT_DEFAULTS[key]
        if callable(default):
            default = default()
        return self.default_font.get(key) or default

    # UI settings
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

    # Key-grid settings
    @property
    def emulate_at_double_click(self) -> bool:
        return self.get("key-grid", "emulate-at-double-click")

    @emulate_at_double_click.setter
    def emulate_at_double_click(self, value: bool) -> None:
        self.set("key-grid", "emulate-at-double-click", value)

    # Warnings settings
    @property
    def enable_fps_warnings(self) -> bool:
        return self.get("warnings", "enable-fps-warnings")

    @enable_fps_warnings.setter
    def enable_fps_warnings(self, value: bool) -> None:
        self.set("warnings", "enable-fps-warnings", value)

    # System settings
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

    # Performance settings
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

    # Store settings
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

    # Dev settings
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


class PluginSettings:
    """One plugin's settings file, which is the app's envelope around the
    plugin's keys.

    A plugin owns everything inside "settings". The app owns the file it sits
    in, which means where it lives, the file-version envelope, and what
    happens when it does not read. The asset manager keeps a second top-level
    key, "assets", in that file, so the whole document is reachable here.

    Here an unreadable file reads as empty

    Every other surface lets an OSError out, because the content is unknown
    and an empty answer invites a write that destroys it. This class catches
    it. These reads run inside a plugin __init__ and inside the plugin
    settings dialog, where a raise costs the user the whole plugin, or the
    dialog, over a file they may never have written to. The plugin starts with
    no settings and logs that loudly.

    The loader still quarantines a decode failure. The bytes exist and do not
    parse, and the next save overwrites the only copy of a configuration
    nobody can retype. A plugin source file, such as manifest.json or
    about.json, gets neither treatment. The app never writes one, so nothing
    overwrites a corrupt one, and a rename would change a developer's working
    tree.

    This class takes no lock. PluginBase serializes get_settings and
    set_settings on its per-plugin lock and calls in here while it holds that
    lock, so the order is fixed. The plugin's lock sits outside and the
    store's leaf cache lock sits inside, and never the reverse.
    SettingsStore.edit here would take them in that order.
    """

    def __init__(self, settings_path: str) -> None:
        self.path: str = settings_path

    def document(self) -> dict[str, Any] | None:
        """The file's entire content, or None when nothing is there to read.

        None covers every way this file yields no document. It is absent, this
        read quarantined it, it does not read, or it holds no JSON object.
        Every caller answers all four the same way and starts empty. None of
        the four is an empty document, which is a real file that the app
        migrates and rewrites.
        """
        try:
            content, corrupt = get().read_reporting_corruption(PLUGIN, self.path)
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not read plugin settings file {self.path} -- treating it as "
                f"empty; whatever is saved next replaces it"
            )
            return None
        if corrupt:
            return None
        if not content and not os.path.isfile(self.path):
            # The file is absent, or another reader moved it aside during
            # this read. Check after the read and not before, so a file
            # quarantined under this read answers "nothing there" rather than
            # "an empty document", which a migration would write back.
            return None
        if not isinstance(content, dict):
            log.error(
                f"Plugin settings file {self.path} does not contain a JSON object "
                f"-- treating it as empty"
            )
            return None
        return content

    def save_document(self, document: dict[str, Any]) -> None:
        """Replace the file with document, atomically."""
        get().write(PLUGIN, document, self.path)

    def read(self) -> dict[str, Any]:
        """The plugin's settings, unwrapped, migrating a pre-envelope file once."""
        document = self.document()
        if document is None:
            return {}
        if document.get("file-version") == PLUGIN_FILE_VERSION:
            return document.get("settings", {})
        # This file predates the envelope, so the whole file holds the
        # settings. Write it back wrapped, and return what it held either way.
        # A migration that cannot write must not also cost the plugin its
        # settings for this run.
        try:
            self.save_document({"file-version": PLUGIN_FILE_VERSION, "settings": document})
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not migrate plugin settings file {self.path} to the current format"
            )
        return document

    def write(self, settings: Any) -> None:
        """Store settings as the plugin's own keys, atomically."""
        document = self.document()
        if document is None or document.get("file-version") != PLUGIN_FILE_VERSION:
            # Nothing usable exists, or a file predates the envelope and its
            # own keys hold the settings this write replaces. This write
            # becomes the envelope and keeps none of that. On a wrapped file
            # it keeps what the app stores beside the settings, the asset
            # overrides.
            document = {"file-version": PLUGIN_FILE_VERSION}
        document["settings"] = settings
        self.save_document(document)
