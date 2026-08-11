"""
The page-flush seam: the one place that turns a Page's in-memory dict into
the bytes in ``pages/<name>.json``, and the one place that decides *when*.

Every ``Page.save()`` used to carry the write itself -- backup, snapshot, key
reorder, atomic replace -- inline in the mutator that called it. That put the
question "when is a page written?" in ~30 call sites at once, and answered it
"now, twice per keystroke, on the GTK main thread" (a page edit is one
``json.dump`` plus an fsync of the file and an fsync of the directory; the
label editor calls it on every "changed" signal). Here it is one question
with one answer, asked in one place.

WHAT THIS MODULE OWNS

* the per-path save lock registry (below), so two controllers showing the same
  page cannot interleave their backup/write on one file,
* a per-path record of which Page still has edits the file has not seen,
* the timer that turns a burst of edits into a single write, and
* which page files this process has already copied into ``pages/backups/``.

THE PROTOCOL, IN FOUR CALLS

``mark_dirty(page)`` records that ``page``'s dict is ahead of its file and
arms the write. ``flush_path(path)`` brings the file level again *now* -- and
is a single dict lookup when the path has nothing pending, which is what
makes it cheap enough to sit in front of every read of a page file.
``flush_all()`` does that for every path with something outstanding;
``discard_path(path)`` throws pending edits away, which is what a page being
deleted needs (flushing one would write the file back into existence).

WHEN THE WRITE HAPPENS

``mark_dirty`` cancels any armed timer and re-arms a trailing one, so a burst
of edits costs one write ``DEBOUNCE_S`` after the last of them. Continuous
typing would otherwise defer the write forever, so the timer is also never
allowed past ``MAX_DIRTY_AGE_S`` after the *first* unwritten edit: once that
deadline is reached the re-arm is for zero seconds, i.e. the next write goes
out immediately and starts a fresh window.

The window only reopens on a write that completes with nothing marked behind
it: a mark landing *inside* a capped write inherits the older
``first_marked``, so it is born already past the deadline and stays clamped
to an immediate write until one completes mark-free. At any normal write
speed that is a single extra write and the debounce is back; where writes
take longer than the user's typing gaps, it degrades toward one write per
edit -- the pre-debounce behaviour, and the safe direction to degrade in,
since the alternative (restarting the clock on every mark) would buy back the
coalescing by widening exactly the loss window the cap exists to bound.

THE BACKUP

Before this process first overwrites a page file, the file as this session
found it is copied to ``pages/backups/<name>.json`` -- the copy the page
manager heals from when the primary reads back corrupt. That copy is taken
ONCE per path per session, not once per write: it is a full re-parse of the
file plus a byte copy, and taken per keystroke it bought a backup whose
contents chased the primary a fraction of a second behind.

Holding the session's opening state rather than the last saved one is the
point of doing it once, not the price of it. A backup is only ever read when
the primary is unreadable, and nothing here can make it unreadable: every
page file this process writes goes out as ``atomic_write_json``'s
temporary-then-replace, so a write that dies mid-flight leaves the previous
complete page, never half of two. Corruption therefore arrives from outside
-- a filesystem fault, a full disk, another program editing the file -- and
against that, a copy from before this session's edits is as serviceable as a
copy from a second ago, while being the only one that predates whatever went
wrong. History older than this session is somebody else's job and already
done: the boot backup zips every page file into a timestamped archive and
keeps the last few of them.

Marking never writes. Every deferred write happens on a timer thread, which
is the point: the fsync pair leaves the GTK main thread. What a user
perceives as "done" still writes synchronously, on the thread that got there
-- page switch, deck close, quit, page move -- and so does any read of the
file (below), so the deferral is invisible except in how often the disk is
touched. The exposure it buys is bounded and stated plainly: a crash or power
loss loses at most the last ``DEBOUNCE_S`` of edits (``MAX_DIRTY_AGE_S``
under a continuous burst). The file itself is never at risk -- the write is
the same atomic tmp+replace it always was, so the primary is either the
previous complete page or the new one.

THE READ BARRIER

Anything that reads a page file from disk must call ``flush_path`` (or
``flush_all``) first, or it reads the file rather than the page. Barriers
live at the page manager's ``get_page_data``, the asset sweep's per-file
read, the page export, the duplicate's read-back, the page move's copy, the
boot backup zip and the video cache sweep; the importers ``discard_path``
instead, because they overwrite the page wholesale. That list is the reader
map as it stands, and it is a precondition for deferring at all: a reader
without a barrier sees a page as it was up to a second ago.

A barrier can make a GTK-thread reader wait for a write already in flight on
a timer thread -- bounded by one page write, and the trade the deferral is
for: one possible wait on a read against a guaranteed pair of fsyncs on every
keystroke.

THREAD CONTRACT

All of it is callable from any thread: saves originate on the GTK main thread
(every editor widget), on plugin and action threads (through
``ActionCore.set_settings``), and on ad-hoc threads (the permission manager's
reload). ``_pending`` and the backup record are guarded by one lock, taken
only inside the per-path save lock or on its own -- never the other way
round, so the two can never deadlock against each other. Neither the write
nor the backup copy is done under ``_pending_guard``, so marking an edit
never waits on a flush.

FAILURE

A flush that raises (an unserializable value in the dict is the shipped
example) propagates to its caller with the file untouched -- ``atomic_json``
writes a temporary and replaces, so a failed serialization cannot truncate
the primary. The pending record is retired either way, so a failure leaves
nothing behind to re-raise at some later reader: an edit that could not be
serialized is not retried, which is precisely what an inline save did. A
timer-driven flush has no caller to raise at, so it logs instead;
``flush_all`` logs per path too, because its callers are shutdown paths that
must not abort on one bad page.

Imports ``atomic_json``, ``timer_wheel`` and ``InputIdentifier`` -- stdlib +
loguru modules with no widget or ``gi`` reach -- because ``Page`` is in the
headless engine's import closure and everything it imports is too. The timer
is the process wheel rather than a GLib timeout for three independent
reasons: GLib here would put ``gi`` in that closure; saves originate on
threads with no main loop to run a GLib source; and a GLib timeout would put
the fsync back on the GTK main thread, which is the whole thing being fixed.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Protocol

from loguru import logger as log

from src.backend import timer_wheel
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.atomic_json import atomic_write_json

if TYPE_CHECKING:
    from src.backend.PageManagement.Page import Page


# How long after the last edit the write goes out, and how long a path may
# stay unwritten while edits keep arriving. One second is below the threshold
# at which a user reaches for the next thing (switching page, closing the
# window -- both of which flush synchronously anyway) and long enough to
# swallow a whole typed label; five seconds bounds what continuous typing can
# leave unwritten, which is the only case the trailing timer alone would
# defer indefinitely.
DEBOUNCE_S = 1.0
MAX_DIRTY_AGE_S = 5.0


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


class Scheduler(Protocol):
    """The two operations the deferral needs from a timer source."""

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> Any:
        """Arm a one-shot timer and return a handle for ``cancel``."""
        ...

    def cancel(self, handle: Any) -> None:
        """Disarm a timer that has not fired yet. Idempotent after a fire."""
        ...


class TimerWheelScheduler:
    """``Scheduler`` backed by the process timer wheel.

    Each fire gets its own short-lived daemon thread (the wheel's own
    contract), so a slow page write delays no other timer -- and, being a
    daemon, dies at ``os._exit``. That is why quit flushes explicitly rather
    than trusting an armed timer to still be there.
    """

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> Any:
        return timer_wheel.schedule(delay_s, callback, name="page_flush")

    def cancel(self, handle: Any) -> None:
        handle.cancel()


class _Pending:
    """One path's outstanding edits: who has them, since when, and the timer
    that will write them.

    Identity matters: a mark that lands while a flush is mid-write REPLACES
    this object rather than mutating it, so the flush can tell "the edits I
    just wrote" from "edits that arrived after I read the dict" by comparing
    the entry it took, not the page it saw.
    """

    __slots__ = ("page", "first_marked", "handle")

    def __init__(self, page: "Page", first_marked: float) -> None:
        self.page = page
        self.first_marked = first_marked
        self.handle: Any = None


class PageFlush:
    """Which pages are ahead of their files, when the file catches up, and
    how.

    The scheduler and clock are constructor arguments so the deferral can be
    driven in virtual time with no sleeps and no wall-clock waits: a headless
    test supplies a scheduler that fires when told and a clock it advances
    itself, and every timing rule above becomes an assertion rather than a
    race.
    """

    def __init__(self, scheduler: "Scheduler | None" = None,
                 clock: "Callable[[], float] | None" = None) -> None:
        # path -> the edits that path is still waiting for. The Page is held
        # by a STRONG reference on purpose: the page cache evicts without
        # touching disk, and an evicted page with pending edits must still be
        # writable -- the next mint of that path reads the file through the
        # barrier, so it picks up exactly what this reference kept alive.
        self._pending: dict[str, _Pending] = {}
        # Page files this process has already copied into pages/backups/. The
        # copy keeps the state a file was in BEFORE this seam overwrote it,
        # which is a property of the session and not of any one write, so it
        # is taken once per path. Grows one entry per distinct page path and
        # never prunes -- bounded by the user's page count, like the lock
        # registry above.
        self._backed_up: set[str] = set()
        self._pending_guard = threading.Lock()
        self._scheduler: Scheduler = scheduler if scheduler is not None else TimerWheelScheduler()
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

    def mark_dirty(self, page: "Page") -> None:
        """Record that `page` holds edits its file does not have yet, and arm
        the write.

        Cheap and non-blocking by contract: this runs on the GTK main thread
        once per keystroke, so it takes one uncontended lock, touches a dict
        and re-arms a timer. It never writes.

        One entry per path, last marker wins: two Page objects for one path
        are kept in sync by the page manager, so the newest is authoritative
        for the file either way.
        """
        with self._pending_guard:
            # Read under the guard, together with the map write: the page
            # move re-points json_path and only then discards the old key, so
            # a mark that reads the old path cannot be inserted after that
            # discard has run.
            path = page.json_path
            now = self._clock()
            previous = self._pending.get(path)
            entry = _Pending(page, previous.first_marked if previous is not None else now)
            self._pending[path] = entry
            if previous is not None and previous.handle is not None:
                self._scheduler.cancel(previous.handle)
            # Trailing, but never past the age cap: once the deadline is
            # here the delay clamps to zero and the write goes out on the
            # next timer dispatch instead of being pushed one more second by
            # every further keystroke.
            deadline = entry.first_marked + MAX_DIRTY_AGE_S
            delay = min(DEBOUNCE_S, max(0.0, deadline - now))
            entry.handle = self._scheduler.schedule(delay, lambda: self._fire(path))

    def pending_page(self, path: str) -> "Page | None":
        """The Page whose edits `path` is still waiting for, or None.

        The authority question, asked by anything that would otherwise re-read
        the file to find out what the page says.
        """
        with self._pending_guard:
            entry = self._pending.get(path)
            return entry.page if entry is not None else None

    def _fire(self, path: str) -> None:
        """Timer dispatch. No caller to raise at, so a failure is logged."""
        try:
            self.flush_path(path)
        except Exception:
            log.opt(exception=True).error(f"Deferred write of page {path} failed")

    def flush_path(self, path: str) -> None:
        """Put `path`'s pending edits on disk now. A no-op when it has none.

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

        The file written is the one this call locked -- the key the edits
        were marked under -- never whatever `page.json_path` says by the time
        the timer fires. The page move re-points that attribute in place, so
        the two can name different files; writing the locked path keeps the
        lock and its file inseparable, and the move retires the stale key so
        the moved-from file is not written back into existence.
        """
        # Deliberately unguarded: a containment test is atomic, and losing
        # the race against a mark means the reader got here before the edit
        # it is not waiting for. Everything that decides anything re-reads
        # `_pending` under the guard below.
        if path not in self._pending:
            return

        with _get_save_lock(path):
            with self._pending_guard:
                entry = self._pending.get(path)
            if entry is None:
                # Another thread's flush wrote it while this call waited on
                # the lock above.
                return
            page = entry.page

            try:
                self._back_up_once(path, page)

                without_objects = page.get_without_action_objects()
                # Make keys last element
                for type in Input.KeyTypes:
                    page.move_key_to_end(without_objects, type)
                # Atomic replace, so an interrupted write can't leave a
                # truncated page.
                atomic_write_json(path, without_objects)
            finally:
                with self._pending_guard:
                    # Only retire the entry that was just written: a save
                    # that landed mid-write replaced it with a newer one,
                    # which is ahead of these bytes and keeps its own timer.
                    # Retiring in a `finally` keeps a failed write from
                    # leaving a mark behind that would re-raise at whichever
                    # reader touched the page next -- an edit that cannot be
                    # serialized is not retried, exactly as an inline save
                    # never retried one.
                    if self._pending.get(path) is entry:
                        del self._pending[path]
                        if entry.handle is not None:
                            # The write is done; the timer that would have
                            # done it has nothing left to find.
                            self._scheduler.cancel(entry.handle)

    def _back_up_once(self, path: str, page: "Page") -> None:
        """Copy `path` into pages/backups/ unless this session already has.

        Runs under the path's save lock and before the write it guards, so
        what lands in pages/backups/ is the file exactly as this seam found
        it -- and no other flush of the same file can slip between the check
        and the copy.

        A REFUSAL COUNTS AS DONE. make_backup declines to copy a primary it
        cannot parse, which is the one case where copying would destroy the
        very thing the backup is for. Asking again on the next write would
        find that primary parseable -- this seam wrote it a moment ago -- and
        put a duplicate of the current file over the last copy that predates
        the corruption, which is the only copy worth having. So the first
        write of a path in a session settles that path's backup either way,
        and only a make_backup that raises (nothing was read, nothing was
        overwritten) leaves the question open for the next write.

        `path`, never page.json_path: a page move re-points that attribute in
        place, and the file whose previous state has to be kept is the one
        this call locked and is about to overwrite.
        """
        with self._pending_guard:
            if path in self._backed_up:
                return
        # The copy itself is outside the guard: it re-parses and duplicates a
        # whole page file, and the guard is also taken by every mark_dirty on
        # the GTK main thread. Two flushes of one path cannot both be here
        # regardless -- the save lock is held for the length of this call.
        page.make_backup(path)
        with self._pending_guard:
            self._backed_up.add(path)

    def flush_all(self) -> None:
        """Put every path's pending edits on disk now.

        For the paths that read everything (the boot backup zip, the video
        cache sweep) and for quit, where the timer threads are about to die
        with the process. Never raises: its callers are shutdown and sweep
        paths that must not abort because one page will not serialize.
        """
        with self._pending_guard:
            paths = list(self._pending)
        for path in paths:
            try:
                self.flush_path(path)
            except Exception:
                log.opt(exception=True).error(f"Could not write pending edits of page {path}")

    def discard_path(self, path: str) -> None:
        """Throw away `path`'s pending edits without writing them.

        For a page that is going away: flushing a deleted page would write
        the file back into existence, and flushing the source of a move would
        write a file the move is about to remove.

        The backup record goes with them. Every caller of this hands the file
        to somebody else -- a delete, a move, an import -- so whatever stands
        at this path afterwards is content this seam has never overwritten
        and no backup in pages/backups/ describes: the old copy is of a page
        that is gone, or of the version an import just replaced, and a heal
        from it would put that page back over the one the user has now. So
        the next flush of this path takes its own copy, which keeps the rule
        exactly what it says it is -- the backup is the file as it was before
        this seam first wrote it -- with "before" starting again each time
        somebody else takes the file over wholesale.
        """
        with self._pending_guard:
            entry = self._pending.pop(path, None)
            self._backed_up.discard(path)
        if entry is not None and entry.handle is not None:
            self._scheduler.cancel(entry.handle)


# The process-wide flush seam. A module singleton rather than a `gl` slot: the
# point of naming this protocol is to shrink what lives on the shared
# namespace, not to add to it.
_flush = PageFlush()


def get() -> PageFlush:
    """The process-wide page flush. Never None."""
    return _flush
