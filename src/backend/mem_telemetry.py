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

# Never sample faster than this: /proc/self/smaps_rollup measured 6.4ms
# median / 20ms max on the live 6.1GB-VmData process, and the walk holds
# mmap_lock for read (docs/memory-footprint-plan.md §2).
SAMPLE_INTERVAL = 60.0

# malloc_trim gate (P0.5): only probe when nothing has switched pages
# recently, and not more than once per window. With MALLOC_ARENA_MAX=2
# (P0.4) a trim holds the shared arena lock that every allocating thread
# funnels through, so it must never run on a hot path -- only from here,
# and only when the deck looks idle.
IDLE_SECONDS = 120.0
MIN_TRIM_INTERVAL = 600.0

CSV_HEADER = (
    "timestamp,vmrss_kb,vmswap_kb,private_dirty_kb,threads,fds,gc0,gc1,gc2,"
    "page_switches,trim_ms,trim_rss_before_kb,trim_rss_after_kb\n"
)


class _PageSwitchCounter:
    """Monotonic counter bumped by DeckController.load_page (any thread) and
    read by the sampler thread. itertools.count().__next__ is a single
    C-level op that holds the GIL for its whole call, so it's safe to bump
    without a lock; the paired timestamp is a plain rebind, also atomic
    under the GIL -- a torn read only costs a diagnostic reading one stale
    tick, never a real race."""

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


_libc = None


def _malloc_trim() -> None:
    """ctypes libc malloc_trim(0). Never call this off the idle+interval
    gate in MemTelemetrySampler -- see the module docstring note above
    IDLE_SECONDS."""
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    _libc.malloc_trim(ctypes.c_size_t(0))


class MemTelemetrySampler(threading.Thread):
    """Process memory sampler + idle malloc_trim.

    Always runs for the trim side: the 2026-07-07 overnight A/B measured 64
    trims at 0-3ms each (no arena-lock stall with MALLOC_ARENA_MAX=2),
    typically reclaiming 2-5MB and pulling a post-burst high-water down
    ~29MB -- cost is negligible, so the P0.5 probe was promoted to default-on
    (opt out with SC_MALLOC_TRIM=0). CSV recording stays opt-in via
    SC_MEM_TELEMETRY=1; without it the loop skips the smaps walk entirely
    and only reads /proc/self/status (microseconds) to log trim deltas.
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
        if not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0:
            with open(self.csv_path, "a") as f:
                f.write(CSV_HEADER)

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
        row = (
            f"{time.time():.0f},{vmrss},{vmswap},{private_dirty},{threads},{fds},"
            f"{gc0},{gc1},{gc2},{page_switches.value},{trim_ms},{trim_before},{trim_after}\n"
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
