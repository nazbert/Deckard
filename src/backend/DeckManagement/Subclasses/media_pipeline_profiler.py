"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import os
import threading
import time
from collections import defaultdict

from loguru import logger as log


class MediaPipelineProfiler:
    """Aggregates per-section timings and counters from the media pipeline and
    logs a one-line summary per report window. All methods are thread-safe."""

    REPORT_INTERVAL = 5.0

    def __init__(self):
        self._lock = threading.Lock()
        self._times: dict[str, list[float]] = defaultdict(list)
        self._counts: dict[str, int] = defaultdict(int)
        self._window_start = time.monotonic()

    def add(self, section: str, seconds: float) -> None:
        with self._lock:
            self._times[section].append(seconds)

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] += n

    def maybe_report(self) -> None:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._window_start
            if elapsed < self.REPORT_INTERVAL:
                return
            times, counts = self._times, self._counts
            self._times, self._counts = defaultdict(list), defaultdict(int)
            self._window_start = now

        parts = []
        ticks = times.get("tick")
        if ticks:
            parts.append(f"loop_fps={len(ticks) / elapsed:.1f}")
        for section in sorted(times):
            values = sorted(times[section])
            total = sum(values)
            p50 = values[len(values) // 2]
            parts.append(f"{section} n={len(values)} tot={total * 1000:.0f}ms p50={p50 * 1000:.2f}ms")
        for name in sorted(counts):
            parts.append(f"{name}={counts[name]}")
        log.info(f"[media-prof] {elapsed:.1f}s window: " + " | ".join(parts))


# None when disabled so call sites can skip timing work entirely.
media_prof = MediaPipelineProfiler() if os.environ.get("DECKARD_MEDIA_PROFILE") else None
