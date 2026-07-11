import json
import os
import tempfile
import threading
import time
from loguru import logger as log

import globals as gl


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
    # Entries carry two clocks: "date" is LAST USE (refreshed on every open,
    # drives remove_old_cache_files eviction of unused entries) and "fetched"
    # is CONTENT AGE (stamped only after a write has fully committed, drives
    # the stale-fallback bound in StoreBackend.get_remote_file). Bounding
    # staleness on "date" would be circular: serving the stale copy would
    # keep renewing it.
    DAYS_TO_KEEP = 3

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
        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_guard = threading.Lock()

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
        with self.write_lock:
            os.makedirs(os.path.dirname(self.files_json), exist_ok=True)
            with open(self.files_json, "w") as f:
                json.dump(files.copy(), f, indent=4)

    def remove_old_cache_files(self):
        for string in self.files.copy():
            path = self.files[string].get("path")
            if not os.path.exists(path):
                continue
            date = self.files[string].get("date")
            if date is None:
                os.remove(path)
                self.files.pop(string)

            if time.time() - date > self.DAYS_TO_KEEP * 24 * 60 * 60:
                os.remove(path)
                self.files.pop(string)

        self.set_files(self.files)

    def create_cache_dirs(self):
        os.makedirs(self.CACHE_PATH, exist_ok=True)

    def create_cache_files(self):
        files = [self.files_json]

        for file in files:
            os.makedirs(os.path.dirname(file), exist_ok=True)
            if not os.path.exists(file):
                with open(file, "w") as f:
                    json.dump({}, f, indent=4)

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
            self.files[cache_string] = {
                "path": path,
                "date": time.time()
            }
            self.set_files(self.files)
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
        entry = self.files.get(cache_string, {})
        entry["path"] = cache_path
        entry["date"] = time.time()     # last use (eviction clock)
        entry["fetched"] = time.time()  # content age (staleness clock)
        self.files[cache_string] = entry
        self.set_files(self.files)

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
        # untouched by reads.
        entry = self.files.get(cache_string, {})
        entry["path"] = cache_path
        entry["date"] = time.time()
        self.files[cache_string] = entry
        self.set_files(self.files)

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
