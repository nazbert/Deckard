"""One owner for the app's settings files.

A settings surface is a JSON file the app owns. The deck settings under
settings/decks/, the asset library index, the app settings, the page manager's
bookkeeping and a plugin's settings file are all surfaces. Each one needs the
same three answers. Where does it live, what happens when it is corrupt, and
who may write it. SurfaceSpec, load_file and save_file below hold those
answers.

A settings file holds what the user chose, and the schema supplies the rest at
read time. SchemaView applies those defaults; its docstring says why a read
must never write one back.

This module imports stdlib, globals, the atomic writer and the logger, and no
toolkit, so any layer can import it.
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

    A spec is a module-level constant, and it compares and hashes by value.
    The store keys nothing on identity, so a caller that builds an equal spec
    gets the behaviour of the registered one.
    """

    #: Human name, used in error messages only.
    name: str
    #: Takes the key for a keyed surface, and nothing for a keyless one.
    #: Every read and write here resolves the path through this, per call, and
    #: holds no path resolved at import. A caller may snapshot path() for its
    #: own use, and the store never does.
    path_fn: Callable[..., str]
    #: Type of an empty file. dict for an object-rooted file, list for an
    #: array-rooted one. A missing file and a corrupt file both read as
    #: root().
    root: type[dict[str, Any]] | type[list[Any]] = dict
    #: Defaults table, or None for a surface whose keys the app does not
    #: describe. A SchemaView reads it, and nothing merges it into the stored
    #: content (see the module docstring). It stays out of the hash, because a
    #: table is a mapping and a mapping is unhashable, and it stays part of
    #: equality. Two specs that describe one file under different schemas are
    #: different specs, and a spec must work as a dict key.
    schema: Mapping[str, Any] | None = field(default=None, hash=False)
    #: Serve reads from memory, deep-copied per call, dropped on write.
    cached: bool = False
    #: Whether path_fn takes a key.
    keyed: bool = False
    #: Hand every reader the cached object itself rather than a copy (see the
    #: module docstring). This needs cached.
    shared: bool = False

    def __post_init__(self) -> None:
        # Fail at import, where the author writes the spec, and not at the
        # first read. An uncached surface has nothing to share, so this
        # request describes the surface wrongly.
        if self.shared and not self.cached:
            raise ValueError(f"the {self.name} surface is shared but not cached: there is nothing to share")

    def path(self, key: str | None = None) -> str:
        """This surface's file, for key where the surface takes one.

        A missing or surplus key raises rather than starts a guess. A keyed
        surface holds a per-device file, and a path invented for a caller that
        forgot the serial writes settings that nothing reads back.
        """
        if self.keyed:
            if key is None:
                raise ValueError(f"the {self.name} surface is keyed: a key is required")
            return self.path_fn(key)
        if key is not None:
            raise ValueError(f"the {self.name} surface takes no key, got {key!r}")
        return self.path_fn()


# The registered surfaces.

# Every deck-settings default, defined once. An inline literal per reader
# drifts, so the device, the page editor and the settings pane each apply a
# different number for one key.
#
# A name here maps to a section, which holds its own table of keys, or to a
# top-level setting stored as a bare value.
DECK_DEFAULTS: dict[str, Any] = {
    "brightness": {
        # What the deck runs at while nothing chooses a brightness. The
        # device layer and the page-level brightness UI both use 75.
        "value": 75,
    },
    "screensaver": {
        "enable": False,
        "media-path": None,
        # Loop by default. A screensaver video or GIF whose config predates
        # the loop toggle otherwise plays one pass and holds its last frame on
        # the device for the whole idle window, which freezes the deck. Every
        # media layer already defaults to True (ScreenSaver.loop,
        # Background.set_from_path, Background.prebuild_from_path,
        # BackgroundVideo and GifBackground), so the toggle and the media
        # agree.
        "loop": True,
        "fps": 30,
        # Minutes of no input before it shows.
        "time-delay": 5,
        # 30, and not the 75 of the running brightness. A screensaver that
        # dims to the normal brightness dims nothing. The device also applies
        # 30 to every config without this key, so those decks already run at
        # this number, and the page editor showed a value the deck ignored.
        "brightness": 30,
    },
    "background": {
        "enable": False,
        "media-path": None,
        # Loop by default, for the reason the screensaver does. A deck-level
        # background runs until something stops it, and a config that predates
        # the toggle otherwise freezes on its last frame. A page-level
        # background keeps its own False where the page arm reads it, because
        # a single pass on page entry is a real use there, and this table
        # describes the deck.
        "loop": True,
        "fps": 30,
        "extend-to-touchscreen": False,
    },
    "display": {
        # A factor that changes nothing. Every application site compares
        # against it before it enhances an image. The clamp and the
        # non-finite check stay with the reader that feeds an ImageEnhance
        # factor and a cache key, because a defaults table describes an absent
        # value rather than a valid one.
        "saturation": 1.0,
    },
    # Degrees. Stored as a bare int rather than a section, so it reads and
    # writes without a key.
    "rotation": 0,
    # This table names no "key-layout". Whoever constructs a fake deck decides
    # its layout and passes that caller's own fallback, so no single shape
    # fits here.
}

#: Per-deck settings, one file per serial. Cached, because the render path
#: reads them again and again and each read parses JSON. Deep-copied per read,
#: because most callers mutate what they get and save it back.
DECK = SurfaceSpec(
    name="deck settings",
    path_fn=lambda serial: os.path.join(gl.DATA_PATH, "settings", "decks", f"{serial}.json"),
    root=dict,
    schema=DECK_DEFAULTS,
    cached=True,
    keyed=True,
)

#: The custom asset library index. A JSON array, so root differs from the
#: default. An absent or corrupt index must read as an empty library, and a
#: dict there leaves a list-rooted reader with nothing. Uncached, because the
#: backend reads it once at construction, the import worker writes it, and the
#: backend holds the parsed list itself.
ASSET_LIBRARY = SurfaceSpec(
    name="asset library",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets.json"),
    root=list,
)


# Every app-settings default, defined exactly once. Callers read through
# AppSettings, rather than repeat .get(section, {}).get(key, default).
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
        # Three states. None means "never asked", and it makes
        # mainWindow.on_close raise the KeepRunningDialog.
        "keep-running": None,
        "autostart": True,
        "lock-on-lock-screen": True,
    },
    "performance": {
        "n-cached-pages": 3,
        "cache-videos": True,
        # Quiescence gating. "screensaver" engages nothing extra, because the
        # deck screensaver's own transition already releases the underlying
        # page's media. "system-idle" also pauses deck animations while the
        # session is idle or locked.
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
    # Resolve this late. gl.fallback_font runs a system font scan on first
    # access, through the __getattr__ of globals.py, so it must not run during
    # the import of this module.
    return gl.fallback_font


# The subkeys of general.default-font. This is no section schema. These keys
# fall back on falsy values as well, because an empty family or a zero size is
# a half-written font rather than a choice, and one value resolves through a
# call. A schema answers "this key is absent". This table answers "no usable
# value sits here". A merge of the two honours a zero size, or gives every
# schema in the app a second meaning. It sits next to APP_DEFAULTS because it
# describes the same file, and only AppSettings.font_default reads it.
APP_FONT_DEFAULTS: dict = {
    "font-family": _fallback_font,
    "font-size": 15,
    "font-weight": 400,
    "font-style": "normal",
    "font-color": (255, 255, 255, 255),
    # Alpha 255, and not 1. This value feeds color_values_to_gdk, which reads
    # 0 to 255 on all four channels, and the render fallback is (0,0,0,255).
    "outline-color": (0, 0, 0, 255),
    "outline-width": 2,
}

#: The app's own settings. Shared rather than copied per read. Its holders
#: work on one process-wide dict. The settings dialog's rows, the store pages
#: and the launch counter all read it, write into it and save it back, and a
#: copy per reader loses every write except the last one saved. One caller
#: must stay off it, the settings dialog's construction snapshot, and it reads
#: through read_fresh for that reason.
APP = SurfaceSpec(
    name="app settings",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "settings", "settings.json"),
    root=dict,
    schema=APP_DEFAULTS,
    cached=True,
    shared=True,
)



# The page manager's own bookkeeping, which is the page each deck opens on
# and the pages it keeps warm. It carries no schema, because its readers walk
# raw .get chains over a default-pages map keyed by serial. It stays uncached,
# because a handful of page operations read and rewrite it and no render path
# touches it. Its read-modify-write callers go through edit(), which
# serializes them so two of them keep both updates.
PAGES = SurfaceSpec(
    name="page manager settings",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "settings", "pages.json"),
    root=dict,
)

#: The static settings, which hold the data-path override. This file sits at
#: a fixed location outside the data path, because it chooses the data path.
#: Uncached, because a settings pane reads it rarely. The bootstrap read of
#: this file in globals.py is the one approved reader that skips this module.
#: It runs before this module or anything else is importable, and it defines
#: gl.DATA_PATH, which every keyed surface resolves against.
#: Its quiet fallback on an error suits a bootstrap and stays there.
STATIC = SurfaceSpec(
    name="static settings",
    path_fn=lambda: gl.STATIC_SETTINGS_FILE_PATH,
    root=dict,
)


# The asset chooser's two filter toggles, remembered between openings. Both
# default to True, because a chooser that opens empty until the user finds the
# toggles reads as broken. They live here once, rather than as an inline
# .get(..., True) at each of the three sites that read or write them.
UI_ASSET_MANAGER_DEFAULTS: dict[str, Any] = {
    "video-toggle": True,
    "image-toggle": True,
}

#: The asset manager window's remembered UI state. It carries a schema for
#: the two toggles. It stays uncached, because each open reads it once on the
#: main thread. A SchemaView reads it, so the True defaults live in one place
#: rather than at every call site.
UI_ASSET_MANAGER = SurfaceSpec(
    name="asset manager ui state",
    path_fn=lambda: os.path.join(gl.DATA_PATH, "settings", "ui", "AssetManager.json"),
    root=dict,
    schema=UI_ASSET_MANAGER_DEFAULTS,
)



#: One plugin's settings file, keyed by the path the plugin resolved for
#: itself and not by its id. PluginBase.__init__ decides that path once, and
#: it moves an old folder-name directory to the manifest-id one. A second
#: derivation here gives the store another opinion about where a plugin's
#: settings live. It carries no schema, because the app owns the envelope and
#: nothing inside it. A defaults table could describe only keys that the
#: plugin knows, and the write-side check refuses every one of them. It
#: stays uncached, because a cache saves a parse that nothing repeats, and
#: owes a coherence answer for a backend in another process.
PLUGIN = SurfaceSpec(
    name="plugin settings",
    path_fn=lambda settings_path: settings_path,
    root=dict,
    keyed=True,
)

#: The envelope version this app writes. It keeps the plugin's own keys under
#: "settings". A file of another shape holds the settings themselves, from
#: before the envelope existed, and the first read migrates it.
PLUGIN_FILE_VERSION = "2.0"


class SettingsStore:
    """The process-wide settings store. get() reaches it."""

    def __init__(self) -> None:
        # Maps a resolved path to its parsed content. This content is the
        # master copy. Nothing hands it out, and every read deep-copies from
        # it. The file keys it, and it holds one entry per settings file read,
        # so it grows with the number of decks a session sees and not with the
        # number of reads. A per-instance cache instead keeps that instance
        # alive; this one belongs to the file it caches.
        self._cache: dict[str, Any] = {}
        # Maps a resolved path to the number of invalidations of that path. A
        # read that misses records this number before it parses, and stores
        # what it parsed only while the number stands still, so a write that
        # lands during a cold read survives that read. A count, and not a
        # flag, because the reader must tell "nothing happened" from
        # "something happened and something else undid it". The edit locks
        # bound it, at one entry per settings file written through the
        # store.
        self._invalidations: dict[str, int] = {}
        # Maps a raw path to the resolved path it caches under, for a read
        # served from memory. A resolution lstats every component, and a
        # resolution per read puts a chain of syscalls in front of a dict
        # lookup. The label engine and the media caches read the app settings,
        # rather than one read per load, and that measured 30 times the cost
        # of the plain shared dict.
        #
        # This memo costs one thing. A retargeted symlink joins the outside
        # changes that the store misses until the next write, next to an
        # outside rewrite of the file, which the content cache never followed.
        # A resolution per read changes the cache key when a link moves, so
        # the next read misses and parses the new target. With the memo the
        # old target's content serves until something resolves again.
        #
        # Every write, edit and invalidation still resolves for real, so
        # nothing writes, locks or invalidates under a remembered name. Any
        # write through the store drops the whole memo, so the next write
        # heals this. The case to picture is a managed config tree, such as
        # stow or chezmoi, that re-links these files while the app runs.
        self._resolved: dict[str, str] = {}
        # A leaf lock. No holder keeps it across file I/O, and no holder
        # keeps it across the edit lock.
        self._cache_lock = threading.Lock()
        # Maps a resolved path to the lock that edit() serializes on. Each
        # lock is built on demand and kept, because a settings file gets
        # edited again, and the number of settings files bounds the map.
        self._edit_locks: dict[str, threading.Lock] = {}
        self._edit_locks_guard = threading.Lock()

    # Surfaces

    def read(self, spec: SurfaceSpec, key: str | None = None) -> Any:
        """This surface's content, or an empty root when it is absent or
        corrupt.

        A cached surface answers from memory with a deep copy, so a mutation of
        the result reaches nothing else and persists nothing. Pair it with
        write(), as an uncached read needs too. A shared surface answers with
        the one object that every other reader holds. Every reader sees a
        mutation of that object, and it still persists nothing.
        """
        data, _corrupt = self.read_reporting_corruption(spec, key)
        return data

    def read_reporting_corruption(self, spec: SurfaceSpec, key: str | None = None) -> tuple[Any, bool]:
        """Returns (content, corrupt) for this surface.

        corrupt describes this read. It is True only when the read found the
        file present and unparseable, and False for an empty file and for a
        missing one. A caller that holds a backup heals on this flag, and not
        on the quarantine rename, because a corrupt file stays corrupt whether
        or not the rename worked.

        This flag belongs to the read and not to the surface, so nothing
        caches it with the content. The read that quarantines reports True. A
        later read of the same surface finds the file gone and reports False,
        and a cached answer reports False for the same reason, which is what a
        fresh read now says.
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

        # Parse outside the lock. A parse must not block another surface's
        # reader.
        data, corrupt = self.load_file(path, root=spec.root)

        with self._cache_lock:
            if self._invalidations.get(resolved, 0) == generation:
                # Use setdefault rather than an assignment. Two readers can
                # miss the same cold surface together, and the second one must
                # adopt the entry the first left. Both parsed the same bytes,
                # because a write between them moves the count. That changes
                # nothing for a copying surface, and it makes "every reader
                # holds one object" hold for a shared surface.
                data = self._cache.setdefault(resolved, data)
            # Otherwise a write landed while this read was in the file. The
            # parsed content is what stood before that write, and a cache of
            # it leaves every later reader on the old settings with nothing to
            # correct them. Drop it instead. This caller keeps the content it
            # read, which is a read from before the write, and the next reader
            # loads afresh.
        return self._handout(spec, data), corrupt

    def read_fresh(self, spec: SurfaceSpec, key: str | None = None) -> Any:
        """This surface as it stands on disk now, as a private copy.

        This serves a caller that a cache cannot. An editor takes a snapshot,
        changes several things against it, and writes the whole snapshot back
        at the end. It must stay off a shared surface, because every other
        reader takes its half-finished edits as settled, and it must not
        get a cache entry, because what it writes must match what it was
        shown. Corruption heals as in any other read, and a cached surface's
        entry stays neither read nor filled.
        """
        data, _corrupt = self.load_file(spec.path(key), root=spec.root)
        return data

    def view(self, spec: SurfaceSpec, key: str | None = None) -> SchemaView:
        """A SchemaView over this surface's current content.

        This serves a schema'd surface read outside the settings-manager
        facade, such as the asset chooser's toggle state. It wraps one read. A
        shared surface's view aliases the cached object, and every other view
        holds a copy (see SchemaView).
        """
        if spec.schema is None:
            raise ValueError(f"the {spec.name} surface has no schema to read through")
        return SchemaView(self.read(spec, key), spec.schema, shared=spec.shared)

    def write(self, spec: SurfaceSpec, data: Any, key: str | None = None) -> None:
        """Persist this surface atomically and drop its cached entry."""
        self.save_file(spec.path(key), data)

    @contextmanager
    def edit(self, spec: SurfaceSpec, key: str | None = None) -> Iterator[Any]:
        """Read-modify-write this surface, serialized against other edits of it.

        The block receives the current content, and the end of the block
        writes back whatever it leaves. An exception inside the block writes
        nothing, because a half-applied edit costs more than none, and only
        the caller knows whether its half meant anything.

        The lock covers one file, and not one surface type, so two decks take
        two locks. It bounds another edit() call only. A plain write() of the
        same surface lands whenever it lands, and the last writer wins.

        The read always comes from disk, for a cached surface too. The block
        exists so that the write matches the read that opened it, and a cache
        holds a copy of something the store did not watch.
        """
        path = spec.path(key)
        with self._edit_lock(path):
            data, _corrupt = self.load_file(path, root=spec.root)
            yield data
            self.save_file(path, data)

    # Files

    def load_file(self, file_path: str, root: type[dict[str, Any]] | type[list[Any]] = dict) -> tuple[Any, bool]:
        """Read one JSON file and heal a corrupt one. Returns (data,
        corrupt).

        This is the path-level entry point. It knows nothing about surfaces,
        and the settings-manager facade forwards its path-taking readers here.
        root decides what an absent or corrupt file reads as.
        """
        empty = root()
        if not os.path.exists(file_path):
            return empty, False
        try:
            with open(file_path) as f:
                return json.load(f), False
        except FileNotFoundError:
            # A concurrent quarantine moved the file between the exists()
            # check and the open.
            return empty, False
        except ValueError as e:
            # Catch ValueError. A file of garbage bytes raises
            # UnicodeDecodeError while the reader decodes it, and json raises
            # JSONDecodeError. Both derive from ValueError, so one clause
            # covers both.
            #
            # Quarantine the file rather than leave it in place. The caller
            # gets an empty root either way, and the next save overwrites the
            # only remaining copy of the user's data. A file renamed aside
            # stays recoverable, and the rename keeps an earlier .corrupt. A
            # page load heals from its backup on the returned corrupt flag,
            # whether or not this rename worked.
            moved, dest = quarantine_corrupt_file(file_path)
            if moved:
                log.error(f"Invalid json in {file_path}: {e} -- preserved at {dest}, loading empty")
                # Bound the sidecar count for the file that just gained one,
                # and never sweep at startup. This covers a page and the deck
                # and app settings alike, because a page load routes its
                # corrupt read through this loader.
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

        The invalidation follows the write rather than the surface, so a
        caller that reaches a cached file through this path-level entry point
        leaves no stale reader behind.
        """
        # An atomic write, with a temp file, an fsync and an os.replace, so an
        # interrupted write cannot truncate the settings file. It also creates
        # the parent directories.
        atomic_write_json(file_path, data)
        self.invalidate_path(file_path)

    def invalidate_path(self, file_path: str) -> None:
        """Forget the cached content of one file, if any is held."""
        resolved = _resolve(file_path)
        with self._cache_lock:
            self._cache.pop(resolved, None)
            # The memo of resolutions serves the read fast path only, and
            # only between writes. A link that moved gets followed again from
            # here on, and the entry it pointed at is already gone.
            self._resolved.clear()
            # Count this even when nothing was cached. A reader can sit
            # between its miss and its store right now, holding content that
            # this write replaces, and a cache cannot drop an entry that does
            # not exist yet.
            self._invalidations[resolved] = self._invalidations.get(resolved, 0) + 1

    # Internals

    def _resolve_for_read(self, file_path: str) -> str:
        """The cache key for a read, remembered per raw path (see _resolved).

        This runs outside the cache lock and takes no lock of its own. Two
        readers that race here compute the same answer, and a second write of
        it costs one dict store. Only a read comes through here. A write, an
        edit and an invalidation each resolve for real.
        """
        try:
            return self._resolved[file_path]
        except KeyError:
            resolved = _resolve(file_path)
            self._resolved[file_path] = resolved
            return resolved

    @staticmethod
    def _handout(spec: SurfaceSpec, data: Any) -> Any:
        """What a reader of a cached surface receives. A shared surface hands
        out the cached object itself, and every other hands out a deep
        copy."""
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

    This resolves a symlink, because the atomic writer resolves one too. A
    settings file that links into a managed config tree must read, write, lock
    and invalidate under one name.
    """
    return os.path.realpath(file_path)


# The process-wide settings store. A module singleton rather than a gl slot,
# for the reason the startup queue and the control plane are singletons. A
# named protocol should shrink the shared namespace.
_store = SettingsStore()


def get() -> SettingsStore:
    """The process-wide settings store. Never None."""
    return _store

# The typed views live in their own module, because the store and its views
# together grew past a reviewable size, and every caller still reaches them
# here. This import keeps "from src.backend.settings_store import AppSettings"
# working, along with DeckSettings, PluginSettings and SchemaView. It sits at
# the foot of the body because settings_views imports the surface specs and
# get() defined above, which must exist before it runs. Nothing imports
# settings_views directly. It is this module's back half, reached through
# here.
from src.backend.settings_views import (  # noqa: E402, F401
    AppSettings,
    DeckSettings,
    PluginSettings,
    SchemaView,
)

