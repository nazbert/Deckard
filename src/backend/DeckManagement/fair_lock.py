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

# FIFO ticket lock, used as the Stream Deck per-device transport mutex
# (issue #164). CPython's threading.Lock is unfair: a thread that releases
# and immediately re-acquires usually wins against a waiter that has been
# parked for milliseconds. The transport serializes every read and write of
# one device on ONE mutex, so an unpaced write burst can repeatedly out-race
# the library's HID read poll -- input events then arrive coalesced and
# dials lag. Ordering acquisitions by arrival bounds the reader's wait at
# the chunk in flight plus whatever was already queued ahead of it (the
# writer only ever queues one chunk at a time), i.e. single-digit ms.
#
# Deliberately NOT reentrant: it stands in for a threading.Lock, and the
# transport takes it for exactly one chunk operation at a time. Reentrancy
# would also break the ticket invariant -- a re-entering holder queues
# behind itself -- so a recursive acquisition deadlocks here exactly as it
# does on the lock this replaces.


class FairLock:
    """A mutex that grants ownership in acquisition order.

    Drop-in for `threading.Lock` in its blocking, non-blocking and
    context-manager forms. `acquire()` takes a ticket and waits until it is
    served; `release()` serves the next ticket.
    """

    __slots__ = ("_cond", "_next_ticket", "_serving", "_abandoned")

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        # Tickets are handed out from _next_ticket and served in order.
        # _serving != _next_ticket means the lock is owned.
        self._next_ticket = 0
        self._serving = 0
        # Tickets whose waiter timed out; release() steps over them so the
        # queue can never stall behind a ticket nobody is waiting on.
        self._abandoned = set()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._cond:
            if not blocking:
                # Must not take a ticket that may go unclaimed: succeed only
                # when nothing is owned or queued.
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
                # Served, if only at the deadline -- the caller owns it.
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
        # notify_all, not notify: the waiters here are the transport reader
        # plus whichever thread is writing -- two or three at a time, so the
        # wasted wakeups are noise next to a USB chunk, and each waiter
        # re-checks its own ticket, which cannot lose a wakeup.
        self._cond.notify_all()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> Literal[False]:
        self.release()
        return False
