"""
Which cached pages the cache must not tear down.

Eviction is an in-memory teardown. It destroys the page's action objects and
pops its cache slot. A holder of an evicted page then drives dead objects, and
the next fetch for the same (controller, path) mints a second Page, so one key
carries two sets of action objects that register live event handlers. The cache
sees two holders by itself: a controller's active_page and its
screensaver-pending page. A pin is how every other holder says so. It is a
count of the holders that a teardown breaks.

The count is kept per Page object, not per (controller, path) key, because the
release must name what the acquire named. A path can be renamed under a holder,
and a cache entry can be replaced. There is one Page per (controller, path), so
the keyspace stays the cache's own.

There are two kinds of holder. holding() and bracket() below serve bracketed
work, and reserve_fetch() serves the one outstanding fetch of a deck. Each of
those states its own release rule. The lock rules live at PagePins.
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
    """Hold page against cache eviction for the body of the block.

    The release runs on the exception path too, and it names the page the
    acquire named. Tolerates a missing page manager and a None page."""
    # The action tick loop and the key handler run work that must outlive a
    # page switch, because a press that changes pages still owes its UP to the
    # page it was pressed on. The release must be structural, here or in a
    # finally, because a count that a raising body skips pins the page for the
    # life of the process. It is a count and not a flag, because the two
    # brackets overlap on one page routinely.
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
    """The holders that eviction cannot see for itself.

    One private lock guards the counts and the reservation table, and nothing
    else. It is the innermost lock of this subsystem.
    """

    # The page cache holds _pages_lock across is_pinned and reserve_fetch, and
    # a deck holds its page-load lock across the release at installation, so
    # the order is always the cache lock or the load lock first and this lock
    # second. Nothing calls out under it. The lock is re-entrant, because the
    # reservation calls are written in terms of the count calls.

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Holders per page, keyed by object identity. Page defines no __eq__,
        # so a key is the object. Weak, because every real holder keeps its own
        # reference. Nobody reads the count of a page that nothing holds, and a
        # deck torn down inside a bracket would strand its page here.
        self._counts: "weakref.WeakKeyDictionary[Page, int]" = weakref.WeakKeyDictionary()
        # The one outstanding fetch per deck, so the deck's next fetch retires
        # the reservation without the deck naming what it asked for last. Weak
        # for the reason the counts are. A strong value keeps a page the cache
        # already dropped, a deleted one included, until the next fetch.
        self._reservations: dict["DeckController", "weakref.ref[Page]"] = {}

    def pin(self, page: "Page | None") -> "Page | None":
        """Add a holder to page and return it, so the caller can bracket the
        work with the page the acquire named."""
        if page is None:
            return None
        with self._lock:
            self._counts[page] = self._counts.get(page, 0) + 1
        return page

    def unpin(self, page: "Page | None") -> None:
        """Drop a holder and forget the page at zero.

        An unmatched release is a no-op. A negative count would read a later
        holder's pin as released."""
        if page is None:
            return
        with self._lock:
            remaining = self._counts.get(page, 0) - 1
            if remaining > 0:
                self._counts[page] = remaining
            else:
                self._counts.pop(page, None)

    def count(self, page: "Page") -> int:
        """Return the holder count of page. For assertions about balance. The
        cache needs only is_pinned()."""
        with self._lock:
            return self._counts.get(page, 0)

    def is_pinned(self, page: "Page | None") -> bool:
        with self._lock:
            return page in self._counts

    def bracket(self, page: "Page | None", ready_to_clear: bool) -> "Page | None":
        """Pin (ready_to_clear False) or release (True) page, and return it.

        The action tick loop and the key handler must pass the page from the
        False call back to the True call. A re-read of active_page after the
        work releases the page a concurrent switch installed. That pins the
        worked page forever and unprotects the page a switch just installed."""
        if ready_to_clear:
            self.unpin(page)
            return page
        return self.pin(page)

    def reserve_fetch(self, page: "Page | None", deck_controller: "DeckController") -> None:
        """Make page this deck's outstanding fetch and retire the previous one.

        It pins before it releases, so a repeat fetch of one page keeps at
        least one holder throughout."""
        # get_page reserves what it returns, because nothing the cache sees
        # refers to the page between the fetch and the caller's load_page. A
        # release the caller owes would leak, so the reservation is bounded to
        # one per deck, retired by that deck's next fetch or by an
        # installation. An abandoned fetch therefore costs one unevictable page
        # on one deck. The bound is per deck, not per caller, so a second
        # caller's fetch retires the first caller's reservation. Window cycling
        # can retire the reservation the screensaver hand-off needs, and a
        # reload that calls load_page(active_page) releases another caller's
        # outstanding fetch.
        with self._lock:
            self.pin(page)
            previous = self._reservations.pop(deck_controller, None)
            if page is not None:
                self._reservations[deck_controller] = weakref.ref(page)
        self._retire(previous)

    def release_fetch(self, deck_controller: "DeckController") -> None:
        """Retire this deck's outstanding fetch.

        The page reached the deck, the caller gave it up, or the deck is gone.
        Each of those ends the reservation."""
        with self._lock:
            reference = self._reservations.pop(deck_controller, None)
        self._retire(reference)

    def _retire(self, reference: "weakref.ref[Page] | None") -> None:
        """Release a popped reservation outside the hold that popped it.

        Resolving the reference can give back the last strong reference to a
        Page. Dropping it runs a teardown that reaches plugin finalizers, which
        must not run under this leaf lock. A dead reference costs nothing."""
        page = reference() if reference is not None else None
        self.unpin(page)
