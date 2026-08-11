"""
Which cached pages the cache is not allowed to tear down.

Evicting a page is an in-memory teardown: its action objects are destroyed and
its cache slot is popped. That is correct for a page nothing is using and
catastrophic for one something is: the holder keeps its reference and drives
dead objects, and the next fetch for the same (controller, path) mints a
SECOND Page, so one key ends up with two sets of action objects registering
live event handlers. The cache can see two kinds of holder by itself -- a
controller's ``active_page`` and its screensaver-pending page -- and it is
blind to the rest. A pin is how the rest say so: a count of holders that would
be broken by a teardown.

Kept per Page OBJECT, not per (controller, path) key, because the release has
to name the same thing the acquire named. Paths get renamed under a holder and
cache entries get replaced; the object identity a caller is holding is the one
thing that cannot change underneath it, so acquire returns the page and
release takes it back. There is one Page per (controller, path) either way, so
the keyspace is the same one the cache uses -- only sturdier.

TWO KINDS OF HOLDER, AND ONLY TWO

**Bracketed work** -- the action tick loop and the key handler, which both run
work that must outlive a page switch (a press that changes pages still owes
its UP to the page it was pressed on). Its release is STRUCTURAL: ``holding``
below, or a ``finally`` where the block cannot be reshaped, because a count
that a raising body skips is permanent. The flag this replaced healed itself
on the next mark; a count does not, and a bracket that raises once per press
on a caller that swallows tracebacks would pin a page per press until nothing
is evictable at all. It is a COUNT rather than a flag because the two brackets
overlap on one page routinely -- a press during a tick -- and with a flag the
first release ends the protection the second is still relying on.

**The fetch reservation** -- ``get_page`` reserves whatever it returns,
because between the fetch and the caller's ``load_page`` the page is
referenced by nothing the cache can see. This release is the one that cannot
be written as a matching call: the caller may abandon the page, raise before
activating it, or hand it to a deck that refuses it, and a reservation that
depends on such a caller is a reservation that leaks. So it is BOUNDED
instead: one reservation per deck, and that deck's next fetch retires the
previous one, as does installing a page on it. An abandoned fetch therefore
costs one unevictable page on one deck until its next fetch or load, never an
accumulating leak.

The bound is per DECK, not per caller, and that asymmetry is real: a deck has
one reservation, so a second caller's fetch retires the first caller's --
window cycling can retire the reservation the screensaver hand-off is relying
on, and a reload that calls ``load_page(active_page)`` releases an unrelated
fetch that is still in flight. Every one of those windows is protected
strictly better than it was with no reservation at all, and none of them is
protected absolutely. Making it absolute means a release the caller owes, and
the callers are exactly the ones that cannot be trusted to pay it.

LOCKING

One private lock, guarding the counts and the reservation table, and held for
nothing else. It is the innermost lock in this subsystem: the page cache's
``_pages_lock`` is held across ``is_pinned`` and ``reserve_fetch``, and a
deck's page-load lock is held across the release at installation, so the order
is always (cache or load lock) -> this one. Nothing here calls out under it --
including the one call that is easy to miss, dropping the last reference to a
Page, whose teardown can reach a plugin's ``__del__``. That is why the
reservation is released outside the hold. Re-entrant because the reservation
calls are written in terms of the count calls.
"""
from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import globals as gl

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.PageManagement.Page import Page


@contextmanager
def holding(page: "Page | None") -> Iterator["Page | None"]:
    """Holds `page` against cache eviction for the body of the block.

    The bracket form for work a page switch must not cut short. Structural on
    purpose: the release runs on the exception path too, and it names the page
    the acquire named rather than whatever ``active_page`` has become by the
    end -- the two ways a hand-written bracket loses a page to a permanent
    pin. Tolerates there being no page manager (a harness tier, or before
    startup builds one) so no caller has to guard for it, and tolerates a None
    page for the same reason."""
    manager = gl.page_manager
    pins = manager.pins if manager is not None else None
    if pins is not None:
        pins.pin(page)
    try:
        yield page
    finally:
        if pins is not None:
            pins.unpin(page)


class PagePins:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Holders per page, keyed by object identity (Page defines no __eq__,
        # so a key IS the object). Weak, because a pin is a statement ABOUT a
        # page rather than a claim on it: every real holder has its own
        # reference, so a page nothing holds any more is one whose count
        # nobody will ever read again -- and a deck torn down mid-bracket
        # would otherwise strand its page here for the life of the process.
        self._counts: "weakref.WeakKeyDictionary[Page, int]" = weakref.WeakKeyDictionary()
        # The one outstanding fetch per deck, so a fetch's reservation can be
        # retired by the deck's next fetch without the deck knowing what it
        # last asked for. Weak for the same reason the counts are: a pin says
        # something about a page, it does not own one, and a strong value here
        # would keep a page the cache has already dropped -- a deleted one,
        # say -- alive until the deck happened to fetch again.
        self._reservations: dict["DeckController", "weakref.ref[Page]"] = {}

    def pin(self, page: "Page | None") -> "Page | None":
        """Adds a holder to `page` and returns it, so a caller can bracket
        work with the page the acquire actually named."""
        if page is None:
            return None
        with self._lock:
            self._counts[page] = self._counts.get(page, 0) + 1
        return page

    def unpin(self, page: "Page | None") -> None:
        """Drops a holder, forgetting the page at zero. Unmatched releases are
        no-ops rather than negative counts, where a later real holder's pin
        would read as already released."""
        if page is None:
            return
        with self._lock:
            remaining = self._counts.get(page, 0) - 1
            if remaining > 0:
                self._counts[page] = remaining
            else:
                self._counts.pop(page, None)

    def count(self, page: "Page") -> int:
        """How many holders `page` has. For assertions about balance -- the
        cache itself only ever needs is_pinned()."""
        with self._lock:
            return self._counts.get(page, 0)

    def is_pinned(self, page: "Page | None") -> bool:
        with self._lock:
            return page in self._counts

    def bracket(self, page: "Page | None", ready_to_clear: bool) -> "Page | None":
        """Pins (ready_to_clear False) or releases (True) `page`, returning it.

        The two callers -- the action tick loop and the key handler -- MUST
        pass the page returned by the False-call back to the True-call.
        Re-dereferencing ``active_page`` after the work instead releases
        whatever page a concurrent switch installed in the meantime, which
        leaves the page that was actually worked on pinned forever
        (unevictable, silently shrinking the eviction budget) and drops the
        protection from a page that a moment ago had none."""
        if ready_to_clear:
            self.unpin(page)
            return page
        return self.pin(page)

    def reserve_fetch(self, page: "Page | None", deck_controller: "DeckController") -> None:
        """Makes `page` this deck's outstanding fetch, retiring the previous
        one. Pin-before-release so a repeat fetch of the same page never dips
        to zero holders in between."""
        with self._lock:
            self.pin(page)
            previous = self._reservations.pop(deck_controller, None)
            if page is not None:
                self._reservations[deck_controller] = weakref.ref(page)
        self._retire(previous)

    def release_fetch(self, deck_controller: "DeckController") -> None:
        """Retires this deck's outstanding fetch -- the page reached the deck,
        the caller gave up on it, or the deck itself is gone. Every one of
        those means this registry must stop reserving the page."""
        with self._lock:
            reference = self._reservations.pop(deck_controller, None)
        self._retire(reference)

    def _retire(self, reference: "weakref.ref[Page] | None") -> None:
        """Releases a popped reservation OUTSIDE the hold that popped it.

        Resolving the reference can hand back the last strong reference to a
        Page in the process, and letting go of it runs page teardown, which
        reaches plugin-owned objects and their finalizers -- the one thing
        that could call out from under this leaf lock. unpin takes the lock
        for itself; the page staying pinned across the hop is the conservative
        direction. A reference whose page is already gone costs nothing:
        unpin(None) is a no-op."""
        page = reference() if reference is not None else None
        self.unpin(page)
