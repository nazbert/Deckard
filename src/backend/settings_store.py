"""
One owner for the app's settings files.

A settings surface is a JSON file the app itself owns: the deck settings under
``settings/decks/``, the asset library index, the app settings, the page
manager's own bookkeeping, the file a plugin's settings are kept in. Each of
them needs the same three answers -- where does it live, what happens when it
is corrupt, and who may write it -- and each of them used to answer
separately. The answers had drifted: one surface was healed and quarantined by
a loader, another was read with a bare ``json.load`` inside a constructor and
took the whole app down with it, and a third was written straight past the
cache that other readers were served from.

This module is where those answers live now.

WHAT A SURFACE IS

A ``SurfaceSpec`` says four things about one file, and nothing else knows them:

* ``path_fn`` -- where it is, resolved at call time rather than at import, so a
  surface follows the data path instead of freezing the one that existed when
  this module was first imported. A ``keyed`` surface takes a key (the deck
  serial); a keyless one refuses to be given one, which turns a swapped
  argument into an exception rather than a file named after a serial number.
* ``root`` -- the type of an empty one. This is the difference between a
  list-rooted file that is missing and a dict: the asset library index is a
  JSON array, and handing its reader ``{}`` on a fresh install is how it came
  to be seeded with the wrong shape.
* ``cached`` -- whether reads are served from memory. A cached surface hands
  out a deep copy per read, so a caller may mutate what it got without
  reaching anything else, and its entry is dropped the moment the store writes
  that path.
* ``shared`` -- whether a cached surface hands out the cached object ITSELF
  instead of a copy of it. The app settings are one dict that many holders
  read, mutate in place, and save back, and every one of them has to be
  looking at the same one: hand each reader its own copy and a write through
  one view becomes invisible to the reader that is about to persist another.
  A surface is shared because its callers were written that way, not because
  sharing is cheaper. Only a cached surface can be one.
* ``schema`` -- the table of defaults its readers fall back to, or None for a
  surface whose keys the app does not describe. A surface that has one is
  read through a ``SchemaView`` (below): defaults applied at read, unknown
  keys refused at write. Whether to describe a surface is a decision per
  surface rather than one this module makes for all of them.

DEFAULTS ARE APPLIED AT READ, AND STORAGE STAYS SPARSE

A settings file holds only what was actually chosen. Everything else comes
from the surface's schema, at the moment it is read, and is never written
back. That is the whole difference between a default and a value: filling a
missing key on a load path -- ``setdefault`` and save, the shape this replaced
-- persists it, so the first person to open a settings page freezes today's
default into their config and the day the default changes they are the only
ones who do not get it. It also means a load path that writes, which is how a
settings pane came to dim a deck to a number nobody chose.

So ``read`` hands back exactly what is on disk (its callers mutate it and save
it back; materializing defaults into that dict would persist the whole table
on the next save), and a ``SchemaView`` applies the defaults on the way out.
A view is built from ONE read and destructured from there: asking the store
per key pays for a read per key.

CORRUPT IS NOT FATAL, AND CORRUPT IS NOT DISCARDED

``load_file`` is the single read-with-heal. A file that is absent reads as an
empty root. A file that is present but unparseable is moved aside to a
``.corrupt`` sidecar -- never clobbering an older one -- logged loudly, and
read as an empty root, with the corruption reported back so a caller holding a
backup can heal from it. Quarantining rather than leaving it in place is the
whole point: the caller gets an empty result either way, but the next save
would otherwise overwrite the only remaining copy of the user's data. The heal
must not depend on the rename succeeding, so the reported flag is set whether
or not the file could be moved.

An unreadable-but-present file (permissions, a dead mount) is NOT corrupt and
is NOT quarantined: the read raises, because the content is unknown and
pretending it is empty would invite a write that destroys it. One surface
answers that differently and says why it does: ``PluginSettings``, where a
raise costs the user the whole plugin.

WRITES GO THROUGH THE ATOMIC WRITER, AND INVALIDATE BY PATH

Every write here lands via ``atomic_write_json``: temp file, fsync, rename, so
an interrupted write can never leave a truncated settings file. Immediately
after, the store drops any cached entry for that RESOLVED path. Invalidation is
therefore keyed by the file that was written, not by the surface the writer
thought it was writing -- a write through the generic path-level entry point
invalidates the cached surface it happens to land on, and there is no way to
write through this module and leave a stale reader behind.

Dropping the entry is not enough on its own, because a reader can be inside
the file when the write lands: it would finish afterwards and re-insert the
content from BEFORE the write, which no later write to any other file would
ever correct. So invalidation COUNTS as well as drops, per path, and a read
that missed only caches what it parsed if the count has not moved since the
miss. A read that loses that race still answers its own caller -- it read what
was there when it read it -- but it leaves the cache cold for the next one.

THREADING

``edit()`` serializes a read-modify-write against other ``edit()`` calls on the
same file, which is what a concurrent pair of them needs to not lose an update.
The cache lock is a leaf: it is never held across file I/O, and never across
the edit lock. Nothing else in here blocks.

The store is a module singleton reached through ``get()``, deliberately not a
``gl`` slot: naming a protocol should shrink the shared namespace, not add to
it. Its imports are the standard library, globals, the atomic writer and the
logger -- no toolkit -- so any layer may import it.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from loguru import logger as log

import globals as gl
from src.backend.atomic_json import (
    atomic_write_json,
    prune_corrupt_sidecars,
    quarantine_corrupt_file,
)


@dataclass(frozen=True)
class SurfaceSpec:
    """One settings file, described once.

    Specs are module-level constants, compared and hashed by value; the store
    keys nothing on their identity, so a caller that builds an equal spec of
    its own gets the same behaviour as the registered one.
    """

    #: Human name, used in error messages only.
    name: str
    #: Called with the key for a keyed surface, with nothing for a keyless
    #: one. Every read and write in here resolves the path through this, per
    #: call, rather than holding one resolved at import: a caller is free to
    #: snapshot ``path()`` for its own use, but the store never does.
    path_fn: Callable[..., str]
    #: Type of an empty one -- ``dict`` for an object-rooted file, ``list``
    #: for an array-rooted one. Missing and corrupt both read as ``root()``.
    root: type[dict[str, Any]] | type[list[Any]] = dict
    #: Defaults table, or None for a surface whose keys the app does not
    #: describe. Read through a ``SchemaView``, never merged into the stored
    #: content (see the module docstring). Out of the hash (a table is a
    #: mapping, and a mapping is unhashable) but still part of equality: two
    #: specs describing the same file under different schemas are different
    #: specs, and a spec must stay usable as a dict key.
    schema: Mapping[str, Any] | None = field(default=None, hash=False)
    #: Serve reads from memory, deep-copied per call, dropped on write.
    cached: bool = False
    #: Whether ``path_fn`` takes a key.
    keyed: bool = False
    #: Hand every reader the cached object itself rather than a copy (see the
    #: module docstring). Requires ``cached``.
    shared: bool = False

    def __post_init__(self) -> None:
        # Fails at import, where the spec is written, rather than at the first
        # read: an uncached surface has nothing to share, so asking for it is a
        # mistake about what the surface is, not a runtime condition.
        if self.shared and not self.cached:
            raise ValueError(f"the {self.name} surface is shared but not cached: there is nothing to share")

    def path(self, key: str | None = None) -> str:
        """This surface's file, for ``key`` where it has one.

        A missing or surplus key is an error rather than a guess: the keyed
        surfaces are per-device files, and inventing a path for a caller that
        forgot the serial would write settings nothing ever reads back.
        """
        if self.keyed:
            if key is None:
                raise ValueError(f"the {self.name} surface is keyed: a key is required")
            return self.path_fn(key)
        if key is not None:
            raise ValueError(f"the {self.name} surface takes no key, got {key!r}")
        return self.path_fn()


# --------------------------------------------------------------------- #
# The registered surfaces                                               #
# --------------------------------------------------------------------- #

# Every deck-settings default, defined exactly once. Each of these used to be
# an inline literal at the four or five places that read the key, and they had
# drifted: the device dimmed an idle deck to one number while the page editor
# showed another, and the settings pane wrote a third into the file the first
# time it was opened.
#
# A name here maps either to a SECTION (its own table of keys) or to a
# top-level setting stored as a bare value.
DECK_DEFAULTS: dict[str, Any] = {
    "brightness": {
        # What the deck runs at when nothing has chosen a brightness. The
        # device layer and the page-level brightness UI have always used 75;
        # the deck settings pane said 50 and applied it on open, which is the
        # one surface that had to move.
        "value": 75,
    },
    "screensaver": {
        "enable": False,
        "media-path": None,
        # Loop ON. A screensaver video or GIF whose config predates the loop
        # toggle otherwise plays exactly one pass and then holds its last
        # frame on the device for the whole idle window -- a frozen deck,
        # never what "screensaver" means. True is also what every media layer
        # already defaults to (ScreenSaver.loop, Background.set_from_path /
        # prebuild_from_path, BackgroundVideo/GifBackground), so the toggle
        # can no longer show OFF while the media loops.
        "loop": True,
        "fps": 30,
        # Minutes of no input before it shows.
        "time-delay": 5,
        # 30, not the 75 the running brightness defaults to: a screensaver
        # that "dims" to the normal brightness is no screensaver. 30 is also
        # what the device has actually applied to every config without the
        # key, so this is the number those decks were already using -- the
        # page editor was the surface displaying a value the deck ignored.
        "brightness": 30,
    },
    "background": {
        "enable": False,
        "media-path": None,
        # Loop ON, for the screensaver's reason: a deck-level background is
        # the "leave it running" case, and a config predating the toggle would
        # otherwise freeze on its last frame. A PAGE-level background keeps
        # its own literal False where the page arm reads it -- a one-shot
        # flourish on page entry is a real use there, and this table describes
        # the deck.
        "loop": True,
        "fps": 30,
        "extend-to-touchscreen": False,
    },
    "display": {
        # A strict no-op factor: every application site compares against it
        # before doing any enhancement work. The clamp and the non-finite
        # rejection stay with the reader that feeds an ImageEnhance factor and
        # a cache key -- a defaults table describes absence, not validity.
        "saturation": 1.0,
    },
    # Degrees. Stored as a bare int rather than a section, which is why it is
    # read and written without a key.
    "rotation": 0,
    # Deliberately absent: "key-layout". A fake deck's layout is decided by
    # whoever constructs the deck and passed in as that caller's own fallback,
    # so there is no one shape this table could name for it.
}

#: Per-deck settings, one file per serial. Cached because the render path
#: reads them repeatedly and the read is a JSON parse; deep-copied per read
#: because most callers mutate what they get and save it back.
DECK = SurfaceSpec(
    name="deck settings",
    path_fn=lambda serial: os.path.join(gl.DATA_PATH, "settings", "decks", f"{serial}.json"),
    root=dict,
    schema=DECK_DEFAULTS,
    cached=True,
    keyed=True,
)

#: The custom asset library index. A JSON ARRAY, which is why ``root`` is not
#: the default: an absent or corrupt one must read as an empty library, and a
#: dict there is what makes a list-rooted reader silently see nothing.
#: Uncached -- it is read once at construction and written by the import
#: worker, and the backend holds the parsed list itself.
ASSET_LIBRARY = SurfaceSpec(
    name="asset library",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets.json"),
    root=list,
)


# Every app-settings default, defined exactly once. Callers read through
# AppSettings instead of repeating `.get(section, {}).get(key, default)`.
APP_DEFAULTS: dict[str, dict] = {
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


def _fallback_font() -> str:
    # Resolved lazily: gl.fallback_font runs a system font scan on first
    # access (globals.py __getattr__), so it must not be evaluated while
    # this module is being imported.
    return gl.fallback_font


# general.default-font subkeys. Deliberately NOT the schema of a section:
# these use `or` fallback semantics (a stored value that is falsy falls back
# too, because an empty family or a zero size is a half-written font, not a
# choice), and one of them is resolved by calling it. A schema answers "this
# key is absent"; this table answers "there is no usable value here", and
# folding the second into the first would either start honouring a zero size
# or give every schema in the app a second meaning. It lives next to
# APP_DEFAULTS because it describes the same file, and it is read only through
# ``AppSettings.font_default``.
APP_FONT_DEFAULTS: dict = {
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

#: The app's own settings. Shared rather than copied per read: its holders
#: were written around one process-wide dict -- the settings dialog's rows,
#: the store pages and the launch counter all read it, write into it and save
#: it back -- and splitting that into a copy per reader would lose whichever
#: write was not the last one saved. The one caller that must NOT join it is
#: the settings dialog's construction snapshot, which reads through
#: ``read_fresh`` for exactly that reason.
APP = SurfaceSpec(
    name="app settings",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "settings", "settings.json"),
    root=dict,
    schema=APP_DEFAULTS,
    cached=True,
    shared=True,
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


#: One plugin's settings file, keyed by the path the plugin resolved for itself
#: rather than by its id: that path is decided once in ``PluginBase.__init__``
#: (which migrates a legacy folder-name directory to the manifest-id one), and
#: rederiving it here would give the store a second opinion about where a
#: plugin's settings live. Schema-less by design -- the app owns the envelope
#: and nothing inside it, so a defaults table could only describe keys the
#: plugin alone knows about, and its write-side tripwire would reject every one
#: of them. Uncached: a cache would buy a parse nothing repeats and owe a
#: coherence answer for a backend running in another process.
PLUGIN = SurfaceSpec(
    name="plugin settings",
    path_fn=lambda settings_path: settings_path,
    root=dict,
    keyed=True,
)

#: The envelope version this app writes: it keeps the plugin's own keys under
#: ``settings``. Anything else IS the settings, from before the envelope
#: existed, and is migrated the first time it is read.
PLUGIN_FILE_VERSION = "2.0"


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


class SettingsStore:
    """The process-wide settings store. Reached through ``get()``."""

    def __init__(self) -> None:
        # Resolved path -> parsed content. The content here is the master
        # copy: it is never handed out, only deep-copied from. It is keyed by
        # FILE and holds an entry per settings file actually read, so it grows
        # with the number of decks a session has seen and not with how often
        # they are read. The cache the deck settings used to live in was per
        # settings-manager instance and kept that instance alive; this one
        # belongs to the file it caches, which is what it was always
        # describing.
        self._cache: dict[str, Any] = {}
        # Resolved path -> how many times that path has been invalidated. A
        # read that misses records this before it parses and stores what it
        # parsed only if the number has not moved, so a write landing during a
        # cold read cannot be undone by that read finishing afterwards.
        # Counted rather than flagged because the reader must be able to tell
        # "nothing happened" from "something happened and was then undone
        # again". Bounded like the edit locks: one entry per settings file
        # ever WRITTEN through the store.
        self._invalidations: dict[str, int] = {}
        # Raw path -> the resolved path it is cached under, for reads served
        # from memory. Resolving is an lstat walk of every component, and
        # paying it per read puts a syscall chain in front of what is
        # otherwise a dict lookup -- the app settings are read from the label
        # engine and the media caches, not once per load, and measured 30x the
        # cost of the plain shared dict they replaced.
        #
        # What remembering it costs, exactly: symlink retargeting moves from
        # the set of outside changes the store followed correctly into the set
        # it is blind to until the next write -- joining outside rewrites of
        # the file, which the content cache never followed. Resolving per read
        # meant a repointed link changed the cache key, so the very next read
        # missed and parsed the new target; now the old target's content is
        # served until something resolves again.
        #
        # Every write, edit and invalidation still resolves for real -- nothing
        # is ever written, locked or invalidated under a remembered name -- and
        # any write through the store drops the whole memo, so the blindness
        # self-heals at the next write. The shape to have in mind is a managed
        # config tree (stow, chezmoi) re-linking these files while the app is
        # running.
        self._resolved: dict[str, str] = {}
        self._cache_lock = threading.Lock()
        # Resolved path -> the lock edit() serializes on. Created on demand and
        # kept, because a settings file is edited again and the map is bounded
        # by how many settings files exist.
        self._edit_locks: dict[str, threading.Lock] = {}
        self._edit_locks_guard = threading.Lock()

    # -- surfaces ------------------------------------------------------ #

    def read(self, spec: SurfaceSpec, key: str | None = None) -> Any:
        """This surface's content, or an empty root if it is absent or corrupt.

        A cached surface answers from memory with a deep copy, so mutating the
        result reaches nothing else and is not persisted -- pair it with
        ``write`` exactly as an uncached read must be. A SHARED surface is the
        exception, and answers with the one object every other reader of it
        holds; mutating that one is seen by all of them, still unpersisted.
        """
        data, _corrupt = self.read_reporting_corruption(spec, key)
        return data

    def read_reporting_corruption(self, spec: SurfaceSpec, key: str | None = None) -> tuple[Any, bool]:
        """``(content, corrupt)`` for this surface.

        ``corrupt`` describes THIS read: True only when the read found the
        file present and unparseable -- not for a legitimately empty one and
        not for a missing one. A caller holding a backup heals on the flag
        rather than on the quarantine side-effect having removed the primary:
        corrupt is corrupt whether or not the file could be moved aside.

        It is deliberately not a property of the surface, so it is not cached
        with the content. The read that quarantines reports True; a later read
        of the same surface finds the file gone and reports False, and a
        cached answer says False for the same reason -- which is what a fresh
        read of that surface would now say.
        """
        path = spec.path(key)
        if not spec.cached:
            return self.load_file(path, root=spec.root)

        resolved = self._resolve_for_read(path)
        with self._cache_lock:
            if resolved in self._cache:
                return self._handout(spec, self._cache[resolved]), False
            # The invalidation count as of the miss. Recorded under the same
            # lock the miss was decided under, so a write that lands from here
            # on is guaranteed to move it.
            generation = self._invalidations.get(resolved, 0)

        # Parsed outside the lock: a parse must never block another surface's
        # reader.
        data, corrupt = self.load_file(path, root=spec.root)

        with self._cache_lock:
            if self._invalidations.get(resolved, 0) == generation:
                # setdefault, not assignment: two readers can miss the same
                # cold surface at once, and the one that finishes second must
                # adopt the entry the first left rather than replace it. The
                # two parsed the same bytes -- an intervening write would have
                # moved the count -- so for a copying surface this is a
                # formality, but for a SHARED one it is what makes "every
                # reader holds the same object" true rather than usually true.
                data = self._cache.setdefault(resolved, data)
            # Otherwise a write landed while this read was in the file: what
            # was just parsed is the content from BEFORE that write, and
            # caching it would leave every later reader on the old settings
            # with nothing to correct it. Dropped instead -- this caller still
            # gets the content it read, which is a read that happened before
            # the write, and the next reader loads afresh.
        return self._handout(spec, data), corrupt

    def read_fresh(self, spec: SurfaceSpec, key: str | None = None) -> Any:
        """This surface as it is ON DISK right now, as a copy of its own.

        For the caller shape a cache cannot serve: an editor that takes a
        snapshot, changes several things against it, and writes the whole
        snapshot back when it is done. It must not join a shared surface --
        its half-finished edits would read as settled to everyone else -- and
        it must not be served a cache entry, because what it eventually writes
        has to be what it was shown. Corruption heals exactly as in any other
        read; a cached surface's entry is neither read nor filled.
        """
        data, _corrupt = self.load_file(spec.path(key), root=spec.root)
        return data

    def write(self, spec: SurfaceSpec, data: Any, key: str | None = None) -> None:
        """Persist this surface atomically and drop its cached entry."""
        self.save_file(spec.path(key), data)

    @contextmanager
    def edit(self, spec: SurfaceSpec, key: str | None = None) -> Iterator[Any]:
        """Read-modify-write this surface, serialized against other edits of it.

        The block is handed the current content and whatever it leaves behind
        is written when the block ends. An exception inside the block writes
        nothing: a half-applied edit is worse than none, and the caller is the
        only one who knows whether its half was meaningful.

        Serialization is per FILE, not per surface type -- two decks are two
        locks. It bounds only other ``edit()`` calls; a plain ``write`` of the
        same surface still lands whenever it lands, which is the same
        last-writer-wins it has always been.

        The read is always from disk, cached surface or not: the point of the
        block is that what is written is what was just read, and a cache is by
        definition a copy of something the store did not watch.
        """
        path = spec.path(key)
        with self._edit_lock(path):
            data, _corrupt = self.load_file(path, root=spec.root)
            yield data
            self.save_file(path, data)

    # -- files --------------------------------------------------------- #

    def load_file(self, file_path: str, root: type[dict[str, Any]] | type[list[Any]] = dict) -> tuple[Any, bool]:
        """Read one JSON file, healing a corrupt one. Returns ``(data, corrupt)``.

        The path-level entry point: it knows nothing about surfaces and is
        what the settings-manager facade forwards its own path-taking readers
        to. ``root`` decides what an absent or corrupt file reads as.
        """
        empty = root()
        if not os.path.exists(file_path):
            return empty, False
        try:
            with open(file_path) as f:
                return json.load(f), False
        except FileNotFoundError:
            # Raced a concurrent quarantine between the exists() check and
            # the open.
            return empty, False
        except ValueError as e:
            # ValueError, not JSONDecodeError: garbage bytes raise
            # UnicodeDecodeError while decoding, which is a ValueError but not
            # a JSON error -- it used to escape this handler and propagate out
            # of every page/settings load. JSONDecodeError is itself a
            # ValueError subclass, so one clause covers both.
            # Quarantine instead of leaving the corrupt file in place: the
            # caller gets an empty root either way, but the next save would
            # overwrite the only remaining copy of the user's data. Renamed
            # aside it stays recoverable (a prior .corrupt is never
            # clobbered), and page loads heal from their backup off the
            # returned corrupt=True flag regardless of whether this rename
            # succeeded.
            moved, dest = quarantine_corrupt_file(file_path)
            if moved:
                log.error(f"Invalid json in {file_path}: {e} -- preserved at {dest}, loading empty")
                # Bounded retention, scoped to the file that just gained a
                # sidecar -- never a startup-wide sweep. Covers pages
                # and deck/app settings alike: page loads route their
                # corrupt-read handling through this loader.
                for pruned in prune_corrupt_sidecars(file_path, protect=dest):
                    log.info(f"Pruned old quarantined copy {pruned}")
            else:
                log.error(
                    f"Invalid json in {file_path}: {e} -- could NOT move it aside "
                    f"(left in place); callers with a backup will heal, loading empty"
                )
            return empty, True

    def save_file(self, file_path: str, data: Any) -> None:
        """Write one JSON file atomically, then invalidate that path.

        Invalidation follows the write rather than the surface, so a caller
        that reaches a cached file through this path-level entry point cannot
        leave a stale reader behind.
        """
        # Atomic write (tmp file + fsync + os.replace) so an interrupted
        # write can't truncate the settings file; also creates parent dirs.
        atomic_write_json(file_path, data)
        self.invalidate_path(file_path)

    def invalidate_path(self, file_path: str) -> None:
        """Forget the cached content of one file, if any is held."""
        resolved = _resolve(file_path)
        with self._cache_lock:
            self._cache.pop(resolved, None)
            # Resolutions are remembered for the read fast path only, and only
            # between writes: a link that has moved is followed again from
            # here on, and the entry it used to point at is already gone.
            self._resolved.clear()
            # Counted even when nothing was cached: a reader may be between
            # its miss and its store right now, holding content this write has
            # just superseded, and dropping an entry that does not exist yet
            # is the one thing a cache cannot do.
            self._invalidations[resolved] = self._invalidations.get(resolved, 0) + 1

    # -- internals ----------------------------------------------------- #

    def _resolve_for_read(self, file_path: str) -> str:
        """The cache key for a read, remembered per raw path (see ``_resolved``).

        Deliberately outside the cache lock and deliberately not guarded by
        one of its own: two readers racing here compute the same answer, and
        one of them writing it twice costs a dict store. Only reads come
        through this -- writes, edits and invalidations resolve for real.
        """
        try:
            return self._resolved[file_path]
        except KeyError:
            resolved = _resolve(file_path)
            self._resolved[file_path] = resolved
            return resolved

    @staticmethod
    def _handout(spec: SurfaceSpec, data: Any) -> Any:
        """What a reader of a cached surface receives: the cached object
        itself for a shared surface, a deep copy of it for every other."""
        return data if spec.shared else copy.deepcopy(data)

    def _edit_lock(self, file_path: str) -> threading.Lock:
        resolved = _resolve(file_path)
        with self._edit_locks_guard:
            lock = self._edit_locks.get(resolved)
            if lock is None:
                lock = threading.Lock()
                self._edit_locks[resolved] = lock
            return lock


def _resolve(file_path: str) -> str:
    """The cache and lock key for a path.

    Symlinks are resolved because the atomic writer resolves them too: a
    settings file that is a link into a managed config tree must read, write,
    lock and invalidate under one name, not two.
    """
    return os.path.realpath(file_path)


# The process-wide settings store. A module singleton rather than a `gl` slot,
# for the reason the startup queue and the control plane are ones: naming a
# protocol should shrink the shared namespace, not add to it.
_store = SettingsStore()


def get() -> SettingsStore:
    """The process-wide settings store. Never None."""
    return _store
