"""
Regression test -- StoreCache rewrote the WHOLE files.json
index (json.dump of every entry, fsync'd, on the calling thread) on every
cache READ, just to renew the entry's last-use clock. A warm store browse
that opened N catalog files performed N full index dumps.

Now the read path (and the first sighting of a cache string) mutates the
in-memory entry and marks the index dirty; one daemon timer flushes the
whole index once, FLUSH_DEBOUNCE_S later. The synchronous half is
deliberately NOT deferred: a committed blob write stamps the index before
_AtomicCacheWriter.close() returns, because a lost "path"/"fetched" record
orphans the blob forever (remove_old_cache_files only walks index entries),
whereas a lost "date" renewal just makes an entry look a couple of seconds
older against a 3-day eviction bound.

Writes are counted by wrapping StoreCache's atomic_write_json reference; the
debounce is shortened per instance instead of sleeping the production 2s.
All network-free.
"""
import atexit
import json
import os
import threading
import time

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

import src.backend.Store.StoreCache as store_cache_mod
from src.backend.Store.StoreCache import StoreCache

REPO = "https://github.com/StreamController/StreamController-Store"

# Short enough to keep the scenario fast, long enough that the "no write
# happened YET" assertions cannot be raced by the timer on a loaded machine
# (run_all.py runs scenarios in parallel subprocesses).
DEBOUNCE = 1.0
SETTLE = DEBOUNCE * 1.5  # comfortably past an armed window

# Size of the simulated store browse: this many cache hits used to mean this
# many full files.json dumps.
BURST = 12


class _IndexWriteCounter:
    """Counts atomic_write_json calls aimed at the cache index, delegating
    to the real writer so the on-disk file stays truthful (the assertions
    read it back)."""

    def __init__(self):
        self._real = store_cache_mod.atomic_write_json
        self._lock = threading.Lock()
        self.index_writes = 0
        self.index_path = None  # set once a cache exists

    def install(self) -> None:
        store_cache_mod.atomic_write_json = self

    def __call__(self, file_path, data, *args, **kwargs):
        if self.index_path is not None and file_path == self.index_path:
            with self._lock:
                self.index_writes += 1
        return self._real(file_path, data, *args, **kwargs)

    def reset(self) -> None:
        with self._lock:
            self.index_writes = 0

    @property
    def count(self) -> int:
        with self._lock:
            return self.index_writes


COUNTER = _IndexWriteCounter()
COUNTER.install()


def _new_cache(debounce: float = DEBOUNCE) -> StoreCache:
    cache = StoreCache()
    cache.FLUSH_DEBOUNCE_S = debounce  # instance shadow of the class default
    COUNTER.index_path = cache.files_json
    return cache


def _seed(cache: StoreCache, names: list) -> None:
    """Commit real content for each name (synchronous stamps)."""
    for name in names:
        with cache.open_cache_file(url=REPO, path=name, mode="w") as f:
            f.write(f"content of {name}")


def _read(cache: StoreCache, name: str) -> str:
    with cache.open_cache_file(url=REPO, path=name, mode="r") as f:
        return f.read()


def _on_disk(cache: StoreCache) -> dict:
    with open(cache.files_json, "r") as f:
        return json.load(f)


def test_read_burst_writes_the_index_once() -> None:
    """(a) N reads => ZERO immediate index writes, then exactly one flush.
    Before the deferred index this was N writes, all synchronous on the caller."""
    cache = _new_cache()
    names = [f"Catalog{i}.json" for i in range(BURST)]
    _seed(cache, names)

    COUNTER.reset()
    for name in names:
        assert _read(cache, name) == f"content of {name}"

    assert COUNTER.count == 0, (
        f"{BURST} cache reads must perform ZERO synchronous index writes, "
        f"got {COUNTER.count}"
    )
    assert fixtures.wait_until(lambda: COUNTER.count >= 1, timeout=5.0), (
        "the debounced flush must land after the window"
    )
    # Let a second (wrongly armed) timer have time to fire before asserting.
    time.sleep(SETTLE)
    assert COUNTER.count == 1, (
        f"the whole burst must collapse into exactly ONE index write, got {COUNTER.count}"
    )


def test_deferral_is_real_and_renewed_date_lands() -> None:
    """(e) The flush actually carries the renewed last-use clock -- and
    before it fires, the on-disk "date" is still the OLD one (proof the
    deferral is real, not just write-count bookkeeping). "fetched" must not
    be blurred into "date" by any of this."""
    cache = _new_cache()
    _seed(cache, ["Renewed.json"])
    key = cache.generate_cache_string(REPO, "Renewed.json")

    seeded = _on_disk(cache)[key]
    seeded_date, seeded_fetched = seeded["date"], seeded["fetched"]

    time.sleep(0.05)  # make the renewal distinguishable from the seed stamp
    COUNTER.reset()
    _read(cache, "Renewed.json")

    assert cache.files[key]["date"] > seeded_date, "the read must renew the in-memory clock"
    assert _on_disk(cache)[key]["date"] == seeded_date, (
        "the renewal must NOT have hit the disk yet -- that is the whole point"
    )

    assert fixtures.wait_until(lambda: COUNTER.count >= 1, timeout=5.0)
    flushed = _on_disk(cache)[key]
    assert flushed["date"] == cache.files[key]["date"], (
        f"the flushed index must carry the renewed date, got {flushed['date']} "
        f"vs in-memory {cache.files[key]['date']}"
    )
    assert flushed["fetched"] == seeded_fetched, (
        "a read must never touch the content-age clock"
    )


def test_committed_write_stamps_synchronously() -> None:
    """(b) The content half stays synchronous: the index records the blob
    before close() returns, so a crash right after can never orphan it."""
    cache = _new_cache()
    COUNTER.reset()

    writer = cache.open_cache_file(url=REPO, path="Committed.json", mode="w")
    assert COUNTER.count == 0, (
        "opening a writer (first sighting of the key) must not write the index"
    )
    writer.write("payload")
    assert COUNTER.count == 0, "an in-flight write must not stamp the index"

    writer.close()
    assert COUNTER.count == 1, (
        f"the commit must stamp the index synchronously, got {COUNTER.count} writes"
    )

    key = cache.generate_cache_string(REPO, "Committed.json")
    entry = _on_disk(cache).get(key, {})
    assert entry.get("fetched") is not None, "committed entry must be on disk with its stamp"
    assert os.path.exists(entry.get("path", "")), "and must point at the landed blob"

    # The synchronous dump also satisfies the dirt the miss path marked, so
    # the timer it armed fires as a no-op instead of writing a second time.
    time.sleep(SETTLE)
    assert COUNTER.count == 1, (
        f"a pending flush must be subsumed by the synchronous dump, got {COUNTER.count}"
    )


def test_second_burst_rearms() -> None:
    """(c) The timer is not one-shot-per-process: reads after a flush arm a
    fresh one, and collapse into a single write again."""
    cache = _new_cache()
    names = [f"Rearm{i}.json" for i in range(4)]
    _seed(cache, names)

    for burst in (1, 2):
        COUNTER.reset()
        for name in names:
            _read(cache, name)
        assert COUNTER.count == 0, f"burst {burst} must defer its writes"
        assert fixtures.wait_until(lambda: COUNTER.count >= 1, timeout=5.0), (
            f"burst {burst} must still be flushed"
        )
        time.sleep(SETTLE)
        assert COUNTER.count == 1, (
            f"burst {burst} must collapse into one write, got {COUNTER.count}"
        )


def test_read_burst_arms_exactly_one_timer() -> None:
    """(f) The debounce dedupes: a burst arms ONE timer, not one per read.

    The write-count checks above cannot see this on their own -- N timers
    all firing inside the same window still collapse to one write once the
    first has cleared the dirty flag. What N timers do cost is N *threads*
    per burst, which is the regression this pins: the debounce runs on a
    threading.Timer, so dropping the "already pending" guard turns a warm
    catalog browse into a thread storm.

    Debounce long enough that nothing can fire mid-burst, so every timer
    this leg arms is still countable at the end."""
    def _armed_flush_threads() -> list:
        return [t for t in threading.enumerate()
                if t.name == "store-cache-index-flush" and t.is_alive()]

    # The earlier legs ran on a ~1s debounce and have all fired by now; wait
    # the last of those threads out so the count below is absolute.
    assert fixtures.wait_until(lambda: not _armed_flush_threads(), timeout=5.0), (
        f"stale flush timers from an earlier check: {_armed_flush_threads()!r}"
    )

    cache = _new_cache(debounce=600.0)
    names = [f"Dedupe{i}.json" for i in range(BURST)]
    _seed(cache, names)  # first sighting of Dedupe0 arms the one timer

    armed_timer = cache._flush_timer
    assert armed_timer is not None, "the first cache-string sighting must arm a flush"

    for name in names:
        _read(cache, name)
        assert cache._flush_timer is armed_timer, (
            "every read in the burst must ride the timer that is already "
            "armed -- a fresh threading.Timer per read is a thread per cache "
            "hit, which is exactly what the debounce exists to avoid"
        )

    armed = len(_armed_flush_threads())
    assert armed == 1, (
        f"a {BURST}-file seed plus a {BURST}-read burst must leave exactly "
        f"ONE flush timer thread armed, got {armed}"
    )

    cache.flush_index()  # leave nothing pending for the later checks
    assert cache._flush_timer is None


def test_exit_hook_drains_a_dirty_index() -> None:
    """(d) A quit inside the debounce window still persists the renewals:
    the module's atexit hook (and the explicit flush the app's os._exit path
    calls) drains every live cache."""
    # A debounce no test can outwait, so ONLY the exit hook can flush here.
    cache = _new_cache(debounce=600.0)
    _seed(cache, ["Draining.json"])
    key = cache.generate_cache_string(REPO, "Draining.json")

    # Settle any dirt left by earlier caches in this process (they share one
    # files.json), so the counts below are attributable to this one.
    store_cache_mod._flush_live_caches()
    time.sleep(0.05)
    COUNTER.reset()

    _read(cache, "Draining.json")
    assert COUNTER.count == 0, "the read must be deferred behind the long debounce"
    renewed = cache.files[key]["date"]

    store_cache_mod._flush_live_caches()
    assert COUNTER.count == 1, (
        f"the exit hook must drain the dirty index, got {COUNTER.count} writes"
    )
    assert _on_disk(cache)[key]["date"] == renewed, (
        "the drained index must carry the renewal"
    )

    store_cache_mod._flush_live_caches()
    assert COUNTER.count == 1, "a clean index must not be rewritten by a second drain"

    # An explicit flush cancels the pending timer, so nothing fires later.
    assert cache._flush_timer is None, "flush_index must clear the armed timer"


def test_exit_hook_is_registered_with_atexit() -> None:
    """(g) ...and that drain is actually wired into interpreter exit.

    The check above calls _flush_live_caches() directly, so it stays green
    even if the @atexit.register decorator is dropped -- at which point a
    plain CLI/harness exit silently loses every deferred renewal (the GTK app
    has its own explicit flush in on_quit, which os._exit(0) forces it to
    need). atexit exposes no way to enumerate its table, so probe it: an
    unregister that actually removes something is proof it was in there.
    """
    before = atexit._ncallbacks()
    atexit.unregister(store_cache_mod._flush_live_caches)
    after = atexit._ncallbacks()
    if after == before - 1:
        atexit.register(store_cache_mod._flush_live_caches)  # put it back

    assert after == before - 1, (
        "StoreCache._flush_live_caches must be registered with atexit: "
        "unregistering it left the callback count unchanged "
        f"({before} -> {after}), so nothing drains the deferred index on a "
        "plain interpreter exit"
    )
    assert atexit._ncallbacks() == before, "the probe must restore the hook"


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_cache_flush")
    test_read_burst_writes_the_index_once()
    test_deferral_is_real_and_renewed_date_lands()
    test_committed_write_stamps_synchronously()
    test_second_burst_rearms()
    test_read_burst_arms_exactly_one_timer()
    test_exit_hook_drains_a_dirty_index()
    test_exit_hook_is_registered_with_atexit()
    print("scenario_store_cache_flush: PASS")


if __name__ == "__main__":
    main()
