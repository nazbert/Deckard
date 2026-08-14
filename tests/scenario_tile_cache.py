"""
Unit-tier scenario for the file-level tile-cache registry.

Two consumers that acquire the same (md5, size, saturation) share one cache
file and one builder thread, each with its own VideoCapture. The detached
builder promotes the cache while a consumer plays from the source, and the
consumer switches over on its next get_frame. Releasing both drops the entry.
"""
import os
import threading

import fixtures
import cv2
import numpy as np

import globals as gl
from src.backend.DeckManagement.Subclasses import mp4_tile_cache

WATCHDOG_SECONDS = 30


def _make_test_video(path: str, n_frames: int = 30, size=(160, 120), fps: int = 30) -> None:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    for i in range(n_frames):
        frame[:, :] = (i % 255, 60, 120)
        writer.write(frame)
    writer.release()


def _make_bogus_video(path: str) -> None:
    """A file cv2 can attempt to open but never decode a frame from. It
    drives the decode-failure path without a byte-for-byte corrupt mp4."""
    with open(path, "wb") as f:
        f.write(b"not a real video container")


def check_shared_file_one_builder() -> None:
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "shared.mp4")
    _make_test_video(video_path, n_frames=40, size=(160, 120))

    size = (64, 64)
    r1 = mp4_tile_cache.acquire(video_path, size, 1.0)
    r2 = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        key = mp4_tile_cache._registry_key(video_path, size, 1.0)
        entry = mp4_tile_cache._registry[key]

        assert entry.refcount == 2, f"expected refcount 2 after two acquires, got {entry.refcount}"
        assert r1 is not r2, "each consumer must get its own reader instance"
        assert r1._registry_entry is r2._registry_entry, "both readers must share one registry entry"
        assert r1.cache_path == r2.cache_path == entry.path, "both readers must target the same cache file"
        # Each consumer owns its own VideoCapture, or None before the open,
        # so one consumer's seeks and reads cannot move the other's decode
        # position.
        assert r1.cap is not r2.cap or r1.cap is None, "consumers must not share a VideoCapture"

        assert entry.builder_thread is not None, "first acquire with no promoted cache must start a builder"
        builder_thread_from_first_acquire = entry.builder_thread

        # A third consumer, while the builder still runs, must not start a
        # second builder thread for the same key.
        r3 = mp4_tile_cache.acquire(video_path, size, 1.0)
        try:
            assert entry.builder_thread is builder_thread_from_first_acquire, (
                "a second acquire on the same in-flight key must not start a second builder"
            )
        finally:
            mp4_tile_cache.release(r3)

        assert fixtures.wait_until(lambda: entry.ready, timeout=10.0), "builder never promoted the cache file"
        assert os.path.isfile(entry.path), "promoted cache file must exist on disk"
    finally:
        mp4_tile_cache.release(r1)
        mp4_tile_cache.release(r2)

    print("PASS: two consumers share one cache file and one builder thread")


def check_builder_promotes_during_playback() -> None:
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "promote_while_playing.mp4")
    # A slower fps than the previous check buys wall-clock time to observe
    # the still-building state before the tiny-frame builder finishes.
    _make_test_video(video_path, n_frames=200, size=(320, 240))

    size = (64, 64)
    reader = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        key = mp4_tile_cache._registry_key(video_path, size, 1.0)
        entry = mp4_tile_cache._registry[key]

        # Drive the consumer from frame 0 at once. Unless the builder raced
        # ahead and promoted, this comes from the consumer's own source
        # decode rather than a cache file that does not exist yet.
        first_frame = reader.get_frame(0)
        assert first_frame is not None
        if not entry.ready:
            assert not reader.is_cache_complete(), (
                "consumer must decode straight from source while the shared "
                "cache is still building, never block on it"
            )

        assert fixtures.wait_until(lambda: entry.ready, timeout=10.0), "builder never promoted the cache file"

        # The consumer's own instance may not have noticed yet, because it
        # checks on the next get_frame call. Drive one more frame and confirm
        # it switched over.
        reader.get_frame(1)
        assert reader.is_cache_complete(), "consumer must adopt the promoted cache on its next get_frame() call"
        assert reader.cap is None, "the now-unneeded source capture must be released on switch-over"
    finally:
        mp4_tile_cache.release(reader)

    print("PASS: consumer plays from source until the detached builder promotes, then switches over")


def check_plays_from_source_forced_window() -> None:
    """The sibling check guards its from-source assertion behind a ready
    test, and on a fast machine the tiny-frame builder promotes first, so
    that assertion is skipped.

    A wrapper around _run_builder blocks on a barrier until a from-source
    read has run, then runs the real builder. Inside that window entry.ready
    is False, so the from-source assertion always runs."""
    fixtures.install_stub_globals()
    # A distinctive frame count and size, so this source's md5, and its
    # cache filename, cannot collide with another check's video in the shared
    # data dir. A byte-identical video would md5 to an already-promoted cache
    # path, leaving entry.ready True and starting no builder to hold.
    video_path = os.path.join(gl.DATA_PATH, "forced_window.mp4")
    _make_test_video(video_path, n_frames=57, size=(176, 132))
    size = (56, 56)

    real_run_builder = mp4_tile_cache._run_builder
    hold = threading.Event()          # the check lets the builder proceed
    builder_entered = threading.Event()  # the builder reports that it holds

    def _held_run_builder(entry, source_path, out_size, saturation):
        # Announce the builder thread before it promotes, then block, so the
        # consumer sees entry.ready False for its first reads. The wait is
        # bounded, so a defect cannot wedge the suite.
        builder_entered.set()
        if not hold.wait(timeout=15):
            return  # never released, so the assertions report it
        real_run_builder(entry, source_path, out_size, saturation)

    mp4_tile_cache._run_builder = _held_run_builder
    try:
        reader = mp4_tile_cache.acquire(video_path, size, 1.0)
        try:
            entry = reader._registry_entry

            # The builder thread must have started and parked in the hold.
            assert builder_entered.wait(timeout=5), "builder thread never started"
            # Inside the window, so nothing is promoted yet.
            assert entry.ready is False, "forced window invariant: builder must not have promoted yet"

            # These from-source assertions run unconditionally. The consumer
            # must decode straight from the source and must not have adopted
            # a cache that does not exist yet.
            first_frame = reader.get_frame(0)
            assert first_frame is not None, "consumer must decode from source inside the forced window"
            assert not reader.is_cache_complete(), (
                "consumer must NOT report a complete cache while the builder "
                "is held pre-promotion -- it must be playing from source"
            )
            assert reader.cap is not None, (
                "consumer must hold its own source VideoCapture while playing "
                "from source (released only on switch-over)"
            )
            assert not os.path.isfile(entry.path), (
                "the shared cache file must not exist yet inside the forced "
                "pre-promotion window"
            )

            # Release the builder and let it promote, then confirm the
            # consumer switches over on its next get_frame call.
            hold.set()
            assert fixtures.wait_until(lambda: entry.ready, timeout=10.0), "builder never promoted after release"
            reader.get_frame(1)
            assert reader.is_cache_complete(), "consumer must adopt the promoted cache on its next get_frame()"
            assert reader.cap is None, "the source capture must be released on switch-over"
        finally:
            hold.set()  # never leave the builder parked
            mp4_tile_cache.release(reader)
    finally:
        mp4_tile_cache._run_builder = real_run_builder

    print("PASS: consumer plays from source in a deterministically-forced pre-promotion window")


def check_release_to_zero_closes_captures() -> None:
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "refcount.mp4")
    _make_test_video(video_path, n_frames=20, size=(160, 120))

    size = (48, 48)
    r1 = mp4_tile_cache.acquire(video_path, size, 1.0)
    r2 = mp4_tile_cache.acquire(video_path, size, 1.0)
    r1.get_frame(0)
    r2.get_frame(0)

    key = mp4_tile_cache._registry_key(video_path, size, 1.0)
    entry = mp4_tile_cache._registry[key]

    mp4_tile_cache.release(r1)
    assert key in mp4_tile_cache._registry, "registry entry must survive while refcount > 0"
    assert r1.cap is None and r1._cache_cap is None, "a released reader's captures must be closed"

    mp4_tile_cache.release(r2)
    assert key not in mp4_tile_cache._registry, "registry entry must be dropped once refcount reaches 0"
    assert r2.cap is None and r2._cache_cap is None, "a released reader's captures must be closed"

    # A fresh acquire after a full release must work cleanly, with no stale
    # state behind. It finds the earlier builder's promoted file or starts a
    # fresh builder.
    r3 = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        assert r3.get_frame(0) is not None
    finally:
        mp4_tile_cache.release(r3)

    print("PASS: release to refcount zero closes captures and drops the registry entry")


def check_decode_failure_clamps_and_releases() -> None:
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "bogus_source.mp4")
    _make_bogus_video(video_path)

    size = (48, 48)
    cache_path = os.path.join(gl.DATA_PATH, "cache", "videos", "keys_48x48", "bogus.mp4")
    builder = mp4_tile_cache.KeyVideoCache(video_path, size, 1.0, cache_path=cache_path, is_builder=True)
    try:
        assert builder.n_frames == 0, "an unreadable source must report zero frames, not raise"
        # Force a decode attempt, as _run_builder does once more before it
        # notices n_frames is not positive. It must clamp and release, and
        # must never raise or hang.
        payload = builder.get_frame(0)
        assert payload is None
        assert not builder.is_cache_complete()
        assert builder.cap is None, (
            "a decode failure must release the source capture even when zero "
            "frames were ever written (the deleted key_video_cache.py's "
            "VideoFrameCache left this open forever -- design doc bug 17)"
        )
    finally:
        builder.close()

    print("PASS: decode failure during build clamps n_frames and releases the capture")


def check_disabled_cache_starts_no_builder() -> None:
    fixtures.install_stub_globals(app_settings={"performance": {"cache-videos": False}})
    video_path = os.path.join(gl.DATA_PATH, "disabled.mp4")
    _make_test_video(video_path, n_frames=15, size=(120, 90))

    size = (48, 48)
    reader = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        key = mp4_tile_cache._registry_key(video_path, size, 1.0)
        entry = mp4_tile_cache._registry[key]
        assert entry.builder_thread is None, "cache-videos=false must never start a builder"

        # A direct source decode must still work, as uncached playback.
        for i in range(5):
            assert reader.get_frame(i) is not None
        assert not reader.is_cache_complete(), "with no builder, the reader must never see a promoted cache"
    finally:
        mp4_tile_cache.release(reader)

    print("PASS: performance.cache-videos=false starts no builder thread")


def check_saturation_key_and_path_agree() -> None:
    """The registry key's saturation component and the cache-file suffix must
    be pure functions of one rounding. Two roundings let two acquires share
    one entry while the second reader targets a file the builder never
    writes, which costs uncached playback and a per-frame stat."""
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "sat_agreement.mp4")
    _make_test_video(video_path, n_frames=10, size=(120, 90))
    size = (48, 48)

    # Whatever the registry rounds a raw factor to must map to the same file
    # suffix the raw factor itself maps to.
    for raw in (1.0, 1.004, 0.996, 1.0049, 1.005, 0.005, 1.3, 1.25, 2.675, 0.999, 1.001):
        key = mp4_tile_cache._registry_key(video_path, size, raw)
        assert mp4_tile_cache.sat_suffix(key[2]) == mp4_tile_cache.sat_suffix(raw), (
            f"registry key and file suffix disagree for saturation {raw}: key "
            f"component {key[2]} -> {mp4_tile_cache.sat_suffix(key[2])!r} vs "
            f"raw -> {mp4_tile_cache.sat_suffix(raw)!r}"
        )

    # End to end, a second consumer whose raw factor lands in an existing
    # entry's bucket must target the file that entry's builder wrote.
    r1 = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        entry = r1._registry_entry
        assert fixtures.wait_until(lambda: entry.ready, timeout=10.0), "builder never promoted"
        r2 = mp4_tile_cache.acquire(video_path, size, 1.004)
        try:
            assert r2._registry_entry is entry, "1.004 must land in the 1.0 entry's bucket"
            assert r2.cache_path == entry.path, (
                f"reader targets {r2.cache_path} but the entry's builder wrote "
                f"{entry.path} -- the reader would wait on this file forever"
            )
            r2.get_frame(0)
            assert r2.is_cache_complete(), "reader must adopt the promoted shared cache"
        finally:
            mp4_tile_cache.release(r2)
    finally:
        mp4_tile_cache.release(r1)

    print("PASS: registry key and cache-file suffix always agree on the saturation bucket")


def check_missing_shared_cache_self_heals() -> None:
    """When the registry claims a shared cache is ready but the file cannot
    be opened, the reader keeps playing from the source. After a bounded
    number of failed adoptions it invalidates the entry, so a later acquire
    starts a fresh builder, and detaches from the missing file."""
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "vanishing.mp4")
    _make_test_video(video_path, n_frames=20, size=(120, 90))
    size = (48, 48)

    # Build and promote once, then drop the registry entry. The file stays.
    r0 = mp4_tile_cache.acquire(video_path, size, 1.0)
    entry0 = r0._registry_entry
    assert fixtures.wait_until(lambda: entry0.ready, timeout=10.0), "builder never promoted"
    path = entry0.path
    mp4_tile_cache.release(r0)
    assert os.path.isfile(path)

    # A deterministic re-creation of the race. The entry stat'ed the file as
    # ready, and the file then vanished before a reader could adopt it.
    entry = mp4_tile_cache._TileCacheEntry(path)
    assert entry.ready
    entry.builder_thread = threading.Thread(target=lambda: None)  # a finished builder
    os.remove(path)

    reader = mp4_tile_cache.KeyVideoCache(video_path, size, 1.0, cache_path=path, is_builder=False)
    reader._registry_key = ("synthetic-key",)
    reader._registry_entry = entry
    try:
        for i in range(10):
            assert reader.get_frame(i) is not None, "reader must degrade to source decode, not go dark"
        assert entry.ready is False, "a ready entry whose file is gone must be invalidated (self-heal)"
        assert entry.builder_thread is None, "invalidation must clear the finished builder so acquire() can start a new one"
        assert reader._registry_entry is None, "reader must detach after bounded adoption failures"
    finally:
        reader.close()

    print("PASS: a vanished shared cache degrades to source decode and invalidates the registry entry")


class _HandoffLock:
    """A drop-in for Mp4FrameCache.lock that widens the race window. When the
    designated frame thread releases the lock, it blocks until close() has
    run on another thread. A payload published outside the lock then
    re-retains the frame close() just dropped."""

    def __init__(self):
        self._inner = threading.Lock()
        self.frame_thread: threading.Thread = None
        self.frame_thread_released = threading.Event()
        self.close_done = threading.Event()

    def __enter__(self):
        self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._inner.release()
        if threading.current_thread() is self.frame_thread:
            self.frame_thread_released.set()
            self.close_done.wait(timeout=5.0)
        return False


def check_close_drops_last_payload() -> None:
    """get_frame must publish last_payload under the lock. A publish after
    the release lets a close() in that window have its None overwritten,
    which retains one decoded frame for the life of the closed cache."""
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "close_race.mp4")
    _make_test_video(video_path, n_frames=10, size=(120, 90))
    size = (48, 48)
    cache_path = os.path.join(gl.DATA_PATH, "cache", "videos", "keys_48x48", "close_race.mp4")
    cache = mp4_tile_cache.KeyVideoCache(video_path, size, 1.0, cache_path=cache_path, is_builder=False)

    lock = _HandoffLock()
    cache.lock = lock

    frame_thread = threading.Thread(target=lambda: cache.get_frame(0), name="frame-thread")
    lock.frame_thread = frame_thread
    frame_thread.start()
    assert lock.frame_thread_released.wait(timeout=5.0), "frame thread never released the cache lock"
    cache.close()          # clears last_payload under the lock
    lock.close_done.set()  # only now may get_frame's tail run on
    frame_thread.join(timeout=5.0)
    assert not frame_thread.is_alive(), "frame thread wedged"

    assert cache.last_payload is None, (
        "a frame decoded before close() must not be re-retained after it "
        "(last_payload published outside the lock)"
    )

    print("PASS: close() leaves no retained frame behind a racing get_frame()")


def check_md5_memo_bounded() -> None:
    """The memo from (path, size, mtime) to md5 must be a small bounded LRU,
    rather than one entry per source-file version forever."""
    fixtures.install_stub_globals()
    original_cap = mp4_tile_cache._MD5_MEMO_MAX
    mp4_tile_cache._MD5_MEMO_MAX = 8
    try:
        with mp4_tile_cache._md5_memo_lock:
            mp4_tile_cache._md5_memo.clear()
        paths = []
        for i in range(20):
            path = os.path.join(gl.DATA_PATH, f"memo_{i}.bin")
            with open(path, "wb") as f:
                f.write(bytes([i]) * 64)
            paths.append(path)
            mp4_tile_cache.get_video_md5(path)
        with mp4_tile_cache._md5_memo_lock:
            memo_len = len(mp4_tile_cache._md5_memo)
        assert memo_len <= 8, f"memo must stay bounded, has {memo_len} entries"

        # Eviction never affects correctness, because an evicted key
        # re-hashes.
        import hashlib
        expected = hashlib.md5(bytes([0]) * 64).hexdigest()
        assert mp4_tile_cache.get_video_md5(paths[0]) == expected
        assert mp4_tile_cache.get_video_md5(paths[0]) == expected  # memoized hit
    finally:
        mp4_tile_cache._MD5_MEMO_MAX = original_cap

    print("PASS: md5 memo is bounded and eviction preserves correctness")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_tile_cache")

    check_shared_file_one_builder()
    check_builder_promotes_during_playback()
    check_plays_from_source_forced_window()
    check_release_to_zero_closes_captures()
    check_decode_failure_clamps_and_releases()
    check_disabled_cache_starts_no_builder()
    check_saturation_key_and_path_agree()
    check_missing_shared_cache_self_heals()
    check_close_drops_last_payload()
    check_md5_memo_bounded()

    print("PASS: scenario_tile_cache")


if __name__ == "__main__":
    main()
