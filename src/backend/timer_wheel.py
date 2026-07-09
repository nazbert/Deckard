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
# Single daemon scheduler thread, replacing the N sleeping threading.Timer
# threads that used to sit around per screensaver-reset-per-keypress,
# overlay-hide and key-hold delay (docs/memory-footprint-impl-plan.md P5.3).
# Every keypress used to cancel+recreate its own Timer (its own OS thread,
# parked in a timed wait); a single min-heap + Condition does the same job
# with exactly one thread asleep at a time, regardless of how many delays
# are outstanding.
#
# Handle semantics deliberately track threading.Timer close enough that the
# existing cancel-and-recreate call sites port with only an import change:
#   - schedule(delay_s, callback) arms immediately (mirrors `Timer(...);
#     .start()`) and returns a handle.
#   - handle.cancel() is idempotent and safe to call after the callback has
#     already fired -- it just can't un-fire it, same as Timer.cancel().
#
# The scheduler thread itself only pops due handles off the heap and hands
# them off; it never runs a callback inline. Ported callbacks are not cheap
# (ScreenSaver.show() prebuilds a background -- hashes the source file,
# possibly opens a video capture -- before it even takes a lock), so running
# one on the scheduler thread would delay every other pending timer in the
# process behind it (the "50 concurrent schedules" / "slow callback doesn't
# delay an unrelated timer" scenarios in scenario_timer_wheel.py exist
# precisely to catch that class of regression). Each fire is dispatched to
# its own short-lived daemon thread rather than GtkHelper's shared
# `@background` pool: that pool is documented I/O-bound-only and shared with
# plugin/asset work (8 workers total) -- routing screensaver/overlay/hold
# fires through it would (a) contend with unrelated plugin work and (b) pull
# a GTK-adjacent import into this module, which the test harness also
# exercises headless. Timer fires here are rare (per-keypress reset, one
# overlay-hide, one hold-timer) so a fresh daemon thread per fire is cheap
# and keeps this module a plain backend/ dependency.
import heapq
import itertools
import threading
import time

from loguru import logger as log


class TimerHandle:
    """Returned by TimerWheel.schedule(). Not constructed directly."""

    __slots__ = ("_wheel", "_seq", "_due", "_callback", "_name", "_cancelled", "_fired")

    def __init__(self, wheel: "TimerWheel", seq: int, due: float, callback: callable, name: str):
        self._wheel = wheel
        self._seq = seq
        self._due = due
        self._callback = callback
        self._name = name
        self._cancelled = False
        self._fired = False

    def cancel(self) -> None:
        """Prevent this timer from firing if it hasn't already. Idempotent,
        and a no-op (not an error) if it already fired -- matches
        threading.Timer.cancel()'s semantics so existing
        cancel-then-maybe-recreate call sites need no other changes."""
        self._wheel._cancel(self)

    def is_alive(self) -> bool:
        """True while still pending (not yet fired or cancelled). Named to
        match threading.Timer's method of the same name/purpose for ported
        call sites that probe it."""
        return not (self._cancelled or self._fired)


class TimerWheel:
    """One daemon scheduler thread backing arbitrarily many independent
    delays. Safe to share across threads; schedule()/cancel() only ever hold
    the wheel's own lock briefly."""

    def __init__(self, name: str = "TimerWheel"):
        self._cond = threading.Condition()
        self._heap: list[tuple[float, int, TimerHandle]] = []
        self._seq_counter = itertools.count()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def schedule(self, delay_s: float, callback: callable, name: str = "TimerWheelJob") -> TimerHandle:
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
            # Lazily dropped from the heap by _run when it next reaches the
            # front -- cheaper than a linear scan-and-remove per cancel(),
            # and correctness doesn't depend on prompt removal.
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
        # Off the scheduler thread: a slow/blocking callback must not delay
        # any other timer due around the same time.
        t = threading.Thread(target=TimerWheel._run_callback, args=(handle,), name=handle._name, daemon=True)
        t.start()

    @staticmethod
    def _run_callback(handle: TimerHandle) -> None:
        try:
            handle._callback()
        except Exception:
            log.opt(exception=True).error(f"timer_wheel: callback '{handle._name}' raised")


# One process-wide wheel: this is the whole point of P5.3 (screensaver reset,
# overlay hide and hold-timer all used to each spin up their own
# threading.Timer thread; now they share this single scheduler thread).
_default_wheel = TimerWheel(name="TimerWheel")


def schedule(delay_s: float, callback: callable, name: str = "TimerWheelJob") -> TimerHandle:
    """Arm a one-shot timer on the process-wide wheel. See TimerWheel.schedule."""
    return _default_wheel.schedule(delay_s, callback, name=name)
