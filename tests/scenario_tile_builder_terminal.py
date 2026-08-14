"""
The tile-cache builder thread must terminate when a build cannot complete.

_run_builder funnels every non-completing outcome through one terminal seam,
is_build_terminal, and returns. A promote failure at end-of-source, a
VideoWriter that never opens and a truncated source each drive that seam. A
bounded join detects a builder that busy-spins instead of returning.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os
import threading

import cv2
import numpy as np

import globals as gl
from fixtures import start_watchdog

from src.backend.DeckManagement.Subclasses import mp4_tile_cache as mtc

JOIN_TIMEOUT = 8.0  # a healthy builder exits in well under 1s, and a
                    # spinning one never exits, so this bounds the hang.


def make_mp4(path: str, n_frames: int = 10, size=(64, 64)) -> str:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, size)
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), i * 20 % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def make_entry(cache_path: str):
    entry = type("Entry", (), {})()
    entry.path = cache_path
    entry.stop_event = threading.Event()
    entry.ready = False
    entry.refcount = 1
    return entry


def run_builder_and_join(entry, source, out_size=(72, 72), saturation=1.0):
    """Start the real _run_builder on its own thread and bound-join it. A
    builder that busy-spins on a terminal build never returns, so is_alive
    after the join catches it directly."""
    t = threading.Thread(
        target=mtc._run_builder,
        args=(entry, source, out_size, saturation),
        daemon=True,
    )
    t.start()
    t.join(timeout=JOIN_TIMEOUT)
    return t


# Leg 1. A promote failure. The cache path is a directory, so the os.replace
# at end-of-source raises OSError and the capture is released without
# completion, which is the terminal state.
def leg_promote_failure() -> int:
    source = make_mp4(os.path.join(gl.DATA_PATH, "source_promote.mp4"))

    cache_path = os.path.join(gl.DATA_PATH, "cache", "promote-target.mp4")
    os.makedirs(cache_path, exist_ok=True)  # a directory occupies the path

    entry = make_entry(cache_path)
    t = run_builder_and_join(entry, source)

    if t.is_alive():
        print("FAIL(1): builder thread still running after join -- busy-spinning "
              "on a terminal build (B-02, os.replace/promote failure)")
        return 1
    if entry.ready:
        print("FAIL(1): entry marked ready although promotion failed")
        return 1
    print("PASS(1): promote (os.replace) failure exits the builder thread")
    return 0


# Leg 2. A VideoWriter that never opens. _end_of_source then runs with
# _writer None and promotes nothing, so the cache never completes and the
# source capture is released. A stub whose isOpened is False forces it.
class _DeadWriter:
    def isOpened(self):
        return False

    def write(self, *a, **k):  # never called, since it is never self._writer
        pass

    def release(self):
        pass


def leg_writer_open_fail() -> int:
    source = make_mp4(os.path.join(gl.DATA_PATH, "source_writer.mp4"))
    cache_path = os.path.join(gl.DATA_PATH, "cache", "writer-fail-target.mp4")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    entry = make_entry(cache_path)

    real_video_writer = mtc.cv2.VideoWriter

    def fake_video_writer(*a, **k):
        # Only the builder's write-cache VideoWriter goes through this call
        # site in _open_source; return a never-opened writer so _writer stays
        # None (the real "could not open tile cache writer" branch).
        return _DeadWriter()

    mtc.cv2.VideoWriter = fake_video_writer
    try:
        t = run_builder_and_join(entry, source)
    finally:
        mtc.cv2.VideoWriter = real_video_writer

    if t.is_alive():
        print("FAIL(2): builder thread still running after join -- a build "
              "whose VideoWriter never opened busy-spun instead of exiting "
              "(B-02, writer-open failure)")
        return 1
    if entry.ready:
        print("FAIL(2): entry marked ready although the writer never opened")
        return 1
    if os.path.isfile(cache_path):
        print("FAIL(2): a cache file was promoted although the writer never opened")
        return 1
    print("PASS(2): VideoWriter-open failure exits the builder thread")
    return 0


# ---------------------------------------------------------------------------
# Leg 3. Truncated source -- the container metadata promises N frames but the
# file delivers fewer. Byte-truncating an mp4v file is all-or-nothing here
# (the moov atom sits in the trailing bytes, so any truncation that drops
# sample data also drops the frame-count metadata -> the file won't open,
# which is the already-covered n_frames<=0 path, not the terminal seam). So
# the truncation is modelled at the capture seam instead. A source capture
# that opens and reports a positive CAP_PROP_FRAME_COUNT but whose read fails
# at once, as a source truncated to its header does. _end_of_source then
# releases the capture with nothing written and nothing promoted, which is
# the terminal state.
class _TruncatedCapture:
    """A cv2.VideoCapture stand-in for a source whose metadata over-promises.
    It opens and reports PROMISED frames, and read() never succeeds."""

    PROMISED = 60

    def __init__(self, *a, **k):
        pass

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return float(self.PROMISED)  # metadata promises 60...
        if prop == cv2.CAP_PROP_FPS:
            return 10.0
        return 0.0

    def set(self, *a, **k):
        return True

    def read(self):
        return (False, None)  # ...but the file is truncated to its header

    def release(self):
        pass


def leg_truncated_source() -> int:
    source = make_mp4(os.path.join(gl.DATA_PATH, "source_trunc.mp4"),
                      n_frames=10, size=(96, 96))

    cache_path = os.path.join(gl.DATA_PATH, "cache", "trunc-target.mp4")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    entry = make_entry(cache_path)

    real_capture = mtc.cv2.VideoCapture
    real_writer = mtc.cv2.VideoWriter

    def fake_capture(*a, **k):
        return _TruncatedCapture()

    # The writer would open fine, but with zero readable source frames nothing
    # is ever written; still, stub it so no real encoder file is touched and
    # _end_of_source's `_frames_written > 0` branch is provably not taken.
    mtc.cv2.VideoCapture = fake_capture
    mtc.cv2.VideoWriter = lambda *a, **k: _DeadWriter()
    try:
        t = run_builder_and_join(entry, source, out_size=(96, 96))
    finally:
        mtc.cv2.VideoCapture = real_capture
        mtc.cv2.VideoWriter = real_writer

    if t.is_alive():
        print("FAIL(3): builder thread still running after join -- a truncated "
              "source (metadata promises 60 frames, file delivers 0) busy-spun "
              "instead of exiting (B-02, truncated source)")
        return 1
    if entry.ready:
        print("FAIL(3): entry marked ready although the source delivered no frames")
        return 1
    if os.path.isfile(cache_path):
        print("FAIL(3): a cache file was promoted from a source that delivered "
              "no frames")
        return 1
    print("PASS(3): truncated source (over-promised metadata) exits the builder")
    return 0


def main() -> int:
    start_watchdog(40, "tile_builder_terminal")

    rc = 0
    rc |= leg_promote_failure()
    rc |= leg_writer_open_fail()
    rc |= leg_truncated_source()
    if rc == 0:
        print("PASS: scenario_tile_builder_terminal")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
