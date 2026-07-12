"""
Unit-tier scenario for issue #19: InputVideo.close() racing a concurrent
get_next_frame().

close() (called from load/teardown threads via close_resources()) used to
null self.video_cache while a render tick could be between its multiple
video_cache reads (n_frames / is_cache_complete() / get_source_fps() /
get_frame()) -> AttributeError on None. Worse than the logged error: a
get_frame() that starts AFTER the reader is released can resurrect a
cv2.VideoCapture via Mp4FrameCache._maybe_adopt_shared_cache and leak it.

The fix serializes the two with a per-instance lock (check-then-hold: an
unlocked None peek keeps the post-close path free; the body holds the lock
so close() waits for an in-flight frame and no frame starts against a
released reader).

Drives the REAL InputVideo via __new__ (house pattern, see
scenario_keyvideo_build.py) with a stub cache whose accessors deliberately
sleep, widening the historical race window, and which records any call
landing after its release. Also constructs one REAL InputVideo over the
mp4_tile_cache registry (real cv2 capture) to prove __init__ wires the lock
and close() works end-to-end.

Covers:
  (a) hammer: N rounds of a render thread ticking get_next_frame() while
      the main thread close()s mid-flight -- zero exceptions.
  (b) serialization: zero cache accesses recorded after release (the
      snapshot-only fix would pass (a) but fail this).
  (c) post-close get_next_frame() returns None; close() is idempotent.
  (d) real-registry InputVideo: construct, tick, close under a concurrent
      ticker -- no exception, reader detached.
"""
import os
import threading
import time

import fixtures

import cv2
import numpy as np

import globals as gl
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses import mp4_tile_cache


class RacyStubCache:
    """Mimics KeyVideoCache's surface used by InputVideo, with deliberate
    sleeps inside the accessors so a concurrent close() lands mid-
    get_next_frame with high probability (the pre-fix failure needed close
    to hit between two video_cache reads). Records use-after-release."""

    def __init__(self, n_frames: int = 10):
        self._n_frames = n_frames
        self.released = False
        self.calls_after_release = 0

    def _check(self):
        if self.released:
            self.calls_after_release += 1

    @property
    def n_frames(self) -> int:
        self._check()
        time.sleep(0.0002)
        return self._n_frames

    def is_cache_complete(self) -> bool:
        self._check()
        time.sleep(0.0002)
        return True

    def get_source_fps(self) -> float:
        self._check()
        return 30.0

    def get_frame(self, n: int):
        self._check()
        time.sleep(0.0002)
        return n

    # mp4_tile_cache.release(reader) calls reader.close(); no registry
    # bookkeeping runs (no _registry_key/_registry_entry attributes).
    def close(self) -> None:
        self.released = True


def make_video(cache) -> InputVideo:
    v = InputVideo.__new__(InputVideo)
    v.fps = 30
    v.loop = True
    v.natural_speed = False
    v.active_frame = -1
    v._play_start = None
    v._last_frame_tick = None
    v.video_cache = cache
    v._close_lock = threading.Lock()  # __init__ sets this; __new__ bypasses it
    return v


def check_close_race_hammer(rounds: int = 150) -> None:
    for r in range(rounds):
        cache = RacyStubCache()
        video = make_video(cache)
        errors: list = []
        stop = threading.Event()

        def render_loop():
            try:
                while not stop.is_set():
                    video.get_next_frame()
            except Exception as e:  # noqa: BLE001 -- the whole point
                errors.append(e)

        t = threading.Thread(target=render_loop, name=f"hammer-{r}", daemon=True)
        t.start()
        # Let the renderer get mid-flight, then close underneath it. Vary
        # the delay so close lands at different points of the body.
        time.sleep(0.0001 + (r % 7) * 0.0002)
        video.close()
        stop.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), f"round {r}: render thread wedged (deadlock?)"

        # (a) the historical failure: AttributeError('NoneType' ... ).
        assert not errors, f"round {r}: get_next_frame raised under concurrent close: {errors[0]!r}"

        # (b) serialization, not just crash-avoidance: once close() released
        # the reader, no cache access may happen (a straggler get_frame on a
        # released reader can resurrect+leak a capture via
        # _maybe_adopt_shared_cache).
        assert cache.calls_after_release == 0, (
            f"round {r}: {cache.calls_after_release} cache accesses after "
            f"release -- close() and get_next_frame() are not serialized"
        )

        # (c) post-close behavior: quiet None, and idempotent close.
        assert video.get_next_frame() is None
        video.close()

    print(f"PASS: close-vs-get_next_frame hammer ({rounds} rounds, no errors, no use-after-release)")


class _StubDeckControllerReal:
    def get_display_saturation(self) -> float:
        return 1.0


class _StubControllerInputReal:
    """Exactly what InputVideo.__init__ reads: .deck_controller (via
    SingleKeyAsset) and get_image_size()."""

    def __init__(self):
        self.deck_controller = _StubDeckControllerReal()

    def get_image_size(self) -> tuple[int, int]:
        return (48, 48)


def _make_test_video(path: str, n_frames: int = 20, size=(64, 64)) -> None:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    for i in range(n_frames):
        frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        frame[:, :] = (10 * i % 255, 80, 200)
        writer.write(frame)
    writer.release()


def check_real_inputvideo_close() -> None:
    # Registry + cv2 tier: __init__ must wire the lock itself (the hammer
    # above hand-sets it), and close() must run the real release path while
    # a ticker is mid-frame.
    fixtures.install_stub_globals()

    video_path = os.path.join(gl.DATA_PATH, "close_race_source.mp4")
    _make_test_video(video_path)

    video = InputVideo(
        controller_input=_StubControllerInputReal(),
        video_path=video_path, fps=30, loop=True,
    )
    assert video.get_next_frame() is not None, "sanity: a real frame decodes"

    errors: list = []
    stop = threading.Event()

    def render_loop():
        try:
            while not stop.is_set():
                video.get_next_frame()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=render_loop, name="real-hammer", daemon=True)
    t.start()
    time.sleep(0.05)
    video.close()
    stop.set()
    t.join(timeout=5.0)
    assert not t.is_alive(), "real-registry render thread wedged"
    assert not errors, f"real-registry close raced get_next_frame: {errors[0]!r}"
    assert video.video_cache is None
    assert video.get_next_frame() is None
    video.close()  # idempotent

    print("PASS: real-registry InputVideo close under concurrent ticks")


def main() -> None:
    fixtures.start_watchdog(60, "scenario_keyvideo_close_race")
    check_close_race_hammer()
    check_real_inputvideo_close()
    print("PASS: scenario_keyvideo_close_race")


if __name__ == "__main__":
    main()
