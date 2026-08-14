"""
Regression test for StoreCache.remove_old_cache_files.

A missing-path entry is purged from the index. A legacy entry with no "date"
falls back to the content clocks, "fetched" and then file mtime, so a fresh
legacy file survives the pass. A failed os.remove is logged and retried on
the next pass instead of raising out of StoreCache.__init__.
"""
import json
import os
import time

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreCache import StoreCache

REPO = "https://github.com/StreamController/StreamController-Store"

DAY = 24 * 60 * 60


def _fresh_cache_with_entry(path_name: str, content: str = "x") -> tuple[StoreCache, str, str]:
    cache = StoreCache()
    with cache.open_cache_file(url=REPO, path=path_name, mode="w") as f:
        f.write(content)
    key = cache.generate_cache_string(REPO, path_name)
    return cache, key, cache.files[key]["path"]


def test_legacy_without_date_survives() -> None:
    """A pre-"date" index entry over a fresh file must neither crash the
    eviction pass nor be evicted."""
    cache, key, cache_path = _fresh_cache_with_entry("Legacy.json", "legacy content")

    # Manufacture a legacy entry with no "date" and no "fetched".
    del cache.files[key]["date"]
    cache.files[key].pop("fetched", None)
    cache.set_files(cache.files)

    cache.remove_old_cache_files()  # an unguarded None date raises TypeError

    assert key in cache.files, "fresh legacy entry must survive the eviction pass"
    assert os.path.exists(cache_path), "fresh legacy entry's file must survive"

    # A re-init, which is the real startup path, must not raise either.
    reloaded = StoreCache()
    assert key in reloaded.files


def test_legacy_old_mtime_evicted() -> None:
    """The mtime fallback must still EVICT a genuinely old legacy file."""
    cache, key, cache_path = _fresh_cache_with_entry("OldLegacy.json")

    del cache.files[key]["date"]
    cache.files[key].pop("fetched", None)
    old = time.time() - (StoreCache.DAYS_TO_KEEP + 7) * DAY
    os.utime(cache_path, (old, old))

    cache.remove_old_cache_files()

    assert key not in cache.files, "stale legacy entry must be evicted"
    assert not os.path.exists(cache_path), "stale legacy entry's file must be removed"


def test_missing_path_entry_purged() -> None:
    cache, key, cache_path = _fresh_cache_with_entry("Ghost.json")
    os.remove(cache_path)

    cache.remove_old_cache_files()

    assert key not in cache.files, (
        "an index entry whose file is gone must be purged, not kept forever"
    )
    # The purge must be persisted, or the next startup resurrects it.
    with open(cache.files_json) as f:
        assert key not in json.load(f)


def test_null_path_entry_purged() -> None:
    cache = StoreCache()
    cache.files["corrupt::entry"] = {"path": None, "date": time.time()}
    cache.set_files(cache.files)

    cache.remove_old_cache_files()  # a None path raises from os.path.exists

    assert "corrupt::entry" not in cache.files


def test_failed_remove_is_retried() -> None:
    """A raising os.remove must not kill the pass. The entry stays, so the
    next pass retries the eviction."""
    cache, key, cache_path = _fresh_cache_with_entry("Stubborn.json")
    cache.files[key]["date"] = time.time() - (StoreCache.DAYS_TO_KEEP + 7) * DAY
    cache.set_files(cache.files)

    real_remove = os.remove

    def failing_remove(path, *a, **kw):
        if path == cache_path:
            raise PermissionError(f"simulated EPERM for {path}")
        return real_remove(path, *a, **kw)

    os.remove = failing_remove
    try:
        cache.remove_old_cache_files()  # must not raise
    finally:
        os.remove = real_remove

    assert key in cache.files, "entry must survive a failed remove for a later retry"

    cache.remove_old_cache_files()
    assert key not in cache.files, "retry with a working os.remove must evict"
    assert not os.path.exists(cache_path)


def test_ordinary_old_entry_still_evicted() -> None:
    """The pass still evicts an ordinary old entry."""
    cache, key, cache_path = _fresh_cache_with_entry("Plain.json")
    cache.files[key]["date"] = time.time() - (StoreCache.DAYS_TO_KEEP + 1) * DAY
    cache.set_files(cache.files)

    cache.remove_old_cache_files()

    assert key not in cache.files
    assert not os.path.exists(cache_path)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_cache_eviction")
    test_legacy_without_date_survives()
    test_legacy_old_mtime_evicted()
    test_missing_path_entry_purged()
    test_null_path_entry_purged()
    test_failed_remove_is_retried()
    test_ordinary_old_entry_still_evicted()
    print("scenario_store_cache_eviction: PASS")


if __name__ == "__main__":
    main()
