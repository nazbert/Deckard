import atexit
import json
import os
import tempfile
import threading
import time
import weakref
from loguru import logger as log

import globals as gl
from src.backend.atomic_json import atomic_write_json


# Every live StoreCache, weakly held so the exit hook below can drain
# deferred index writes without keeping instances alive (production has one,
# the test harness builds several per process).
_live_caches: "weakref.WeakSet[StoreCache]" = weakref.WeakSet()


@atexit.register
def _flush_live_caches() -> None:
    """Last-chance drain of every live cache's deferred index (issue #180).

    Covers plain interpreter exits: CLI runs, the test harness, an uncaught
    exception. The GTK app quits through os._exit(0) (src/app.py on_quit),
    which bypasses atexit entirely -- on_quit therefore calls
    StoreCache.flush_index() explicitly before it gets there.
    """
    for cache in list(_live_caches):
        try:
            cache.flush_index()
        except Exception as e:
            log.warning(f"Could not flush the store cache index at exit: {e}")


class _AtomicCacheWriter:
    """Write handle returned by StoreCache.open_cache_file for write modes.

    Content goes to a sibling temp file in the cache dir; a successful
    close() atomically os.replace()s it over the real cache path and only
    THEN invokes on_committed (which stamps the index's "fetched" clock).
    An exception inside the caller's `with` block -- or an explicit
    abort() -- discards the temp file, leaving the previous content and its
    honest stamp untouched. The old behavior (stamp-then-let-the-caller-
    write, directly into the real file) meant a crash mid-write left a
    truncated file that the index swore was fresh, and the stale-fallback
    then served that poison for up to DAYS_TO_KEEP.

    Holds the per-file lock handed in by StoreCache from construction until
    close/abort, so concurrent writers on the same cache key serialize
    instead of interleaving.
    """

    def __init__(self, final_path: str, mode: str, lock: threading.Lock, on_committed):
        self._final_path = final_path
        self._lock = lock
        self._on_committed = on_committed
        self._finished = False
        fd, self._tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(final_path),
            prefix=os.path.basename(final_path) + ".",
            suffix=".tmp",
        )
        try:
            self._file = os.fdopen(fd, mode)
        except Exception:
            os.close(fd)
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass
            raise

    def write(self, data):
        return self._file.write(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.abort()
        else:
            self.close()
        return False

    def abort(self) -> None:
        """Discard the pending write: previous cache content survives."""
        if self._finished:
            return
        self._finished = True
        try:
            self._file.close()
        finally:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass
            self._lock.release()

    def close(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            os.replace(self._tmp_path, self._final_path)
            # Stamp only now that the content is fully, atomically on disk.
            self._on_committed()
        except Exception:
            try:
                os.remove(self._tmp_path)
            except OSError:
                pass
            raise
        finally:
            self._lock.release()

    def __del__(self):
        # Dropped without close()/abort() (caller bug): never commit.
        if not getattr(self, "_finished", True):
            try:
                self.abort()
            except Exception:
                pass


class StoreCache:
    """files.json index + on-disk blobs for downloaded store files.

    Entries carry two clocks: "date" is LAST USE (refreshed on every open,
    drives remove_old_cache_files eviction of unused entries) and "fetched"
    is CONTENT AGE (stamped only after a write has fully committed, drives
    the stale-fallback bound in StoreBackend.get_remote_file). Bounding
    staleness on "date" would be circular: serving the stale copy would keep
    renewing it.

    Index persistence is split by what losing a write would cost (issue
    #180); the two halves are NOT interchangeable:

      * CONTENT commits stay SYNCHRONOUS. _stamp_committed (called only
        after a blob's os.replace has landed) and remove_old_cache_files
        (eviction) write files.json immediately. A lost "path"/"fetched"
        stamp orphans the blob forever: remove_old_cache_files only ever
        walks index entries, so a file with no entry is never aged out and
        never found again. This is the crash-safety result of gl#73/#25 and
        must not be deferred.

      * READ-CLOCK renewals are DEFERRED. The read path and the first
        sighting of a cache string only renew "date" in memory and mark the
        index dirty; a single daemon timer flushes the whole index
        FLUSH_DEBOUNCE_S later (armed on the first dirty mark, no-op while
        one is already pending). A warm store browse used to rewrite the
        entire files.json -- json.dump of every entry, fsync'd -- once per
        catalog file opened; it now costs one write per burst. A hard kill
        inside the window loses at most that much renewal, making an entry
        look FLUSH_DEBOUNCE_S older against a DAYS_TO_KEEP (3 day) eviction
        bound, and the next read renews it again.

    Entry mutations and the index write both happen under write_lock, so the
    flush's files.copy() can never run against a half-applied mutation.
    """

    DAYS_TO_KEEP = 3

    # Trailing debounce for the deferred read-clock writes. Overridable per
    # instance (the harness shortens it rather than sleeping seconds).
    FLUSH_DEBOUNCE_S = 2.0

    def __init__(self):
        self.CACHE_PATH = os.path.join(gl.DATA_PATH, "Store" , "cache")

        self.files_json = os.path.join(self.CACHE_PATH, "files.json")
        self.files_dir = os.path.join(self.CACHE_PATH, "files")

        self.write_lock = threading.Lock()

        # One lock per cache key, held for a writer's whole open->close
        # window (see _AtomicCacheWriter) so e.g. two store tabs force-
        # refetching versions.json can't interleave writes. Readers need no
        # lock: os.replace guarantees they see either the old or the new
        # complete file.
        #
        # This map is never evicted -- deliberately. It is keyed by cache
        # string (user::repo::branch::type::path), so its size is bounded by
        # the number of DISTINCT store files ever written this session (the
        # catalog: a few hundred entries, each a bare threading.Lock of tens
        # of bytes). Evicting a key is not worth the risk of dropping a lock
        # while a writer still holds it; the bound is the catalog, not user
        # input, so it cannot grow without limit.
        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_guard = threading.Lock()

        # Deferred read-clock index state; both fields are guarded by
        # write_lock. Set up before the first set_files() call below.
        self._index_dirty = False
        self._flush_timer: threading.Timer | None = None
        _live_caches.add(self)

        self.files = self.get_files()
        self.remove_old_cache_files()

        self.create_cache_dirs()
        self.create_cache_files()

    def get_files(self) -> dict:
        if not os.path.exists(self.files_json):
            return {}
        try:
            with open(self.files_json, "r") as f:
                return json.load(f)
        except json.decoder.JSONDecodeError as e:
            log.error(e)
            return {}

    def set_files(self, files: dict):
        """Persist the index NOW -- the synchronous half of the split
        documented on the class (content commits + eviction)."""
        with self.write_lock:
            self._write_index_locked(files)

    def _write_index_locked(self, files: dict = None) -> None:
        """Dump the index to disk. Caller must hold write_lock.

        Writing the live index also satisfies whatever the pending timer was
        going to flush, so the dirty flag clears and the armed timer becomes
        a no-op (it is not cancelled: a Timer cancel from an arbitrary
        caller thread buys nothing over a no-op wake-up in <=
        FLUSH_DEBOUNCE_S). A caller that passes some OTHER dict -- only the
        harness does -- has not persisted the live index, so the flag
        stands."""
        if files is None:
            files = self.files
        if files is self.files:
            self._index_dirty = False
        atomic_write_json(self.files_json, files.copy())

    def _mark_index_dirty_locked(self) -> None:
        """Note a deferred read-clock change and arm the trailing flush.
        Caller must hold write_lock (which the flush also takes, so the
        snapshot it writes can never catch a half-applied mutation)."""
        self._index_dirty = True
        if self._flush_timer is not None:
            return  # a flush is already pending; it will pick this up
        timer = threading.Timer(self.FLUSH_DEBOUNCE_S, self.flush_index)
        timer.name = "store-cache-index-flush"
        timer.daemon = True
        self._flush_timer = timer
        timer.start()

    def flush_index(self) -> None:
        """Write out any deferred read-clock renewals now.

        Called by the debounce timer, by the app's quit path, and by the
        module's atexit hook. A no-op when nothing is dirty, so calling it
        defensively costs nothing."""
        with self.write_lock:
            timer, self._flush_timer = self._flush_timer, None
            if self._index_dirty:
                self._write_index_locked()
        if timer is not None:
            # No-op when this IS the timer that just fired; cancels the
            # pending wake-up when an explicit flush beat it to the write.
            timer.cancel()

    def remove_old_cache_files(self):
        now = time.time()
        for string in self.files.copy():
            entry = self.files[string]
            path = entry.get("path")
            if not path or not os.path.exists(path):
                # The file is already gone (or the entry never recorded
                # one): drop the index entry too. Skipping it made the
                # entry immortal -- with no file to age out, no future
                # pass ever removed it.
                self.files.pop(string)
                continue
            # "date" is the last-use clock. Entries written before it
            # existed fall back to the content clocks -- "fetched", then
            # the file's mtime -- the same order get_fetched_date uses. A
            # legacy dateless entry used to fall through into `now - None`
            # and kill StoreCache.__init__ at startup.
            date = entry.get("date")
            if date is None:
                date = entry.get("fetched")
            if date is None:
                try:
                    date = os.path.getmtime(path)
                except OSError:
                    date = None

            if date is None or now - date > self.DAYS_TO_KEEP * 24 * 60 * 60:
                try:
                    os.remove(path)
                except OSError as e:
                    # Keep the entry: the next pass retries instead of
                    # orphaning the file on disk with no index record.
                    log.warning(f"Could not remove old cache file {path}: {e}")
                    continue
                self.files.pop(string)

        self.set_files(self.files)

    def create_cache_dirs(self):
        os.makedirs(self.CACHE_PATH, exist_ok=True)

    def create_cache_files(self):
        files = [self.files_json]

        for file in files:
            if not os.path.exists(file):
                atomic_write_json(file, {})

    def get_user_name(self, repo_url:str) -> str:
        splitted =  repo_url.split("/")
        domain = "github.com"
        if domain not in splitted:
            domain = "raw.githubusercontent.com"

        return splitted[splitted.index(domain)+1]

    def get_repo_name(self, repo_url:str) -> str:
        github_split = repo_url.split("github")
        if len(github_split) < 2:
            return
        split = github_split[1].split("/")
        if len(split) < 3:
            return
        return split[2]

    def generate_cache_string(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> str:
        user = self.get_user_name(url)
        repo = self.get_repo_name(url)
        return f"{user}::{repo}::{branch}::{data_type}::{path}"

    def get_cache_path(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> str:
        # return os.path.join(self.files_dir, self.generate_cache_string(url, path, branch, data_type))

        cache_string = self.generate_cache_string(url, path, branch, data_type)
        if cache_string in self.files:
            return self.files[cache_string].get("path")

        else:
            path = os.path.join(self.files_dir, cache_string)
            # First sighting of this cache string: records only where the
            # blob WOULD live plus the last-use clock -- there is no content
            # yet, and the write that creates it stamps the index
            # synchronously (_stamp_committed). Deferred; see the class doc.
            with self.write_lock:
                self.files[cache_string] = {
                    "path": path,
                    "date": time.time()
                }
                self._mark_index_dirty_locked()
            return path

    def is_cached(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> bool:
        cache_string = self.generate_cache_string(url, path, branch, data_type)
        if cache_string not in self.files:
            return False

        if self.files[cache_string].get("path") is None:
            return False

        return os.path.exists(self.files[cache_string].get("path"))

    def _get_file_lock(self, cache_string: str) -> threading.Lock:
        with self._file_locks_guard:
            return self._file_locks.setdefault(cache_string, threading.Lock())

    def _stamp_committed(self, cache_string: str, cache_path: str) -> None:
        """Index update for a fully committed write -- called by the atomic
        writer AFTER os.replace has landed the content, never before."""
        with self.write_lock:
            entry = self.files.get(cache_string, {})
            entry["path"] = cache_path
            entry["date"] = time.time()     # last use (eviction clock)
            entry["fetched"] = time.time()  # content age (staleness clock)
            self.files[cache_string] = entry
            # Synchronous, and under the same lock acquisition as the
            # mutation: losing this record orphans the blob that just
            # landed (class doc). Never route it through the debounce.
            self._write_index_locked()

    def open_cache_file(self, url: str, path: str, branch: str = "main", data_type: str = "text", mode: str = "r"):
        cache_path = self.get_cache_path(url, path, branch, data_type)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        cache_string = self.generate_cache_string(url, path, branch, data_type)

        if any(flag in mode for flag in ("w", "a", "x", "+")):
            if mode not in ("w", "wb"):
                # Append/update modes can't be expressed as a fresh-temp +
                # atomic-replace; no caller uses them. Fail loud rather than
                # silently reintroducing in-place writes.
                raise ValueError(f"unsupported cache write mode {mode!r}: only 'w'/'wb' are supported")
            lock = self._get_file_lock(cache_string)
            lock.acquire()
            try:
                return _AtomicCacheWriter(
                    cache_path, mode, lock,
                    on_committed=lambda: self._stamp_committed(cache_string, cache_path),
                )
            except Exception:
                lock.release()
                raise

        # Read: renew only the last-use clock; "fetched" (content age) is
        # untouched by reads. Deferred behind the debounce -- this used to
        # rewrite all of files.json on every cache HIT (issue #180).
        with self.write_lock:
            entry = self.files.get(cache_string, {})
            entry["path"] = cache_path
            entry["date"] = time.time()
            self.files[cache_string] = entry
            self._mark_index_dirty_locked()

        return open(cache_path, mode)

    def get_fetched_date(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> float:
        """When the cached content was last WRITTEN; None if unknown.
        Entries predating the "fetched" field fall back to the cache file's
        mtime (reads never touch it; os.replace carries the temp file's
        write time) -- NOT the index's "date", which every read renews and
        would keep a legacy entry eternally "fresh" to the stale-fallback."""
        entry = self.files.get(self.generate_cache_string(url, path, branch, data_type), {})
        fetched = entry.get("fetched")
        if fetched is not None:
            return fetched
        cache_path = entry.get("path")
        if cache_path and os.path.exists(cache_path):
            try:
                return os.path.getmtime(cache_path)
            except OSError:
                return None
        return None
