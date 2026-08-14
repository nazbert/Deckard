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
from src.backend.Store.StoreURL import parse_repo_url


# Every live StoreCache, held weakly, so the exit hook below drains deferred
# index writes and keeps no instance alive. Production builds one instance and
# the test harness builds several per process.
_live_caches: "weakref.WeakSet[StoreCache]" = weakref.WeakSet()


@atexit.register
def _flush_live_caches() -> None:
    """Last drain of every live cache's deferred index.

    This covers a plain interpreter exit, which a CLI run, the test harness
    and an uncaught exception all take. The GTK app quits through os._exit(0)
    (src/app.py on_quit), which skips atexit, so on_quit calls
    StoreCache.flush_index() itself before it gets there.
    """
    for cache in list(_live_caches):
        try:
            cache.flush_index()
        except Exception as e:
            log.warning(f"Could not flush the store cache index at exit: {e}")


class _AtomicCacheWriter:
    """Write handle that StoreCache.open_cache_file returns for a write mode.

    The content goes to a sibling temp file in the cache directory. A
    successful close() replaces the real cache path with it through
    os.replace, and calls on_committed only afterwards, which stamps the
    index's "fetched" clock. An exception inside the caller's with block, or
    an explicit abort(), discards the temp file and leaves the previous
    content and its stamp. A stamp before the write would let a crash
    mid-write leave a truncated file that the index calls fresh, and the
    stale fallback would serve it for up to DAYS_TO_KEEP.

    This holds the per-file lock that StoreCache hands in, from construction
    until close or abort, so two writers on one cache key serialize.
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
        """Discard the pending write. The previous cache content survives."""
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
            # Stamp only now that the whole content sits on disk.
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
        # A caller dropped this handle without close() or abort(). Never
        # commit such a write.
        if not getattr(self, "_finished", True):
            try:
                self.abort()
            except Exception:
                pass


class StoreCache:
    """The files.json index and the on-disk blobs for downloaded store files.

    Each entry carries two clocks. "date" records the last use. Every open
    renews it, and remove_old_cache_files evicts an unused entry by it.
    "fetched" records the content age. A write stamps it only after the
    commit, and it bounds the stale fallback in
    StoreBackend.get_remote_file. A staleness bound on "date" would be
    circular, because serving the stale copy renews that clock.

    The index persists in two halves, split by the cost of a lost write. The
    two halves are not interchangeable.

    A content commit writes synchronously. _stamp_committed runs after a
    blob's os.replace lands, and remove_old_cache_files evicts, and both
    write files.json at once. A lost "path" or "fetched" stamp orphans the
    blob forever, because remove_old_cache_files walks index entries only, so
    no pass ages out a file with no entry and nothing finds it again. Crash
    safety demands this write, so it must not defer.

    A read-clock renewal defers. The read path, and the first sighting of a
    cache string, renew "date" in memory and mark the index dirty. One daemon
    timer flushes the whole index FLUSH_DEBOUNCE_S later. The first dirty
    mark arms that timer, and a later mark leaves the pending timer alone. A
    synchronous renewal would rewrite the whole files.json, with a json.dump
    of every entry and an fsync, once per catalog file opened. This costs one
    write per burst. A hard kill inside the window loses that much renewal,
    which makes an entry look FLUSH_DEBOUNCE_S older against a DAYS_TO_KEEP
    (3 day) eviction bound, and the next read renews it again.

    An entry mutation and the index write both run under write_lock. The
    hazard is not the top-level files.copy(), because dict.copy() is one
    atomic operation. files.copy() is shallow, so json.dump then iterates the
    same per-entry dicts that the read and commit paths mutate. A thread that
    adds "fetched" to an entry mid-dump raises "dictionary changed size
    during iteration", and can truncate the index to what atomic_write_json
    buffered. remove_old_cache_files is the one exception, and it pops
    entries without the lock. It runs from __init__ only, before another
    thread can reach the instance and before a flush timer can exist.
    """

    DAYS_TO_KEEP = 3

    # Trailing debounce for the deferred read-clock writes. An instance can
    # override it, and the harness shortens it rather than sleep for seconds.
    FLUSH_DEBOUNCE_S = 2.0

    def __init__(self):
        self.CACHE_PATH = os.path.join(gl.DATA_PATH, "Store" , "cache")

        self.files_json = os.path.join(self.CACHE_PATH, "files.json")
        self.files_dir = os.path.join(self.CACHE_PATH, "files")

        self.write_lock = threading.Lock()

        # One lock per cache key. A writer holds it from open to close (see
        # _AtomicCacheWriter), so two store tabs that force a refetch of
        # versions.json cannot interleave their writes. A reader needs no
        # lock, because os.replace shows it the old file or the new complete
        # file.
        #
        # Nothing evicts this map. Its key is the cache string
        # (user::repo::branch::type::path), so the number of distinct store
        # files written this session bounds its size. The catalog holds a few
        # hundred entries, and each lock costs tens of bytes. An eviction
        # risks dropping a lock while a writer still holds it, and the catalog
        # bounds the growth rather than user input.
        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_guard = threading.Lock()

        # Deferred read-clock index state. write_lock guards both fields.
        # Set them up before the first set_files() call below.
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
        """Persist the index at once. This is the synchronous half of the
        split that the class docstring describes, for a content commit and
        for an eviction.

        A foreign dict, which is anything other than self.files, does not
        persist. It lands on disk, but the deferred renewals stay dirty, so
        the next flush overwrites the file with the live index. Every
        production caller passes self.files, and the foreign-dict form serves
        a test that pokes the writer directly."""
        with self.write_lock:
            self._write_index_locked(files)

    def _write_index_locked(self, files: dict = None) -> None:
        """Dump the index to disk. The caller must hold write_lock.

        A write of the live index also covers what the pending timer would
        flush, so the dirty flag clears and the armed timer then does
        nothing. This does not cancel that timer, because a Timer cancel from
        an arbitrary thread gains nothing over one idle wake-up within
        FLUSH_DEBOUNCE_S. The flag clears only after the write returns. A
        write that raises (ENOSPC, a read-only filesystem) must leave the
        index dirty, so the next flush from the timer, the quit path or the
        exit hook retries the renewals rather than drop them. A caller that
        passes another dict, which only the harness does, persists no live
        index, so the flag stands."""
        if files is None:
            files = self.files
        atomic_write_json(self.files_json, files.copy())
        if files is self.files:
            self._index_dirty = False

    def _mark_index_dirty_locked(self) -> None:
        """Note a deferred read-clock change and arm the trailing flush. The
        caller must hold write_lock. The flush takes it too, so json.dump
        never iterates an entry dict during a mutation."""
        self._index_dirty = True
        if self._flush_timer is not None:
            return  # A flush is already pending and covers this mark.
        timer = threading.Timer(self.FLUSH_DEBOUNCE_S, self.flush_index)
        timer.name = "store-cache-index-flush"
        timer.daemon = True
        try:
            timer.start()
        except Exception as e:
            # Thread exhaustion (RuntimeError, can't start new thread) must
            # not park a dead Timer in the slot. Every later mark would read
            # that as a pending flush and the debounce would die for the rest
            # of the process. A _flush_timer left None lets the next dirty
            # mark arm again. The dirty flag still stands, so even a run of
            # failed arms loses nothing, because the quit path and the atexit
            # hook still drain it.
            log.warning(f"Could not arm the store cache index flush: {e}")
            return
        # Assign only once the thread runs. The live timer is safe, because
        # the body of flush_index needs write_lock, which this caller holds
        # past this assignment.
        self._flush_timer = timer

    def flush_index(self) -> None:
        """Write out any deferred read-clock renewals now.

        The debounce timer, the app's quit path and the module's atexit hook
        all call this. It does nothing while the index is clean, so an extra
        call costs nothing."""
        with self.write_lock:
            timer, self._flush_timer = self._flush_timer, None
            if self._index_dirty:
                self._write_index_locked()
        if timer is not None:
            # This does nothing for the timer that just fired, and cancels
            # the pending wake-up when an explicit flush wrote first.
            timer.cancel()

    def remove_old_cache_files(self):
        now = time.time()
        for string in self.files.copy():
            entry = self.files[string]
            path = entry.get("path")
            if not path or not os.path.exists(path):
                # The file is gone, or the entry never recorded one. Drop the
                # index entry too. A skip here makes the entry permanent,
                # because no file remains to age out and no later pass
                # removes it.
                self.files.pop(string)
                continue
            # "date" is the last-use clock. An entry written before that
            # field existed falls back to the content clocks, first "fetched"
            # and then the file's mtime, the order get_fetched_date uses. An
            # old entry with no date otherwise reaches "now - None" and
            # kills StoreCache.__init__ at startup.
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
                    # Keep the entry, so the next pass retries. A dropped
                    # entry orphans the file on disk with no index record.
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
        ref = parse_repo_url(repo_url)
        if ref is None:
            # A caller reaches the cache only after its entry passes
            # parse_repo_url, so this means a broken caller and not bad user
            # input. Name the url, which "x not in list" never did.
            raise ValueError(f"Not a store repository url: {repo_url!r}")
        return ref.user

    def get_repo_name(self, repo_url:str) -> str | None:
        ref = parse_repo_url(repo_url)
        return None if ref is None else ref.repo

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
            # The first sighting of this cache string. Record where the blob
            # goes, plus the last-use clock. No content exists yet, and the
            # write that creates it stamps the index synchronously through
            # _stamp_committed. This record defers; see the class docstring.
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
        """Index update for a committed write. The atomic writer calls this
        after os.replace lands the content, and never before."""
        with self.write_lock:
            entry = self.files.get(cache_string, {})
            entry["path"] = cache_path
            entry["date"] = time.time()     # last use (eviction clock)
            entry["fetched"] = time.time()  # content age (staleness clock)
            self.files[cache_string] = entry
            # Write synchronously, under the same lock hold as the mutation.
            # A lost record orphans the blob that just landed (see the class
            # docstring). Never route this write through the debounce.
            self._write_index_locked()

    def open_cache_file(self, url: str, path: str, branch: str = "main", data_type: str = "text", mode: str = "r"):
        cache_path = self.get_cache_path(url, path, branch, data_type)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        cache_string = self.generate_cache_string(url, path, branch, data_type)

        if any(flag in mode for flag in ("w", "a", "x", "+")):
            if mode not in ("w", "wb"):
                # An append or update mode does not fit a fresh temp file
                # plus an atomic replace, and no caller uses one. Fail loud
                # rather than write in place again.
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

        # A read renews the last-use clock only, and leaves "fetched", the
        # content age, alone. The renewal defers behind the debounce, so a
        # cache hit does not rewrite the whole files.json.
        with self.write_lock:
            entry = self.files.get(cache_string, {})
            entry["path"] = cache_path
            entry["date"] = time.time()
            self.files[cache_string] = entry
            self._mark_index_dirty_locked()

        return open(cache_path, mode)

    def get_fetched_date(self, url: str, path: str, branch: str = "main", data_type: str = "text") -> float | None:
        """When a write last landed the cached content, or None when that is
        unknown. An entry older than the "fetched" field falls back to the
        cache file's mtime, which a read never touches and which os.replace
        carries over from the temp file. It must not fall back to the index
        clock "date", which every read renews and which would keep an old
        entry fresh for the stale fallback."""
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
