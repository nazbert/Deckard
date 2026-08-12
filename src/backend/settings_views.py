"""
The typed views over the settings surfaces.

A ``SchemaView`` is a settings dict read through its schema: absent keys read
as the schema's default, unknown keys refused at write, storage kept sparse so
a load path never persists a default. ``DeckSettings``, ``AppSettings`` and
``PluginSettings`` are the surface-specific ones -- the named accessors, the
envelope handling -- built on it.

WHY THIS IS ITS OWN MODULE

These live next to the surfaces they read, in ``settings_store``, because they
describe the same files. They are HERE, in a module of their own, because the
store plus its views had grown past what one module stays reviewable at, and
the views split cleanly: they depend on the store (the surface specs and the
``get()`` singleton) and the store does not depend on them at import time.
``settings_store`` re-imports these four names at the foot of its own body, so
every external importer still reaches ``AppSettings``, ``DeckSettings``,
``PluginSettings`` and ``SchemaView`` at ``settings_store`` exactly as before --
this module is the store's back half, not a second entry point, and nothing
imports it directly.
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
    """A value safe to hand out: containers are copied, scalars are not.

    Nothing may ever receive the schema's own container -- ``font_defaults``
    is mutated in place by its holder, and one shared default would poison
    every later read of it.
    """
    return copy.deepcopy(value) if isinstance(value, (dict, list)) else value


class SchemaView:
    """A settings dict read through its schema.

    Wraps a mapping somebody already read -- it copies nothing, so a writer
    that still holds the same dict stays valid. Absent keys read as the
    schema's default; reading never fills one in, because a filled key is a
    persisted key the next save would write (see the module docstring).
    Writing a key the schema does not describe raises, because a misspelled
    one would otherwise be written silently and never read back.

    A view is the unit of "one read": build it once at the top of a load path
    and destructure it, rather than asking the store per key.

    WHAT A READ HANDS BACK

    A stored value is handed back as a COPY here, matching the surfaces whose
    store reads are copies: the caller mutates what it got and saves it back,
    and two callers must not be able to reach each other's half-made edits.
    An ALIASING view (``shared=True``) hands the stored value back by
    reference instead, because the surface it reads is one object every holder
    writes into -- see ``AppSettings``. The sharing has to hold at every level
    or it is not sharing: a view that handed out the top dict by reference and
    a list inside it by copy would accept ``view.custom_stores.append(x)``
    followed by a save and persist nothing.

    A DEFAULT is copied either way. There is nothing stored to alias, and
    handing out the schema's own container is how one holder mutating what it
    read poisons every later reader of that key.
    """

    def __init__(self, data: dict, schema: Mapping[str, Any], shared: bool = False):
        self.data: dict = data
        self.schema: Mapping[str, Any] = schema
        #: Hand stored values back by reference rather than as copies.
        self.shared: bool = shared

    # -- reads --------------------------------------------------------- #

    def get(self, name: str, key: str | None = None) -> Any:
        """One setting: what is stored, or the schema's default for it.

        ``key`` names a setting inside the section ``name``; omit it for a
        top-level one. A name the schema does not describe raises here rather
        than reading as None for the rest of the program's life.
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
        """Section ``name`` with every absent key filled from the schema.

        The shape a load path destructures. A copy, and deliberately so --
        on an aliasing view as much as a copying one, because this is the
        destructuring shape and not a handle on the settings: mutating it
        reaches neither the schema nor the stored settings, and saving it back
        would persist exactly the defaults it just filled in. Keys stored but
        not described are kept -- this fills gaps, it does not prune a file it
        did not write.
        """
        merged = {k: _copied(v) for k, v in self._section_defaults(name).items()}
        merged.update(copy.deepcopy(dict(self._stored_section(name))))
        return merged

    # -- writes -------------------------------------------------------- #

    def set(self, name: str, key: str, value: Any) -> None:
        """Store one setting inside section ``name``. Sparse: nothing else is
        written, so every key still absent keeps following the schema."""
        if key not in self._section_defaults(name):
            raise KeyError(f"{name}.{key} is not in this schema")
        section = self.data.get(name)
        if not isinstance(section, dict):
            # Absent, or a scalar left by a hand edit: either way there is no
            # section to add to, and the value being written is the one the
            # caller has now chosen.
            section = {}
            self.data[name] = section
        section[key] = value

    def set_value(self, name: str, value: Any) -> None:
        """Store one top-level setting -- the ones held as a bare value."""
        self._top_level(name)
        self.data[name] = value

    # -- internals ----------------------------------------------------- #

    def _stored_value(self, value: Any) -> Any:
        """A stored value on its way out: by reference on an aliasing view, as
        a copy on a copying one (see the class docstring)."""
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
        # section belongs. The schema's answer is the right one then.
        return stored if isinstance(stored, Mapping) else {}


class DeckSettings(SchemaView):
    """One deck's settings, read through ``DECK_DEFAULTS``.

    Built by the settings-manager facade: ``deck(serial)`` reads the file and
    can save back to it, ``deck_view(settings)`` wraps settings a caller
    already has and cannot.

    A COPYING view: the deck surface hands out a deep copy per store read, so
    the dict underneath this one is already the caller's alone, and what this
    hands back out of it is copied for the same reason -- one caller's edits
    are its own until it saves them.
    """

    def __init__(self, data: dict, serial: str | None = None):
        super().__init__(data, DECK_DEFAULTS)
        self.serial: str | None = serial

    def save(self) -> None:
        """Persist what was set through this view -- the same write the
        settings manager's ``save_deck_settings`` performs, atomically and
        invalidating this deck's cached copy."""
        if self.serial is None:
            raise ValueError(
                "this deck-settings view wraps a dict, not a deck: it has no file to save to"
            )
        get().write(DECK, self.data, self.serial)


class AppSettings(SchemaView):
    """The app's own settings, read through ``APP_DEFAULTS``.

    Wraps *any* app-settings mapping and copies nothing: the shared dict the
    settings manager hands out, or the settings dialog's private snapshot.
    Which one it wraps decides who sees a write before it is saved, and that
    is the caller's decision to make, not this class's.

    An ALIASING view, always -- a stored list or dict comes back by reference,
    so ``settings.custom_stores.append(entry)`` followed by a save persists
    the entry, and the font-defaults dict the label engine holds is the one
    inside the settings rather than a snapshot of it. Copying there would
    accept both and silently keep neither. Absent keys still read as copies of
    the table's defaults: there is nothing stored to alias, and the table must
    survive the first holder that mutates what it read.

    Named accessors rather than raw keys, one per setting, because the same
    key used to carry a different inline default in every module that read it.
    """

    def __init__(self, data: dict):
        super().__init__(data, APP_DEFAULTS, shared=True)

    def save(self) -> None:
        """Persist the whole wrapped dict, atomically, dropping the shared
        copy so the next reader loads what was just written."""
        get().write(APP, self.data)

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

    def font_default(self, key: str) -> Any:
        """A general.default-font subkey, with its `or` fallback applied."""
        default = APP_FONT_DEFAULTS[key]
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


class PluginSettings:
    """One plugin's settings file: the app's envelope around the plugin's keys.

    A plugin owns everything inside ``settings``; the app owns the file it sits
    in -- where it lives, the ``file-version`` envelope, and what happens when
    it cannot be read. The asset manager keeps a second top-level key
    (``assets``) there, which is why the whole document is reachable here.

    UNREADABLE IS NOT EMPTY ANYWHERE ELSE -- IT IS HERE

    Every other surface lets an OSError out, because the content is unknown and
    pretending it is empty invites a write that destroys it. This one swallows
    it, deliberately and as it always has: these reads run inside plugin
    ``__init__`` and inside the plugin settings dialog, where raising costs the
    user the whole plugin -- or the dialog -- over a file they may never have
    put anything in. The plugin starts with none and says so loudly instead.

    A DECODE failure IS quarantined by the loader: the bytes are there and
    unusable, and the next save would otherwise overwrite the only copy of a
    configuration nobody can retype. Plugin SOURCE files (manifest.json,
    about.json) are neither -- the app never writes them, so nothing overwrites
    a corrupt one, and moving one would mutate a developer's working tree.

    LOCKING: none of its own. ``PluginBase`` serializes get/set_settings on its
    per-plugin lock and calls in here holding it, so the tree has exactly one
    order -- the plugin's lock outside, the store's leaf cache lock inside,
    never the reverse; ``SettingsStore.edit`` here would take it in that order.
    """

    def __init__(self, settings_path: str) -> None:
        self.path: str = settings_path

    def document(self) -> dict[str, Any] | None:
        """The file's entire content, or None when there is nothing to read.

        None is every way this file can fail to yield a document: absent,
        quarantined by this very read, unreadable, or not a JSON object.
        Callers treat all four the same -- start empty -- but none is an EMPTY
        DOCUMENT, which is a real file the app migrates and rewrites.
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
            # Absent -- or moved aside by another reader mid-read, which is why
            # this is checked after the read and not before: a file quarantined
            # under this one reads as "nothing there" rather than as an empty
            # document, which would be migrated back into existence.
            return None
        if not isinstance(content, dict):
            log.error(
                f"Plugin settings file {self.path} does not contain a JSON object "
                f"-- treating it as empty"
            )
            return None
        return content

    def save_document(self, document: dict[str, Any]) -> None:
        """Replace the file with ``document``, atomically."""
        get().write(PLUGIN, document, self.path)

    def read(self) -> dict[str, Any]:
        """The plugin's settings, unwrapped, migrating a pre-envelope file once."""
        document = self.document()
        if document is None:
            return {}
        if document.get("file-version") == PLUGIN_FILE_VERSION:
            return document.get("settings", {})
        # Pre-envelope: the whole file WAS the settings. Rewrite it wrapped, and
        # hand back what it held either way -- a migration that cannot be
        # written must not also cost the plugin its settings for this run.
        try:
            self.save_document({"file-version": PLUGIN_FILE_VERSION, "settings": document})
        except OSError as e:
            log.opt(exception=e).error(
                f"Could not migrate plugin settings file {self.path} to the current format"
            )
        return document

    def write(self, settings: Any) -> None:
        """Store ``settings`` as the plugin's own keys, atomically."""
        document = self.document()
        if document is None or document.get("file-version") != PLUGIN_FILE_VERSION:
            # Nothing usable, or a pre-envelope file whose own keys ARE the
            # settings being replaced: this write becomes the envelope and none
            # of it survives. What survives on an already wrapped file is what
            # the app keeps beside them -- the asset overrides.
            document = {"file-version": PLUGIN_FILE_VERSION}
        document["settings"] = settings
        self.save_document(document)
