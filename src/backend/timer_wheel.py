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
# One daemon scheduler thread for the ported delays: the screensaver reset per
# keypress, the overlay hide, and the key-hold delay. A min-heap and a
# Condition keep exactly one thread asleep, whatever the number of outstanding
# delays.
#
# The handles match threading.Timer, so a cancel-and-recreate call site needs
# only an import change:
#   - schedule(delay_s, callback) arms at once and returns a handle.
#   - handle.cancel() is idempotent and safe after the callback fired. It
#     cannot un-fire the callback, the same as Timer.cancel().
#
# The scheduler thread pops due handles and hands them off. It never runs a
# callback inline, because a callback is expensive. ScreenSaver.show() hashes
# the source file and can open a video capture before it takes a lock, and an
# inline run delays every other pending timer in the process. Each fire gets
# its own short-lived daemon thread, not main_loop's shared @background pool.
# That pool holds 8 workers for I/O-bound plugin and asset work, and these
# fires would contend with it. A fire here is rare (one reset per keypress,
# one overlay hide, one hold timer), so a fresh daemon thread per fire is
# cheap. It also keeps this module on stdlib and loguru, with no GLib, which
# the headless test harness needs.
import heapq
import itertools
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger as log


class TimerHandle:
    """Returned by TimerWheel.schedule(). Not constructed directly."""

    __slots__ = ("_wheel", "_seq", "_due", "_callback", "_name", "_cancelled", "_fired")

    def __init__(self, wheel: "TimerWheel", seq: int, due: float, callback: Callable[[], Any], name: str):
        self._wheel = wheel
        self._seq = seq
        self._due = due
        self._callback = callback
        self._name = name
        self._cancelled = False
        self._fired = False

    def cancel(self) -> None:
        """Stop this timer before it fires. Idempotent, and a no-op after the
        timer fired, the same as threading.Timer.cancel()."""
        self._wheel._cancel(self)

    def is_alive(self) -> bool:
        """True while the timer is pending. The name matches
        threading.Timer.is_alive() for call sites that probe it."""
        return not (self._cancelled or self._fired)


class TimerWheel:
    """One daemon scheduler thread behind any number of independent delays.
    Any thread can share it, because schedule() and cancel() hold the wheel's
    own lock for a short time only."""

    def __init__(self, name: str = "TimerWheel"):
        self._cond = threading.Condition()
        self._heap: list[tuple[float, int, TimerHandle]] = []
        self._seq_counter = itertools.count()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def schedule(self, delay_s: float, callback: Callable[[], Any], name: str = "TimerWheelJob") -> TimerHandle:
        """Arm a one-shot timer that calls callback() after delay_s seconds
        on its own dispatch thread. Returns a handle with .cancel()."""
        seq = next(self._seq_counter)
        due = time.monotonic() + delay_s
        handle = TimerHandle(self, seq, due, callback, name)
        with self._cond:
            heapq.heappush(self._heap, (due, seq, handle))
            self._cond.notify_all()
        return handle

    def _cancel(self, handle: TimerHandle) -> None:
        with self._cond:
            if handle._fired:
                return
            handle._cancelled = True
            # _run drops the handle once it reaches the front of the heap. A
            # scan-and-remove per cancel() costs more, and a late removal
            # changes nothing.
            self._cond.notify_all()

    def _run(self) -> None:
        with self._cond:
            while True:
                while self._heap and self._heap[0][2]._cancelled:
                    heapq.heappop(self._heap)

                if not self._heap:
                    self._cond.wait()
                    continue

                due, seq, handle = self._heap[0]
                remaining = due - time.monotonic()
                if remaining > 0:
                    self._cond.wait(timeout=remaining)
                    continue

                heapq.heappop(self._heap)
                if handle._cancelled:
                    continue
                handle._fired = True
                self._dispatch(handle)

    @staticmethod
    def _dispatch(handle: TimerHandle) -> None:
        # Run off the scheduler thread. A slow callback must not delay another
        # timer due at about the same time.
        t = threading.Thread(target=TimerWheel._run_callback, args=(handle,), name=handle._name, daemon=True)
        t.start()

    @staticmethod
    def _run_callback(handle: TimerHandle) -> None:
        try:
            handle._callback()
        except Exception:
            log.opt(exception=True).error(f"timer_wheel: callback '{handle._name}' raised")


# One process-wide wheel. The screensaver reset, the overlay hide and the
# hold timer all share this single scheduler thread.
_default_wheel = TimerWheel(name="TimerWheel")


def schedule(delay_s: float, callback: Callable[[], Any], name: str = "TimerWheelJob") -> TimerHandle:
    """Arm a one-shot timer on the process-wide wheel. See TimerWheel.schedule."""
    return _default_wheel.schedule(delay_s, callback, name=name)
