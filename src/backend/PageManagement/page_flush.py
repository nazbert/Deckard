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

* the canonical key a page file is known by everywhere in this process
  (``canonical_path`` below -- the document registry keys through it too),
* the per-file save lock registry, so two controllers showing the same page
  cannot interleave their backup/write on one file -- and so that a mutation
  of the page's content cannot interleave with either (``PageDocument.edit``
  takes this same lock: one file, one lock, held by whoever is changing the
  content or turning it into bytes),
* a per-file record of who still has edits the file has not seen,
* the timer that turns a burst of edits into a single write, and
* which page files this process has already copied into ``pages/backups/``.

THE PROTOCOL, IN FOUR CALLS

``mark_dirty(source)`` records that ``source``'s content is ahead of its file
and arms the write. ``flush_path(path)`` brings the file level again *now* -- and
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

Before this process first overwrites a page file, the file as this seam
found it is copied to ``pages/backups/<name>.json`` -- the copy the page
manager heals from when the primary reads back corrupt. That copy is taken
ONCE per path per session, not once per write: it is a full re-parse of the
file plus a byte copy, and taken per keystroke it bought a backup whose
contents chased the primary a fraction of a second behind.

Holding the state the seam found rather than the last one it wrote is the
point of doing it once, not the price of it. A backup is only ever read when
the primary is unreadable, and nothing here can make it unreadable: every
page file this process writes goes out as ``atomic_write_json``'s
temporary-then-replace, so a write that dies mid-flight leaves the previous
complete page, never half of two. (The one page write anywhere that is not
atomic is the rename's ``copy2`` onto the destination name it is claiming --
it can leave a partial file under that new name, but it cannot truncate the
page being renamed.) Corruption therefore arrives from outside -- a
filesystem fault, a full disk, another program editing the file -- and
against that, a copy from before this seam's writes is as serviceable as a
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
keystroke. ``discard_path`` waits on the same lock for the same bound, and
must: a discard that walked past a flush already inside its critical section
would be undone by that flush the moment it finished.

THREAD CONTRACT

All of it is callable from any thread: saves originate on the GTK main thread
(every editor widget), on plugin and action threads (through
``ActionCore.set_settings``), and on ad-hoc threads (the permission manager's
reload). ``_pending`` and the backup record are guarded by one lock, taken
only inside the per-path save lock or on its own -- never the other way
round, so the two can never deadlock against each other. Neither the write
nor the backup copy is done under ``_pending_guard``, so marking an edit
never waits on a flush.

The save lock is a LEAF and must stay one: while it is held, the only things
taken are stdlib file I/O, ``_pending_guard``, and the timer source's own
lock when the write cancels the timer that would have done it. That last one
keeps it a leaf in practice rather than by inspection: the wheel's cancel
never waits on a callback (each fire runs on a thread of its own, so the
wheel's lock is never held across one), and no wheel callback but this
module's own takes a page's save lock -- so there is no cycle for the two to
close. Locks held ABOVE it, never below: the controller's page-load lock (the
page switch flushes the outgoing page inside it), a document's load guard
(its fill reads the file through the barrier), and the page cache lock is
never held across it at all.

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

import os
import threading
import time
from typing import Any, Callable, Protocol

from loguru import logger as log

from src.backend import timer_wheel
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.atomic_json import atomic_write_json


class PageContent(Protocol):
    """Whoever holds a page's unwritten content, as the seam sees them.

    A ``Page`` and a ``PageDocument`` both satisfy this and both reach here.
    An edit made through a deck's Page marks the Page; an edit made through
    the document -- the page settings, the whole-file editor, the asset sweep
    -- marks the document, because a page no deck is showing has no Page
    object to mark. They hold the same dict either way, so the bytes are the
    same bytes; which one is recorded only decides whose methods produce
    them.
    """

    json_path: str

    def get_without_action_objects(self) -> dict[str, Any]:
        """The content as it goes into the file, live objects stripped."""
        ...

    def move_key_to_end(self, dictionary: dict[str, Any], key: str) -> None:
        """Re-file one top-level key last in the caller's snapshot."""
        ...

    def make_backup(self, json_path: str) -> None:
        """Copy `json_path` into pages/backups/ before it is overwritten."""
        ...


# How long after the last edit the write goes out, and how long a path may
# stay unwritten while edits keep arriving. One second is below the threshold
# at which a user reaches for the next thing (switching page, closing the
# window -- both of which flush synchronously anyway) and long enough to
# swallow a whole typed label; five seconds bounds what continuous typing can
# leave unwritten, which is the only case the trailing timer alone would
# defer indefinitely.
DEBOUNCE_S = 1.0
MAX_DIRTY_AGE_S = 5.0


def canonical_path(path: str) -> str:
    """The one key for a page file, whatever spelling names it.

    Everything per-page in this process -- the save lock, the pending record,
    the backup record, the document holding the content -- is keyed by this,
    because two spellings of one file that key differently are two owners of
    one file. A page reached through a symlinked data directory and the same
    page reached through the resolved one would take separate save locks (and
    interleave their backup and write), and a mark left under one spelling
    would be invisible to a read barrier asking under the other -- which does
    not merely delay that edit, it lets the re-read that follows the barrier
    erase it from memory as well.

    Resolution costs an lstat per path component, paid once per mark and once
    per barrier: about 8 us for a page under a normal data directory, against
    roughly 150 us for the write it guards, at a rate bounded by how fast a
    person types. A keystroke used to cost two fsyncs here.

    It lives here rather than with the document because this module is inside
    the headless engine's import closure and reaches for nothing beyond the
    stdlib; the document, which needs the page manager, imports it from here.
    """
    return os.path.realpath(path)


# One save lock per page file, shared across every Page object for that file:
# each controller showing the same page holds its OWN Page instance, so a
# per-object lock/semaphore can never order two controllers' saves of the same
# file. Grows one entry per distinct page file and never prunes -- bounded by
# the user's page count (tens), so no eviction is needed.
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def save_lock(path: str) -> threading.Lock:
    """The one lock for `path`'s page file.

    Public because it is not only the write's: a page's content and its bytes
    are two states of one file, so the document's mutation seam holds this
    while it changes the content, exactly as the flush holds it while it turns
    the content into bytes. Two locks over one file would only pose the
    question of which comes first.

    Canonicalizes rather than trusting its caller, even though every caller in
    this module has the key already: this registry is the whole of the "one
    file, one writer" guarantee, and a caller that keys it by a raw spelling
    hands one file two locks with nothing to notice. Resolving an
    already-resolved path returns it unchanged.
    """
    with _save_locks_guard:
        return _save_locks.setdefault(canonical_path(path), threading.Lock())


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
    """One file's outstanding edits: who has them, which name they were marked
    under, since when, and the timer that will write them.

    Identity matters: a mark that lands while a flush is mid-write REPLACES
    this object rather than mutating it, so the flush can tell "the edits I
    just wrote" from "edits that arrived after I read the dict" by comparing
    the entry it took, not the source it saw.

    `path` is the spelling the mark was made under, kept because the file has
    to be written and backed up by a name rather than by the key: the backup
    is filed under the page's basename, and the key is a resolved path that
    can carry a different one. It is the spelling as of the MARK, never
    whatever the source's json_path says when the timer fires -- a page move
    re-points that attribute in place.
    """

    __slots__ = ("source", "path", "first_marked", "handle")

    def __init__(self, source: PageContent, path: str, first_marked: float) -> None:
        self.source = source
        self.path = path
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
        # path -> the edits that path is still waiting for. The source is held
        # by a STRONG reference on purpose: the page cache evicts without
        # touching disk, and an evicted page with pending edits must still be
        # writable -- the next mint of that path reads the file through the
        # barrier, so it picks up exactly what this reference kept alive.
        self._pending: dict[str, _Pending] = {}
        # Page files this seam has already copied into pages/backups/. The
        # copy keeps the state a file was in BEFORE this seam overwrote it,
        # which is a property of the session and not of any one write, so it
        # is taken once per path. One entry per distinct page path, pruned
        # only by discard_path (which hands the file to another writer, so
        # the question reopens) -- bounded by the user's page count, like the
        # lock registry above.
        self._backed_up: set[str] = set()
        self._pending_guard = threading.Lock()
        self._scheduler: Scheduler = scheduler if scheduler is not None else TimerWheelScheduler()
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

    def mark_dirty(self, source: PageContent) -> None:
        """Record that `source` holds edits its file does not have yet, and
        arm the write.

        Cheap and non-blocking by contract: this runs on the GTK main thread
        once per keystroke, so it takes one uncontended lock, touches a dict
        and re-arms a timer. It never writes.

        One entry per FILE, last marker wins: every Page on a page and the
        document behind them read and write the one dict the page manager
        holds for that file, so whichever marked last is authoritative for the
        file either way.
        """
        with self._pending_guard:
            # Read under the guard, together with the map write: the page
            # move re-points json_path and only then discards the old key, so
            # a mark that reads the old path cannot be inserted after that
            # discard has run.
            path = source.json_path
            key = canonical_path(path)
            now = self._clock()
            previous = self._pending.get(key)
            entry = _Pending(source, path,
                             previous.first_marked if previous is not None else now)
            self._pending[key] = entry
            if previous is not None and previous.handle is not None:
                self._scheduler.cancel(previous.handle)
            # Trailing, but never past the age cap: once the deadline is
            # here the delay clamps to zero and the write goes out on the
            # next timer dispatch instead of being pushed one more second by
            # every further keystroke.
            deadline = entry.first_marked + MAX_DIRTY_AGE_S
            delay = min(DEBOUNCE_S, max(0.0, deadline - now))
            entry.handle = self._scheduler.schedule(delay, lambda: self._fire(key))

    def pending_source(self, path: str) -> "PageContent | None":
        """Who holds the edits `path` is still waiting for, or None.

        Whether a path is still ahead of its file, and who holds the edits --
        the seam's own state, readable so that the ordering rules stated here
        can be asserted from outside rather than taken on trust.
        """
        with self._pending_guard:
            entry = self._pending.get(canonical_path(path))
            return entry.source if entry is not None else None

    def _fire(self, key: str) -> None:
        """Timer dispatch. No caller to raise at, so a failure is logged."""
        try:
            self.flush_path(key)
        except Exception:
            log.opt(exception=True).error(f"Deferred write of page {key} failed")

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
        were marked under -- never whatever the source's `json_path` says by
        the time the timer fires. The page move re-points that attribute in
        place, so the two can name different files; writing the locked path
        keeps the lock and its file inseparable, and the move retires the
        stale key so the moved-from file is not written back into existence.
        """
        key = canonical_path(path)
        # Deliberately unguarded: a containment test is atomic, and losing
        # the race against a mark means the reader got here before the edit
        # it is not waiting for. Everything that decides anything re-reads
        # `_pending` under the guard below.
        if key not in self._pending:
            return

        with save_lock(key):
            with self._pending_guard:
                entry = self._pending.get(key)
            if entry is None:
                # Another thread's flush wrote it while this call waited on
                # the lock above.
                return
            source = entry.source

            try:
                self._back_up_once(key, entry.path, source)

                without_objects = source.get_without_action_objects()
                # Make keys last element
                for type in Input.KeyTypes:
                    source.move_key_to_end(without_objects, type)
                # Atomic replace, so an interrupted write can't leave a
                # truncated page.
                atomic_write_json(entry.path, without_objects)
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
                    if self._pending.get(key) is entry:
                        del self._pending[key]
                        if entry.handle is not None:
                            # The write is done; the timer that would have
                            # done it has nothing left to find.
                            self._scheduler.cancel(entry.handle)

    def _back_up_once(self, key: str, path: str, source: PageContent) -> None:
        """Copy `path` into pages/backups/ unless this session already has.

        Runs under the path's save lock and before the write it guards, so
        what lands in pages/backups/ is the file exactly as this seam found
        it -- and no other flush of the same file can slip between the check
        and the copy.

        A REFUSAL COUNTS AS DONE. make_backup returns without copying when
        the primary will not parse (copying it would destroy the very thing
        the backup is for) and when the primary is not there at all (there is
        nothing to copy, and the write about to follow recreates the file).
        Asking again on the next write would find that primary parseable --
        this seam wrote it a moment ago -- and put a duplicate of the current
        file over the last copy taken before the damage, which is the only
        copy worth having. So the first write of a path in a session settles
        that path's backup, whether it took one or declined to; what is left
        open for the next write is only the case where make_backup RAISES,
        which is now an unexpected I/O error -- nothing was read and nothing
        gets overwritten, because the exception takes the write with it.

        `path`, never the source's json_path: a page move re-points that
        attribute in place, and the file whose previous state has to be kept
        is the one this call locked and is about to overwrite.
        """
        with self._pending_guard:
            if key in self._backed_up:
                return
        # The copy itself is outside the guard: it re-parses and duplicates a
        # whole page file, and the guard is also taken by every mark_dirty on
        # the GTK main thread. Two flushes of one path cannot both be here
        # regardless -- the save lock is held for the length of this call.
        source.make_backup(path)
        with self._pending_guard:
            self._backed_up.add(key)

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

        UNDER THE PATH'S SAVE LOCK, like a flush, so a discard cannot land in
        the middle of one. A flush that is already inside its critical
        section has read its entry and may be anywhere between the backup and
        the write; a discard slipping past it would be undone twice over --
        the backup record re-added by the copy the flush is in the middle of,
        and the file itself overwritten with the pre-discard page after the
        caller had replaced it. Waiting means the flush finishes against the
        old file, and the delete, move or import that follows this call has
        the last word.

        The wait is bounded by one page write (or by one edit of the page's
        content, which is a handful of dict operations), the same bound and
        the same lock a read barrier already accepts, and every caller
        reaches here holding no other lock of its own: the save lock takes
        nothing else while held -- the backup, the snapshot and the atomic
        write are stdlib file I/O, and a document edit is dict mutation -- so
        it is a leaf, and acquiring it under a page manager or controller
        lock cannot invert anything.
        """
        key = canonical_path(path)
        with save_lock(key):
            with self._pending_guard:
                entry = self._pending.pop(key, None)
                self._backed_up.discard(key)
        if entry is not None and entry.handle is not None:
            self._scheduler.cancel(entry.handle)


# The process-wide flush seam. A module singleton rather than a `gl` slot: the
# point of naming this protocol is to shrink what lives on the shared
# namespace, not to add to it.
_flush = PageFlush()


def get() -> PageFlush:
    """The process-wide page flush. Never None."""
    return _flush
