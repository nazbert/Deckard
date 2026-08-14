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
import ctypes
import ctypes.util
import gc
import itertools
import os
import threading
import time

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses import cache_budget

# Never sample faster than this. A read of /proc/self/smaps_rollup measured
# 6.4ms median and 20ms maximum on the live process at 6.1GB of VmData, and
# that walk holds mmap_lock for read (docs/memory-footprint-plan.md).
SAMPLE_INTERVAL = 60.0

# The malloc_trim gate. Probe only while no page switch happened recently,
# and once per window at most. With MALLOC_ARENA_MAX=2 a trim holds the shared
# arena lock that every allocating thread passes through, so it must never run
# on a hot path. It runs from here alone, and only while the deck looks
# idle.
IDLE_SECONDS = 120.0
MIN_TRIM_INTERVAL = 600.0

CSV_HEADER = (
    "timestamp,vmrss_kb,vmswap_kb,private_dirty_kb,threads,fds,gc0,gc1,gc2,"
    "page_switches,trim_ms,trim_rss_before_kb,trim_rss_after_kb,"
    "img_cache_kb,img_cache_evictions,img_cache_evicted_kb,"
    "video_readers_kb,gif_frames_kb\n"
)


class _PageSwitchCounter:
    """A monotonic counter. DeckController.load_page raises it from any
    thread, and the sampler thread reads it. itertools.count().__next__ is one
    C-level operation that holds the GIL for its whole call, so a bump needs
    no lock. The paired timestamp is a plain rebind, which is atomic under the
    GIL too. A torn read costs a diagnostic one stale tick and no more."""

    def __init__(self):
        self._counter = itertools.count(1)
        self.value = 0
        self.last_switch_monotonic = time.monotonic()

    def bump(self) -> None:
        self.value = next(self._counter)
        self.last_switch_monotonic = time.monotonic()


page_switches = _PageSwitchCounter()


def _read_status_fields() -> tuple[int, int]:
    """Return (VmRSS, VmSwap) in kB from /proc/self/status."""
    vmrss = vmswap = 0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    vmrss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    vmswap = int(line.split()[1])
    except OSError:
        pass
    return vmrss, vmswap


def _read_private_dirty_kb() -> int:
    """Private_Dirty from /proc/self/smaps_rollup, in kB."""
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Private_Dirty:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _thread_count() -> int:
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return threading.active_count()


def _fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _image_cache_fields() -> tuple[int, int, int, int, int]:
    """Returns the evictable image-cache kB, the cumulative evictions, the
    cumulative evicted kB, the video-reader kB and the GIF-frame kB, from the
    image-cache budget.

    This attributes the memory, which is why it stays on by default. The
    ceiling tunes against the field once the CSV says how much image RAM the
    process holds, how hard the ceiling bites, and how much of the rest sits
    in holders that the ceiling does not govern, which are the video readers
    and above all the GIF frame lists, which carry no byte cap. Every value is
    a cheap sum of per-cache counters, and nothing walks a cache."""
    try:
        totals = cache_budget.totals()
        evictions, evicted_bytes = cache_budget.eviction_stats()
        return (
            cache_budget.evictable_bytes() // 1024,
            evictions,
            evicted_bytes // 1024,
            totals.get("video_readers", 0) // 1024,
            totals.get("gif_frames", 0) // 1024,
        )
    except Exception as e:
        log.debug(f"mem_telemetry: image-cache fields unavailable: {e}")
        return 0, 0, 0, 0, 0


_libc = None


def _malloc_trim() -> None:
    """Calls malloc_trim(0) in libc through ctypes. Call it from the idle and
    interval gate in MemTelemetrySampler alone; see the note above
    IDLE_SECONDS."""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    _libc.malloc_trim(ctypes.c_size_t(0))


class MemTelemetrySampler(threading.Thread):
    """The process memory sampler, and the idle malloc_trim.

    The trim side always runs. An overnight A/B measured 64 trims at 0 to 3ms
    each, with no arena-lock stall under MALLOC_ARENA_MAX=2, which reclaimed 2
    to 5MB each and pulled a post-burst high-water down by about 29MB. That
    cost is small, so the trim is on by default, and SC_MALLOC_TRIM=0 turns it
    off. The CSV recording stays opt-in through SC_MEM_TELEMETRY=1. Without it
    the loop skips the smaps walk and reads /proc/self/status alone, which
    costs microseconds, to log the trim deltas.
    """

    def __init__(self):
        super().__init__(name="mem_telemetry", daemon=True)
        self._stop_event = threading.Event()
        self._trim_enabled = os.environ.get("SC_MALLOC_TRIM", "1") != "0"
        self._csv_enabled = bool(os.environ.get("SC_MEM_TELEMETRY"))
        self._last_trim_monotonic = 0.0
        logs_dir = os.path.join(gl.DATA_PATH, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        self.csv_path = os.path.join(logs_dir, "mem_telemetry.csv")
        if self._csv_enabled:
            self._ensure_header()

    def _ensure_header(self) -> None:
        """Writes the header into a new or empty CSV.

        An existing file whose header predates a schema change rotates once
        to <path>.old, and a fresh file starts. Wider rows appended under a
        narrower header misalign every column for every reader of the file,
        which are tests/soak/mem_census.py, the hw_verify.py soak, and any
        spreadsheet."""
        try:
            if os.path.exists(self.csv_path) and os.path.getsize(self.csv_path) > 0:
                with open(self.csv_path) as f:
                    existing = f.readline()
                if existing == CSV_HEADER:
                    return
                os.replace(self.csv_path, self.csv_path + ".old")
                log.info(
                    f"mem_telemetry: column schema changed; rotated the old CSV to "
                    f"{self.csv_path}.old"
                )
            with open(self.csv_path, "a") as f:
                f.write(CSV_HEADER)
        except OSError as e:
            log.warning(f"mem_telemetry: could not prepare {self.csv_path}: {e}")

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(SAMPLE_INTERVAL):
            try:
                self._sample()
            except Exception as e:
                log.debug(f"mem_telemetry: sample failed: {e}")

    def _idle(self) -> bool:
        return (time.monotonic() - page_switches.last_switch_monotonic) >= IDLE_SECONDS

    def _trim_due(self) -> bool:
        return (time.monotonic() - self._last_trim_monotonic) >= MIN_TRIM_INTERVAL

    def _maybe_trim(self, rss_before: int) -> tuple[str, str, str]:
        if not (self._trim_enabled and self._idle() and self._trim_due()):
            return "", "", ""
        t0 = time.perf_counter()
        try:
            _malloc_trim()
        except Exception as e:
            log.debug(f"mem_telemetry: malloc_trim failed: {e}")
            return "", "", ""
        duration_ms = (time.perf_counter() - t0) * 1000
        rss_after, _ = _read_status_fields()
        self._last_trim_monotonic = time.monotonic()
        log.info(f"mem_telemetry: malloc_trim took {duration_ms:.1f}ms, RSS {rss_before}->{rss_after}kB")
        return f"{duration_ms:.1f}", str(rss_before), str(rss_after)

    def _sample(self) -> None:
        vmrss, vmswap = _read_status_fields()
        trim_result = self._maybe_trim(vmrss)
        if not self._csv_enabled:
            return
        private_dirty = _read_private_dirty_kb()
        threads = _thread_count()
        fds = _fd_count()
        gc0, gc1, gc2 = gc.get_count()
        trim_ms, trim_before, trim_after = trim_result
        img_kb, evictions, evicted_kb, video_kb, gif_kb = _image_cache_fields()
        row = (
            f"{time.time():.0f},{vmrss},{vmswap},{private_dirty},{threads},{fds},"
            f"{gc0},{gc1},{gc2},{page_switches.value},{trim_ms},{trim_before},{trim_after},"
            f"{img_kb},{evictions},{evicted_kb},{video_kb},{gif_kb}\n"
        )
        with open(self.csv_path, "a") as f:
            f.write(row)


_sampler: MemTelemetrySampler | None = None


def start_if_enabled() -> None:
    """Start the sampler thread. Always runs (for the default-on idle
    malloc_trim) unless SC_MALLOC_TRIM=0 *and* SC_MEM_TELEMETRY is unset;
    CSV recording additionally requires SC_MEM_TELEMETRY=1. No-op if
    already started (safe to call more than once)."""
    global _sampler
    if _sampler is not None:
        return
    trim_on = os.environ.get("SC_MALLOC_TRIM", "1") != "0"
    csv_on = bool(os.environ.get("SC_MEM_TELEMETRY"))
    if not trim_on and not csv_on:
        return
    _sampler = MemTelemetrySampler()
    _sampler.start()
    if csv_on:
        log.info(f"mem_telemetry: sampler started, writing to {_sampler.csv_path}")
    else:
        log.info("mem_telemetry: idle malloc_trim active (CSV off; enable with SC_MEM_TELEMETRY=1)")
