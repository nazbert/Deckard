"""
Unit-tier scenario for the file-level tile-cache registry
(docs/memory-footprint-impl-plan.md P2.1, mp4_tile_cache.py).

The registry's whole reason to exist is the rejected v1 design it replaces:
sharing one Mp4FrameCache *instance* across consumers was measured to break
the build (interleaved frame requests abort the writer) and seek-thrash
playback afterward. This scenario drives the real registry
(acquire()/release(), KeyVideoCache, the detached builder thread) end to
end against small synthetic mp4 sources, covering the plan's P2.1 Verify
list:

  (a) two consumers acquiring the same (md5, size, saturation) share one
      cache file and one builder thread, each with its own VideoCapture.
  (b) the detached builder completes and promotes the cache file while a
      consumer is still playing directly from the source; the consumer
      then switches over on its next get_frame() call.
  (c) releasing both consumers (refcount -> 0) closes their captures and
      drops the registry bookkeeping entry.
  (d) a decode failure partway through a build clamps n_frames and
      releases the builder's source capture instead of leaking it (the
      deleted key_video_cache.py's bug 17 class must not be reproduced).
  (e) performance.cache-videos=false starts no builder thread at all.
"""
import os
import threading
import time

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
    """A file cv2 can open-attempt but never successfully decode a frame
    from -- exercises the "decode failure" path without needing a
    genuinely truncated/corrupt mp4 byte-for-byte."""
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
        # Each consumer owns its own VideoCapture (or None pre-open) -- never
        # the same object -- so one consumer's seeks/reads can't perturb the
        # other's decode position.
        assert r1.cap is not r2.cap or r1.cap is None, "consumers must not share a VideoCapture"

        assert entry.builder_thread is not None, "first acquire with no promoted cache must start a builder"
        builder_thread_from_r1_acquire = entry.builder_thread

        # A third consumer while the builder is still running must NOT start
        # a second builder thread for the same key.
        r3 = mp4_tile_cache.acquire(video_path, size, 1.0)
        try:
            assert entry.builder_thread is builder_thread_from_r1_acquire, (
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


def check_builder_promotes_while_consumer_plays_from_source() -> None:
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "promote_while_playing.mp4")
    # A slower fps than the previous check buys more wall-clock time for the
    # assertions below to observe the "still building" state before the
    # (very fast, tiny-frame) builder finishes.
    _make_test_video(video_path, n_frames=200, size=(320, 240))

    size = (64, 64)
    reader = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        key = mp4_tile_cache._registry_key(video_path, size, 1.0)
        entry = mp4_tile_cache._registry[key]

        # Drive the consumer from frame 0 immediately: unless the builder
        # already raced ahead and promoted, this must come from the
        # consumer's own direct source decode, not the (not yet existing)
        # cache file.
        first_frame = reader.get_frame(0)
        assert first_frame is not None
        if not entry.ready:
            assert not reader.is_cache_complete(), (
                "consumer must decode straight from source while the shared "
                "cache is still building, never block on it"
            )

        assert fixtures.wait_until(lambda: entry.ready, timeout=10.0), "builder never promoted the cache file"

        # The consumer's own instance hasn't necessarily noticed yet (it only
        # checks on the next get_frame() call) -- drive one more frame and
        # confirm it switched over.
        reader.get_frame(1)
        assert reader.is_cache_complete(), "consumer must adopt the promoted cache on its next get_frame() call"
        assert reader.cap is None, "the now-unneeded source capture must be released on switch-over"
    finally:
        mp4_tile_cache.release(reader)

    print("PASS: consumer plays from source until the detached builder promotes, then switches over")


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

    # A fresh acquire after full release must work cleanly (no stale state
    # left behind by the dropped entry) -- either finds the promoted file
    # from the earlier builder or starts a fresh one.
    r3 = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        assert r3.get_frame(0) is not None
    finally:
        mp4_tile_cache.release(r3)

    print("PASS: release to refcount zero closes captures and drops the registry entry")


def check_decode_failure_during_build_clamps_and_releases() -> None:
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "bogus_source.mp4")
    _make_bogus_video(video_path)

    size = (48, 48)
    cache_path = os.path.join(gl.DATA_PATH, "cache", "videos", "keys_48x48", "bogus.mp4")
    builder = mp4_tile_cache.KeyVideoCache(video_path, size, 1.0, cache_path=cache_path, is_builder=True)
    try:
        assert builder.n_frames == 0, "an unreadable source must report zero frames, not raise"
        # Force a decode attempt anyway (mirrors _run_builder calling
        # get_frame(last_frame_index + 1) once more before it notices
        # n_frames <= 0) -- must clamp and release, never raise, never hang.
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


def check_cache_videos_disabled_starts_no_builder() -> None:
    fixtures.install_stub_globals(app_settings={"performance": {"cache-videos": False}})
    video_path = os.path.join(gl.DATA_PATH, "disabled.mp4")
    _make_test_video(video_path, n_frames=15, size=(120, 90))

    size = (48, 48)
    reader = mp4_tile_cache.acquire(video_path, size, 1.0)
    try:
        key = mp4_tile_cache._registry_key(video_path, size, 1.0)
        entry = mp4_tile_cache._registry[key]
        assert entry.builder_thread is None, "cache-videos=false must never start a builder"

        # Direct source decode must still work (permanent uncached playback).
        for i in range(5):
            assert reader.get_frame(i) is not None
        assert not reader.is_cache_complete(), "with no builder, the reader must never see a promoted cache"
    finally:
        mp4_tile_cache.release(reader)

    print("PASS: performance.cache-videos=false starts no builder thread")


def check_saturation_key_and_path_agree() -> None:
    """Issue #53 item 1: the registry key's saturation component and the
    cache-file suffix must be pure functions of the SAME rounding. With the
    old split (`round(sat, 2)` for the key, `int(round(sat * 100))` for the
    suffix) two acquires whose raw factors round to the same key could share
    one _TileCacheEntry while the second reader targeted a file the entry's
    builder never writes -- permanent uncached playback plus a per-frame
    stat on the never-appearing file."""
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "sat_agreement.mp4")
    _make_test_video(video_path, n_frames=10, size=(120, 90))
    size = (48, 48)

    # Property: whatever the registry rounds a raw factor to must map to the
    # same file suffix the raw factor itself maps to.
    for raw in (1.0, 1.004, 0.996, 1.0049, 1.005, 0.005, 1.3, 1.25, 2.675, 0.999, 1.001):
        key = mp4_tile_cache._registry_key(video_path, size, raw)
        assert mp4_tile_cache.sat_suffix(key[2]) == mp4_tile_cache.sat_suffix(raw), (
            f"registry key and file suffix disagree for saturation {raw}: key "
            f"component {key[2]} -> {mp4_tile_cache.sat_suffix(key[2])!r} vs "
            f"raw -> {mp4_tile_cache.sat_suffix(raw)!r}"
        )

    # End to end: a second consumer whose raw factor lands in an existing
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


def check_missing_shared_cache_degrades_and_self_heals() -> None:
    """Issue #53 items 1+2 (degrade/self-heal): if the registry claims a
    shared cache is ready but the file cannot be opened (deleted behind the
    registry's back), the reader must keep playing from the source, and
    after a bounded number of failed adoption attempts must invalidate the
    entry (so a future acquire() starts a fresh builder) and detach (so it
    stops stat-ing the missing file on every frame)."""
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "vanishing.mp4")
    _make_test_video(video_path, n_frames=20, size=(120, 90))
    size = (48, 48)

    # Build + promote once, then drop the registry entry (file stays on disk).
    r0 = mp4_tile_cache.acquire(video_path, size, 1.0)
    entry0 = r0._registry_entry
    assert fixtures.wait_until(lambda: entry0.ready, timeout=10.0), "builder never promoted"
    path = entry0.path
    mp4_tile_cache.release(r0)
    assert os.path.isfile(path)

    # Deterministic re-creation of the race: an entry that stat'ed the file
    # as ready, whose file then vanishes before a reader can adopt it.
    entry = mp4_tile_cache._TileCacheEntry(path)
    assert entry.ready
    entry.builder_thread = threading.Thread(target=lambda: None)  # finished-builder stand-in
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
    """Context-manager drop-in for Mp4FrameCache.lock that widens the race
    window deterministically: when the designated frame thread RELEASES the
    lock, it blocks until close() has fully run on another thread. If the
    decoded payload is published outside the lock (the bug), the frame
    thread's tail then re-retains the frame close() just dropped."""

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


def check_close_does_not_retain_last_payload() -> None:
    """Issue #53 item 3: get_frame() used to publish `last_payload` after
    releasing the lock, so a close() that ran in that window had its
    `last_payload = None` overwritten -- one decoded frame retained for the
    life of the (supposedly closed) cache object."""
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
    lock.close_done.set()  # only now may get_frame's tail run
    frame_thread.join(timeout=5.0)
    assert not frame_thread.is_alive(), "frame thread wedged"

    assert cache.last_payload is None, (
        "a frame decoded before close() must not be re-retained after it "
        "(last_payload published outside the lock)"
    )

    print("PASS: close() leaves no retained frame behind a racing get_frame()")


def check_md5_memo_bounded() -> None:
    """Issue #53 item 4: the (path, size, mtime) -> md5 memo grew one entry
    per source-file version forever; it must be a small bounded LRU."""
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

        # Eviction must never affect correctness -- an evicted key simply
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
    check_builder_promotes_while_consumer_plays_from_source()
    check_release_to_zero_closes_captures()
    check_decode_failure_during_build_clamps_and_releases()
    check_cache_videos_disabled_starts_no_builder()
    check_saturation_key_and_path_agree()
    check_missing_shared_cache_degrades_and_self_heals()
    check_close_does_not_retain_last_payload()
    check_md5_memo_bounded()

    print("PASS: scenario_tile_cache")


if __name__ == "__main__":
    main()
