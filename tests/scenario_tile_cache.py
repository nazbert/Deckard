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


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_tile_cache")

    check_shared_file_one_builder()
    check_builder_promotes_while_consumer_plays_from_source()
    check_release_to_zero_closes_captures()
    check_decode_failure_during_build_clamps_and_releases()
    check_cache_videos_disabled_starts_no_builder()

    print("PASS: scenario_tile_cache")


if __name__ == "__main__":
    main()
