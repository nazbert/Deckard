"""
Regression test for gl#25 -- StoreCache stamped entry["fetched"]=now and
persisted the index BEFORE the caller wrote a byte of content, and wrote
in-place with no per-file lock. A crash mid-write left a truncated file the
index swore was fresh, and get_remote_file's stale-fallback (782a1dac) then
served that poison for up to 3 days.

Now writes go to a sibling temp file, os.replace()d over the real path on
successful close, and "fetched" is stamped only after that commit; an
exception inside the caller's `with` block discards the temp file entirely.
Writers on the same cache key serialize on a per-file lock. Legacy entries
without "fetched" fall back to the cache file's mtime instead of the
ever-renewed "date" (the circular clock). All network-free.
"""
import asyncio
import os
import threading
import time

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError
from src.backend.Store.StoreCache import StoreCache

REPO = "https://github.com/StreamController/StreamController-Store"


def _tmp_leftovers(cache: StoreCache) -> list:
    if not os.path.isdir(cache.files_dir):
        return []
    return [n for n in os.listdir(cache.files_dir) if n.endswith(".tmp")]


def test_crash_mid_write_preserves_previous_content() -> None:
    cache = StoreCache()

    with cache.open_cache_file(url=REPO, path="Plugins.json", mode="w") as f:
        f.write("GOOD CONTENT")
    fetched_good = cache.get_fetched_date(url=REPO, path="Plugins.json")
    assert fetched_good is not None, "committed write must stamp fetched"

    time.sleep(0.05)

    try:
        with cache.open_cache_file(url=REPO, path="Plugins.json", mode="w") as f:
            f.write("TRUNC")  # partial write...
            raise RuntimeError("simulated crash mid-write")
    except RuntimeError:
        pass

    with cache.open_cache_file(url=REPO, path="Plugins.json", mode="r") as f:
        content = f.read()
    assert content == "GOOD CONTENT", (
        f"crashed write must not clobber the previous content, got {content!r}"
    )
    assert cache.get_fetched_date(url=REPO, path="Plugins.json") == fetched_good, (
        "crashed write must not renew the fetched stamp"
    )
    assert _tmp_leftovers(cache) == [], "aborted write must not leak temp files"


def test_fetched_stamped_only_after_close() -> None:
    cache = StoreCache()

    writer = cache.open_cache_file(url=REPO, path="Icons.json", mode="w")
    writer.write("half-way")
    assert cache.get_fetched_date(url=REPO, path="Icons.json") is None, (
        "fetched must NOT be stamped while the write is still in flight"
    )
    assert not cache.is_cached(url=REPO, path="Icons.json"), (
        "an in-flight first write must not present as cached"
    )
    writer.close()
    assert cache.get_fetched_date(url=REPO, path="Icons.json") is not None
    assert cache.is_cached(url=REPO, path="Icons.json")
    assert _tmp_leftovers(cache) == []


def test_concurrent_writers_serialize() -> None:
    cache = StoreCache()

    first = cache.open_cache_file(url=REPO, path="versions.json", mode="w")
    first.write("WRITER-A")

    second_done = threading.Event()

    def second_writer():
        with cache.open_cache_file(url=REPO, path="versions.json", mode="w") as f:
            f.write("WRITER-B")
        second_done.set()

    t = threading.Thread(target=second_writer, name="second_writer")
    t.start()

    time.sleep(0.2)
    assert not second_done.is_set(), (
        "second writer must block until the first writer on the same key closes"
    )

    first.close()
    assert second_done.wait(timeout=3.0), "second writer must proceed after the first closes"
    t.join(timeout=3.0)

    with cache.open_cache_file(url=REPO, path="versions.json", mode="r") as f:
        assert f.read() == "WRITER-B"
    assert _tmp_leftovers(cache) == []


def test_stale_fallback_serves_last_good_content() -> None:
    """End-to-end with get_remote_file: after a crashed refetch-write, a
    failing fetch must still fall back to the LAST GOOD copy."""
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    sb.store_cache = StoreCache()

    async def fetch_ok(url):
        class Resp:
            text = '[{"good": "catalog"}]'
            content = text.encode()
        return Resp()

    async def fetch_fail(url):
        return NoConnectionError()

    sb.request_from_url = fetch_ok
    first = asyncio.run(sb.get_remote_file(REPO, "Wallpapers.json", "main", force_refetch=True))
    assert first == '[{"good": "catalog"}]'

    # Crash a rewrite of the same key mid-write (bypassing get_remote_file,
    # which has no injectable seam mid-write): previous content must survive.
    try:
        with sb.store_cache.open_cache_file(url=REPO, path="Wallpapers.json", mode="w") as f:
            f.write('[{"trunc')
            raise RuntimeError("simulated crash mid-write")
    except RuntimeError:
        pass

    sb.request_from_url = fetch_fail
    second = asyncio.run(sb.get_remote_file(REPO, "Wallpapers.json", "main", force_refetch=True))
    assert second == '[{"good": "catalog"}]', (
        f"stale fallback must serve the last COMMITTED copy, got {second!r}"
    )


def test_legacy_entry_uses_mtime_not_renewed_date() -> None:
    cache = StoreCache()

    with cache.open_cache_file(url=REPO, path="Legacy.json", mode="w") as f:
        f.write("legacy content")

    key = cache.generate_cache_string(REPO, "Legacy.json")
    cache_path = cache.files[key]["path"]

    # Manufacture a pre-"fetched" entry whose content is old but whose
    # last-use "date" keeps getting renewed by reads.
    del cache.files[key]["fetched"]
    old_mtime = time.time() - 10 * 24 * 60 * 60  # 10 days old
    os.utime(cache_path, (old_mtime, old_mtime))
    with cache.open_cache_file(url=REPO, path="Legacy.json", mode="r") as f:
        f.read()  # renews "date"

    fetched = cache.get_fetched_date(url=REPO, path="Legacy.json")
    assert fetched is not None and abs(fetched - old_mtime) < 2.0, (
        f"legacy entry must report content age from mtime (~{old_mtime}), got {fetched!r} "
        "-- falling back to the renewed 'date' would make it eternally fresh"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_cache_atomic")
    test_crash_mid_write_preserves_previous_content()
    test_fetched_stamped_only_after_close()
    test_concurrent_writers_serialize()
    test_stale_fallback_serves_last_good_content()
    test_legacy_entry_uses_mtime_not_renewed_date()
    print("scenario_store_cache_atomic: PASS")


if __name__ == "__main__":
    main()
