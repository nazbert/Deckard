"""
Scenario: the tile-cache builder thread must terminate when a build cannot
complete (issue #2 / B-02).

_run_builder looped `get_frame(last+1)` with no sleep; when _end_of_source()
released the source capture WITHOUT promoting the cache (VideoWriter open
failure, os.replace failure, cache-reopen failure, truncated source), every
further get_frame returned instantly and the loop busy-spun a full core for
as long as the video key stayed on screen (reproduced upstream at ~4.6M
calls/sec).

Trigger used here: the cache path is occupied by a DIRECTORY, so the
end-of-source os.replace(tmp, cache_path) fails -> writer released, cap
released, _complete False -- the terminal state. Pre-fix _run_builder never
returns (bounded join detects it); post-fix it logs and exits promptly.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os
import threading

import cv2
import numpy as np

import globals as gl
from fixtures import start_watchdog

from src.backend.DeckManagement.Subclasses import mp4_tile_cache as mtc


def make_mp4(path: str, n_frames: int = 10, size=(64, 64)) -> str:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, size)
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), i * 20 % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path


def main() -> int:
    start_watchdog(30, "tile_builder_terminal")

    source = make_mp4(os.path.join(gl.DATA_PATH, "source.mp4"))

    # Occupy the cache path with a directory: os.replace() onto it fails at
    # end-of-source, releasing the capture without completion.
    cache_path = os.path.join(gl.DATA_PATH, "cache", "poison-target.mp4")
    os.makedirs(cache_path, exist_ok=True)

    entry = type("Entry", (), {})()
    entry.path = cache_path
    entry.stop_event = threading.Event()
    entry.ready = False
    entry.refcount = 1

    t = threading.Thread(
        target=mtc._run_builder,
        args=(entry, source, (72, 72), 1.0),
        daemon=True,
    )
    t.start()
    t.join(timeout=8)

    if t.is_alive():
        print("FAIL: builder thread still running after 8s -- busy-spinning "
              "on a terminal build (B-02); it would spin until the key "
              "leaves the screen")
        return 1
    if entry.ready:
        print("FAIL: entry marked ready although the cache never completed")
        return 1
    print("PASS: terminal build exits the builder thread promptly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
