"""
The page-flush seam: the one place that turns a Page's in-memory dict into
the bytes in ``pages/<name>.json``.

Every ``Page.save()`` used to carry the write itself -- backup, snapshot, key
reorder, atomic replace -- inline in the mutator that called it. That put the
question "when is a page written?" in ~30 call sites at once. Here it is one
question with one answer, asked in one place.

WHAT THIS MODULE OWNS

* the per-path save lock registry (below), so two controllers showing the same
  page cannot interleave their backup/write on one file, and
* a per-path record of which Page still has edits the file has not seen.

THE PROTOCOL, IN TWO CALLS

``mark_dirty(page)`` records that ``page``'s dict is ahead of its file.
``flush_path(path)`` brings the file level again -- and is a single dict
lookup when the path has nothing pending, which is what makes it cheap enough
to sit in front of every read of a page file.

Today ``Page.save()`` calls both, back to back: a save is on disk by the time
it returns, exactly as it always was. The seam exists so that the second call
can move away from the first without any mutator noticing.

THE READ BARRIER

Anything that reads a page file from disk must call ``flush_path`` first --
``PageManagerBackend.get_page_data``, the asset sweep's per-file read, and the
page export. A reader that skips it reads the file, not the page.

THREAD CONTRACT

All three methods are callable from any thread: saves originate on the GTK
main thread (every editor widget), on plugin and action threads (through
``ActionCore.set_settings``), and on ad-hoc threads (the permission manager's
reload). ``_pending`` is guarded by its own lock, taken only inside the
per-path save lock or on its own -- never the other way round, so the two can
never deadlock against each other.

FAILURE

A flush that raises (an unserializable value in the dict is the shipped
example) propagates to its caller with the file untouched -- ``atomic_json``
writes a temporary and replaces, so a failed serialization cannot truncate the
primary. The pending record is retired either way, so a failure leaves nothing
behind to re-raise at some later reader: an edit that could not be serialized
is not retried, which is precisely what an inline save did.

Imports ``atomic_json`` and ``InputIdentifier`` -- stdlib-only modules with no
widget or ``gi`` reach -- because ``Page`` is in the headless engine's import
closure and everything it imports is too.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.atomic_json import atomic_write_json

if TYPE_CHECKING:
    from src.backend.PageManagement.Page import Page


# One save lock per page json path, shared across every Page object for that
# path: each controller showing the same page holds its OWN Page instance, so
# a per-object lock/semaphore can never order two controllers' saves of the
# same file. Grows one entry per distinct page path and never
# prunes -- bounded by the user's page count (tens), so no eviction is needed.
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def _get_save_lock(json_path: str) -> threading.Lock:
    with _save_locks_guard:
        return _save_locks.setdefault(json_path, threading.Lock())


class PageFlush:
    """Which pages are ahead of their files, and how to get a file level."""

    def __init__(self) -> None:
        # path -> the Page whose dict the file has not seen yet. A STRONG
        # reference on purpose: the page cache evicts without touching disk,
        # and an evicted page with pending edits must still be writable.
        self._pending: dict[str, Page] = {}
        self._pending_guard = threading.Lock()

    def mark_dirty(self, page: "Page") -> None:
        """Record that `page` holds edits its file does not have yet.

        One entry per path, last marker wins: two Page objects for one path
        are kept in sync by the page manager, so the newest is authoritative
        for the file either way.
        """
        with self._pending_guard:
            self._pending[page.json_path] = page

    def pending_page(self, path: str) -> "Page | None":
        """The Page whose edits `path` is still waiting for, or None.

        The authority question, asked by anything that would otherwise re-read
        the file to find out what the page says.
        """
        with self._pending_guard:
            return self._pending.get(path)

    def flush_path(self, path: str) -> None:
        """Put `path`'s pending edits on disk. A no-op when it has none.

        The clean case is one dict lookup and a return -- cheap enough to sit
        in front of every read of a page file.

        The entry is RETIRED AFTER the write, not claimed before it, and both
        happen under the per-path save lock. That ordering is what stops a
        reader from overtaking a write already in flight: an entry taken up
        front would leave the map empty while the bytes were still being
        written, and the fast path above would wave the reader straight
        through to the stale file. Retiring last means the map says "pending"
        for exactly as long as the file is behind, so a concurrent flush
        either waits on the lock and finds the work done, or finds a mark
        that landed mid-write and does it again.
        """
        if path not in self._pending:
            return

        with _get_save_lock(path):
            with self._pending_guard:
                page = self._pending.get(path)
            if page is None:
                # Another thread's flush wrote it while this call waited on
                # the lock above.
                return

            try:
                # Make backup in case something goes wrong
                page.make_backup()

                without_objects = page.get_without_action_objects()
                # Make keys last element
                for type in Input.KeyTypes:
                    page.move_key_to_end(without_objects, type)
                # Atomic replace, so an interrupted write can't leave a
                # truncated page.
                atomic_write_json(page.json_path, without_objects)
            finally:
                with self._pending_guard:
                    # Only retire the mark that was just written: a save that
                    # landed mid-write is newer than these bytes and stays
                    # pending for its own flush. Retiring in a `finally`
                    # keeps a failed write from leaving a mark behind that
                    # would re-raise at whichever reader touched the page
                    # next -- an edit that cannot be serialized is not
                    # retried, exactly as an inline save never retried one.
                    if self._pending.get(path) is page:
                        del self._pending[path]


# The process-wide flush seam. A module singleton rather than a `gl` slot: the
# point of naming this protocol is to shrink what lives on the shared
# namespace, not to add to it.
_flush = PageFlush()


def get() -> PageFlush:
    """The process-wide page flush. Never None."""
    return _flush
