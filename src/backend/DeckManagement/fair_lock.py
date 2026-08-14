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
import threading
import time
from typing import Literal

# FIFO ticket lock. It serves as the Stream Deck per-device transport mutex.
# CPython's threading.Lock is unfair. A thread that releases and immediately
# re-acquires beats a waiter parked for milliseconds, so an unpaced write
# burst out-races the library's HID read poll on this shared per-device
# mutex. Input events then arrive coalesced and dials lag. Ticket order
# bounds the reader's wait at the chunk in flight plus the chunks queued
# ahead of it. The writer queues one chunk at a time, so that bound is
# single-digit milliseconds.
#
# This lock is not reentrant, because the transport takes it for one chunk
# operation at a time. A re-entering holder queues behind itself and
# deadlocks, as it does on the threading.Lock this stands in for.


class FairLock:
    """A mutex that grants ownership in acquisition order.

    Replaces threading.Lock in its blocking, non-blocking and context-manager
    forms. acquire() takes a ticket and waits for it. release() serves the
    next ticket.
    """

    __slots__ = ("_cond", "_next_ticket", "_serving", "_abandoned")

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        # acquire() hands out tickets from _next_ticket and serves them in
        # order. _serving != _next_ticket means a thread owns the lock.
        self._next_ticket = 0
        self._serving = 0
        # Tickets whose waiter timed out. release() steps over them, so the
        # queue never stalls behind a ticket with no waiter.
        self._abandoned = set()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._cond:
            if not blocking:
                # Do not take a ticket that can go unclaimed. Succeed only
                # when no thread owns or queues for the lock.
                if self._serving != self._next_ticket:
                    return False
                self._next_ticket += 1
                return True

            ticket = self._next_ticket
            self._next_ticket += 1

            if timeout is None or timeout < 0:
                while self._serving != ticket:
                    self._cond.wait()
                return True

            deadline = time.monotonic() + timeout
            while self._serving != ticket:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)

            if self._serving == ticket:
                # Served at or before the deadline. The caller owns the lock.
                return True
            self._abandoned.add(ticket)
            return False

    def release(self) -> None:
        with self._cond:
            if self._serving == self._next_ticket:
                raise RuntimeError("release unlocked FairLock")
            self._advance_locked()

    def locked(self) -> bool:
        with self._cond:
            return self._serving != self._next_ticket

    def _advance_locked(self) -> None:
        # Caller holds self._cond.
        self._serving += 1
        while self._serving in self._abandoned:
            self._abandoned.discard(self._serving)
            self._serving += 1
        # Use notify_all and not notify. The waiters are the transport reader
        # and the writing thread, two or three at a time. The wasted wakeups
        # cost less
        # than a USB chunk, and each waiter re-checks its own ticket, so no
        # wakeup is lost.
        self._cond.notify_all()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> Literal[False]:
        self.release()
        return False
