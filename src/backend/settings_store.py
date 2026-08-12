"""
One owner for the app's settings files.

A settings surface is a JSON file the app itself owns: the deck settings under
``settings/decks/``, the asset library index, the app settings, the page
manager's own bookkeeping. Each of them needs the same three answers -- where
does it live, what happens when it is corrupt, and who may write it -- and each
of them used to answer separately. The answers had drifted: one surface was
healed and quarantined by a loader, another was read with a bare ``json.load``
inside a constructor and took the whole app down with it, and a third was
written straight past the cache that other readers were served from.

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
pretending it is empty would invite a write that destroys it.

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
    """

    def __init__(self, data: dict, schema: Mapping[str, Any]):
        self.data: dict = data
        self.schema: Mapping[str, Any] = schema

    # -- reads --------------------------------------------------------- #

    def get(self, name: str, key: str | None = None) -> Any:
        """One setting: what is stored, or the schema's default for it.

        ``key`` names a setting inside the section ``name``; omit it for a
        top-level one. A name the schema does not describe raises here rather
        than reading as None for the rest of the program's life.
        """
        if key is None:
            default = self._top_level(name)
            return _copied(self.data[name]) if name in self.data else _copied(default)
        defaults = self._section_defaults(name)
        if key not in defaults:
            raise KeyError(f"{name}.{key} is not in this schema")
        stored = self._stored_section(name)
        return _copied(stored[key]) if key in stored else _copied(defaults[key])

    def section(self, name: str) -> dict[str, Any]:
        """Section ``name`` with every absent key filled from the schema.

        The shape a load path destructures. A copy, and deliberately so:
        mutating it reaches neither the schema nor the stored settings, and
        saving it back would persist exactly the defaults it just filled in.
        Keys stored but not described are kept -- this fills gaps, it does not
        prune a file it did not write.
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
        ``write`` exactly as an uncached read must be.
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

        resolved = _resolve(path)
        with self._cache_lock:
            if resolved in self._cache:
                return copy.deepcopy(self._cache[resolved]), False
            # The invalidation count as of the miss. Recorded under the same
            # lock the miss was decided under, so a write that lands from here
            # on is guaranteed to move it.
            generation = self._invalidations.get(resolved, 0)

        # Parsed outside the lock: a parse must never block another surface's
        # reader.
        data, corrupt = self.load_file(path, root=spec.root)

        with self._cache_lock:
            if self._invalidations.get(resolved, 0) == generation:
                self._cache[resolved] = data
            # Otherwise a write landed while this read was in the file: what
            # was just parsed is the content from BEFORE that write, and
            # caching it would leave every later reader on the old settings
            # with nothing to correct it. Dropped instead -- this caller still
            # gets the content it read, which is a read that happened before
            # the write, and the next reader loads afresh.
        return copy.deepcopy(data), corrupt

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
            # Counted even when nothing was cached: a reader may be between
            # its miss and its store right now, holding content this write has
            # just superseded, and dropping an entry that does not exist yet
            # is the one thing a cache cannot do.
            self._invalidations[resolved] = self._invalidations.get(resolved, 0) + 1

    # -- internals ----------------------------------------------------- #

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
