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
* ``schema`` -- the table of defaults its readers fall back to. Reserved: no
  surface registered here carries one yet, and nothing in this module reads
  the field. A surface that gains one gains defaults applied at read and
  unknown keys refused at write, which is a decision per surface rather than
  one this module makes for all of them.

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
from dataclasses import dataclass
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
    #: describe. Nothing here reads it yet (see the module docstring).
    schema: Mapping[str, Any] | None = None
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

#: Per-deck settings, one file per serial. Cached because the render path
#: reads them repeatedly and the read is a JSON parse; deep-copied per read
#: because most callers mutate what they get and save it back.
DECK = SurfaceSpec(
    name="deck settings",
    path_fn=lambda serial: os.path.join(gl.DATA_PATH, "settings", "decks", f"{serial}.json"),
    root=dict,
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
