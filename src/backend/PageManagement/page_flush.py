"""
The page-flush seam is the one place that turns a Page's in-memory dict into
the bytes in pages/<name>.json, and the one place that decides when.

An inline write in each mutator asks that question at about 30 call sites and
answers it "now, twice per keystroke, on the GTK main thread". A page write is
one json.dump plus an fsync of the file and an fsync of the directory, and the
label editor saves on every changed signal. Here the question has one answer,
in one place.

What this module owns

* the canonical key a page file is known by in this process (canonical_path
  below, which the document registry keys through too),
* the per-file save lock registry, so two controllers on one page cannot
  interleave their backup and write, and so a mutation of the page's content
  cannot interleave with either. PageDocument.edit takes this same lock, so
  one file has one lock, held by whoever changes the content or writes it,
* a per-file record of who holds edits the file does not have,
* the timer that turns a burst of edits into one write, and
* which page files this process copied into pages/backups/ already.

The protocol, in four calls

mark_dirty(source) records that the content of source is ahead of its file, and
arms the write. flush_path(path) brings the file level now. It is one dict
lookup when the path has nothing pending, which is what makes it cheap enough
to sit in front of every read of a page file. flush_all() does the same for
every path with something outstanding. discard_path(path) throws pending edits
away, which a page deletion needs, because a flush would write the file back
into existence.

When the write happens

mark_dirty cancels an armed timer and re-arms a trailing one, so a burst of
edits costs one write DEBOUNCE_S after the last edit. Continuous typing would
defer that write forever, so the timer never goes past MAX_DIRTY_AGE_S after
the first unwritten edit. At that deadline the re-arm is for zero seconds, so
the next write goes out immediately and starts a new window.

The window reopens only on a write that completes with nothing marked behind
it. A mark that lands inside a capped write inherits the older first_marked, so
it starts past the deadline and stays clamped to an immediate write until one
write completes mark-free. At a normal write speed that costs one extra write
and the debounce returns. Where a write takes longer than the user's typing
gaps, it degrades toward one write per edit, which is the safe direction. A
clock restart on every mark would buy the coalescing back by widening the loss
window that the cap bounds.

The backup

Before this process overwrites a page file the first time, it copies the file
as this seam found it to pages/backups/<name>.json. The page manager heals from
that copy when the primary reads back corrupt. The copy is taken once per path
per session, and not once per write. It is a full re-parse of the file plus a
byte copy, and one copy per keystroke gives a backup that chases the primary a
fraction of a second behind.

One copy per session keeps the state the seam found. A backup is read only when
the primary is unreadable, and nothing here can make it unreadable. Every page
file goes out through the temporary-then-replace of atomic_write_json, so an
interrupted write leaves the previous complete page. The one page write that is
not atomic is the copy2 of a rename onto the destination name it claims. That
can leave a partial file under the new name, but it cannot truncate the page
being renamed. Corruption therefore arrives from outside: a filesystem fault, a
full disk, or another program that edits the file. Against that, a copy from
before this seam's writes serves as well as a copy from a second ago, and it is
the only one that predates the fault. The boot backup zips every page file into
a timestamped archive and keeps the last few, which covers older history.

A mark never writes. Every deferred write runs on a timer thread, which takes
the fsync pair off the GTK main thread. What a user reads as "done" still
writes synchronously on the thread that got there: a page switch, a deck close,
a quit, a page move. So does any read of the file, through the barrier below.
The deferral is therefore invisible except in how often the disk is touched.
The exposure it buys is bounded. A crash or a power loss loses at most the
last DEBOUNCE_S of edits, or MAX_DIRTY_AGE_S under a continuous burst. The file
itself is never at risk, because the write is the same atomic temporary and
replace, so the primary is the previous complete page or the new one.

The read barrier

Anything that reads a page file from disk must call flush_path (or flush_all)
first, or it reads the file instead of the page. The barriers are at the page
manager's get_page_data, the asset sweep's per-file read, the page export, the
duplicate's read-back, the page move's copy, the boot backup zip and the video
cache sweep. The importers call discard_path instead, because they overwrite
the page wholesale. That list is the reader map, and it is a precondition for
any deferral, because a reader without a barrier sees a page as it was up to a
second ago.

A barrier can make a GTK-thread reader wait for a write in flight on a timer
thread. The wait is bounded by one page write, and that is the trade. It
weighs one possible wait on a read against two fsyncs on every keystroke. discard_path
waits on the same lock for the same bound, and must. A discard that walked past
a flush inside its critical section is undone by that flush at the end.

Thread contract

Every call works from any thread. Saves start on the GTK main thread (every
editor widget), on plugin and action threads (through ActionCore.set_settings),
and on ad-hoc threads (the permission manager's reload). One lock guards
_pending and the backup record. A caller takes it inside the per-path save lock
or on its own, never the other way round, so the two cannot deadlock. Neither
the write nor the backup copy runs under _pending_guard, so a mark never waits
on a flush.

The save lock is a leaf and must stay one. While a caller holds it, it takes
only stdlib file I/O, _pending_guard, and the timer source's own lock when the
write cancels the timer that would have done it. That last one stays a leaf in
practice. The wheel's cancel never waits on a callback, because each fire runs
on its own thread and the wheel's lock is never held across one, and no wheel
callback but this module's own takes a page's save lock. Locks that sit above
this one: the controller's page-load lock (a page switch flushes the outgoing
page inside it) and a document's load guard (its fill reads the file through
the barrier). The page cache lock is never held across it.

Failure

A flush that raises (an unserializable value in the dict is the shipped
example) propagates to its caller with the file untouched, because atomic_json
writes a temporary and replaces. A failed serialization therefore cannot
truncate the primary. The pending record is retired either way, so a failure
leaves nothing that re-raises at a later reader. An edit that cannot be
serialized is not retried, which is what an inline save does. A timer-driven
flush has no caller to raise at, so it logs. flush_all logs per path too,
because its callers are shutdown paths that must not abort on one bad page.

This module imports atomic_json, timer_wheel and InputIdentifier, which are
stdlib and loguru modules with no widget or gi reach. Page is in the headless
engine's import closure, so everything it imports is too. The timer is the
process wheel and not a GLib timeout, for three independent reasons: GLib would
put gi in that closure, saves start on threads with no main loop to run a GLib
source, and a GLib timeout would put the fsync back on the GTK main thread.
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
    """The holder of a page's unwritten content, as the seam sees it.

    A Page and a PageDocument both satisfy this. An edit through a deck's Page
    marks the Page. An edit through the document (the page settings, the
    whole-file editor, the asset sweep) marks the document, because a page no
    deck shows has no Page to mark. Both hold the same dict, so the record only
    decides whose methods produce the bytes.
    """

    json_path: str

    def get_without_action_objects(self) -> dict[str, Any]:
        """Give the content as it goes into the file, without live objects."""
        ...

    def move_key_to_end(self, dictionary: dict[str, Any], key: str) -> None:
        """Re-file one top-level key last in the caller's snapshot."""
        ...

    def make_backup(self, json_path: str) -> None:
        """Copy json_path into pages/backups/ before the write overwrites it."""
        ...


# How long after the last edit the write goes out, and how long a path can stay
# unwritten while edits keep arriving. One second stays below the delay a user
# notices before the next action (a page switch or a window close, which both
# flush synchronously), and it swallows a whole typed label. Five seconds bounds
# what continuous typing leaves unwritten, which the trailing timer alone defers
# without end.
DEBOUNCE_S = 1.0
MAX_DIRTY_AGE_S = 5.0


def canonical_path(path: str) -> str:
    """Give the one key for a page file, whatever spelling names it.

    Every per-page registry in this process keys by this: the save lock, the
    pending record, the backup record, and the document that holds the content.
    Two spellings that key differently give one file two owners. A page reached
    through a symlinked data directory and the same page reached through the
    resolved one take separate save locks and interleave their backup and
    write. A mark left under one spelling is invisible to a read barrier that
    asks under the other, and the re-read after that barrier erases the edit
    from memory too.

    Resolution costs one lstat per path component, once per mark and once per
    barrier. That is about 8 us for a page under a normal data directory,
    against about 150 us for the write it guards, at the rate a person types.

    It lives here and not with the document, because this module sits in the
    headless engine's import closure and reaches no further than the stdlib.
    The document needs the page manager, so it imports this from here.
    """
    return os.path.realpath(path)


# One save lock per page file, shared by every Page object for that file. Each
# controller on one page holds its own Page instance, so a per-object lock
# cannot order two controllers' saves of one file. The registry grows one entry
# per distinct page file and never prunes. The user's page count (tens) bounds
# it, so it needs no eviction.
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def save_lock(path: str) -> threading.Lock:
    """Give the one lock for the page file at path.

    Public because the write is not its only holder. A page's content and its
    bytes are two states of one file, so the document's mutation seam holds
    this lock while it changes the content, as the flush holds it while it
    turns the content into bytes. Two locks over one file only pose the
    question of which comes first.

    It canonicalizes instead of trusting its caller, although every caller in
    this module holds the key. This registry carries the whole one-file
    one-writer rule, and a caller that keys it by a raw spelling gives one file
    two locks unnoticed. A resolved path resolves to itself.
    """
    with _save_locks_guard:
        return _save_locks.setdefault(canonical_path(path), threading.Lock())


class Scheduler(Protocol):
    """The two operations the deferral needs from a timer source."""

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> Any:
        """Arm a one-shot timer and return a handle for cancel()."""
        ...

    def cancel(self, handle: Any) -> None:
        """Disarm a timer that has not fired yet. Idempotent after a fire."""
        ...


class TimerWheelScheduler:
    """A Scheduler backed by the process timer wheel.

    Each fire gets its own short-lived daemon thread, which is the wheel's
    contract, so a slow page write delays no other timer. A daemon thread dies
    at os._exit, so quit flushes explicitly instead of trusting an armed timer.
    """

    def schedule(self, delay_s: float, callback: Callable[[], None]) -> Any:
        return timer_wheel.schedule(delay_s, callback, name="page_flush")

    def cancel(self, handle: Any) -> None:
        handle.cancel()


class _Pending:
    """One file's outstanding edits.

    It holds who has them, the name they were marked under, the time of the
    first mark, and the timer that writes them.

    Identity matters. A mark that lands mid-write replaces this object instead
    of mutating it, so the flush separates the edits it wrote from the edits
    that arrived after its read by comparing the entry it took.

    path is the spelling of the mark. The write and the backup need a name and
    not the key. The backup is filed under the page's basename, and the key is
    a resolved path that can carry another one. A page move re-points the
    source's json_path in place, so the timer must not read that attribute.
    """

    __slots__ = ("source", "path", "first_marked", "handle")

    def __init__(self, source: PageContent, path: str, first_marked: float) -> None:
        self.source = source
        self.path = path
        self.first_marked = first_marked
        self.handle: Any = None


class PageFlush:
    """Which pages are ahead of their files, and when the file catches up.

    The scheduler and the clock are constructor arguments, so a caller can
    drive the deferral in virtual time with no sleep. A headless test supplies
    a scheduler that fires on command and a clock it advances, which turns
    every timing rule above into an assertion.
    """

    def __init__(self, scheduler: "Scheduler | None" = None,
                 clock: "Callable[[], float] | None" = None) -> None:
        # path -> the edits that path waits for. A strong reference to the
        # source, because the page cache evicts without a write and an evicted
        # page with pending edits must stay writable. The next mint of that path
        # reads the file through the barrier, so it gets what this kept alive.
        self._pending: dict[str, _Pending] = {}
        # Page files this seam copied into pages/backups/ already. The copy
        # keeps the state the file had before this seam overwrote it, which
        # belongs to the session and not to one write, so it is taken once per
        # path. Only discard_path prunes an entry, because it gives the file to
        # another writer. The user's page count bounds it, like the locks.
        self._backed_up: set[str] = set()
        self._pending_guard = threading.Lock()
        self._scheduler: Scheduler = scheduler if scheduler is not None else TimerWheelScheduler()
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

    def mark_dirty(self, source: PageContent) -> None:
        """Record that source holds edits its file lacks, and arm the write.

        The contract is cheap and non-blocking. This runs on the GTK main
        thread once per keystroke, so it takes one uncontended lock, touches a
        dict and re-arms a timer. It never writes.

        One entry per file, and the last marker wins. Every Page on a page and
        the document behind them share the one dict the page manager holds for
        that file, so the last marker speaks for the file either way.
        """
        with self._pending_guard:
            # Read under the guard, together with the map write. The page move
            # re-points json_path first and discards the old key second, so a
            # mark that reads the old path cannot land after that discard.
            path = source.json_path
            key = canonical_path(path)
            now = self._clock()
            previous = self._pending.get(key)
            entry = _Pending(source, path,
                             previous.first_marked if previous is not None else now)
            self._pending[key] = entry
            if previous is not None and previous.handle is not None:
                self._scheduler.cancel(previous.handle)
            # Trailing, but never past the age cap. At the deadline the delay
            # clamps to zero and the write goes out on the next timer dispatch,
            # instead of moving one more second per keystroke.
            deadline = entry.first_marked + MAX_DIRTY_AGE_S
            delay = min(DEBOUNCE_S, max(0.0, deadline - now))
            entry.handle = self._scheduler.schedule(delay, lambda: self._fire(key))

    def pending_source(self, path: str) -> "PageContent | None":
        """Give the holder of the edits path waits for, or None.

        This is the seam's own state. It is readable, so a test can assert the
        ordering rules above from outside.
        """
        with self._pending_guard:
            entry = self._pending.get(canonical_path(path))
            return entry.source if entry is not None else None

    def _fire(self, key: str) -> None:
        """Timer dispatch. It logs a failure, because it has no caller."""
        try:
            self.flush_path(key)
        except Exception:
            log.opt(exception=True).error(f"Deferred write of page {key} failed")

    def flush_path(self, path: str) -> None:
        """Put the pending edits of path on disk now. A no-op when it has none.

        The clean case is one dict lookup and a return, which is cheap enough
        to sit in front of every read of a page file.

        The write comes first and the retire second, both under the per-path
        save lock. That order stops a reader from overtaking a write in flight.
        An entry claimed up front leaves the map empty while the bytes go down,
        and the fast path above then sends the reader to the stale file. A
        retire at the end keeps the map marked for as long as the file is
        behind. A concurrent flush therefore waits on the lock and finds the
        work done, or finds a mark that landed mid-write and writes again.

        It writes the file this call locked, which is the key the edits were
        marked under. It never writes the path the source's json_path holds
        when the timer fires. A page move re-points that attribute in place, so
        the two can name different files. A write to the locked path keeps the
        lock and its file together, and the move retires the stale key, so
        nothing writes the moved-from file back into existence.
        """
        key = canonical_path(path)
        # This test runs unguarded. A containment test is atomic, and a lost
        # race against a mark means the reader arrived before an edit it does
        # not wait for.
        # Every decision below re-reads _pending under the guard.
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
                for type in Input.KeyTypes:
                    source.move_key_to_end(without_objects, type)
                # Atomic replace, so an interrupted write leaves no truncated
                # page.
                atomic_write_json(entry.path, without_objects)
            finally:
                with self._pending_guard:
                    # Retire only the entry this call wrote. A save that landed
                    # mid-write replaced it with a newer entry, which is ahead
                    # of these bytes and keeps its own timer. The finally keeps
                    # a failed write from leaving a mark that re-raises at the
                    # next reader. An unserializable edit is not retried.
                    if self._pending.get(key) is entry:
                        del self._pending[key]
                        if entry.handle is not None:
                            # The write is done, so the armed timer has nothing
                            # left to find.
                            self._scheduler.cancel(entry.handle)

    def _back_up_once(self, key: str, path: str, source: PageContent) -> None:
        """Copy path into pages/backups/ unless this session did it already.

        It runs under the path's save lock and before the write it guards, so
        pages/backups/ gets the file as this seam found it, and no other flush
        of that file fits between the check and the copy.

        A refusal counts as done. make_backup returns without a copy when the
        primary does not parse, because that copy destroys what the backup is
        for, and when the primary is absent, because the write that follows
        recreates the file. A second attempt on the next write finds that
        primary parseable, since this seam wrote it, and puts a duplicate of
        the current file over the last copy taken before the damage. So the
        first write of a path in a session settles that path's backup. Only a
        raise from make_backup leaves the question open for the next write. A
        raise is an unexpected I/O error. It reads nothing and overwrites
        nothing, because it takes the write with it.

        It uses path and never the source's json_path. A page move re-points
        that attribute in place, and the file that needs its previous state
        kept is the one this call locked and overwrites next.
        """
        with self._pending_guard:
            if key in self._backed_up:
                return
        # The copy runs outside the guard, because it re-parses and duplicates
        # a whole page file, and every mark_dirty on the GTK main thread takes
        # that guard. Two flushes of one path cannot both be here, because the save
        # lock is held for the length of this call.
        source.make_backup(path)
        with self._pending_guard:
            self._backed_up.add(key)

    def flush_all(self) -> None:
        """Put the pending edits of every path on disk now.

        For the callers that read everything (the boot backup zip, the video
        cache sweep) and for quit, where the timer threads die with the
        process. It never raises, because its callers are shutdown and sweep
        paths that must not abort over one unserializable page.
        """
        with self._pending_guard:
            paths = list(self._pending)
        for path in paths:
            try:
                self.flush_path(path)
            except Exception:
                log.opt(exception=True).error(f"Could not write pending edits of page {path}")

    def discard_path(self, path: str) -> None:
        """Throw the pending edits of path away without a write.

        For a page that goes away. A flush of a deleted page writes the file
        back into existence, and a flush of a move's source writes a file the
        move removes next.

        The backup record goes with them. Every caller here gives the file to
        somebody else: a delete, a move, or an import. Whatever stands at this
        path afterwards is content this seam never overwrote, and no backup in
        pages/backups/ describes it. The old copy holds a page that is gone or
        the version an import replaced, and a heal from it puts that page back
        over the one the user has now. The next flush of this path therefore
        takes its own copy. The rule stays what it says. The backup is the file
        as it was before this seam first wrote it, and "before" restarts each
        time another writer takes the file over wholesale.

        It runs under the path's save lock, like a flush, so a discard cannot
        land inside one. A flush already inside its critical section read its
        entry and sits somewhere between the backup and the write. A discard
        that slipped past it is undone twice. The flush's copy re-adds the
        backup record, and the write puts the pre-discard page over the content
        the caller just replaced. The wait lets the flush finish against the old
        file, and the delete, move or import after this call has the last word.

        One page write bounds the wait, or one edit of the page's content,
        which is a few dict operations. A read barrier already accepts that
        bound and that lock. Every caller arrives holding no other lock. The
        save lock takes nothing else while held. The backup, the snapshot and
        the atomic write are stdlib file I/O, and a document edit is dict
        mutation. It is a leaf, so a caller under a page manager lock or a
        controller lock inverts nothing.
        """
        key = canonical_path(path)
        with save_lock(key):
            with self._pending_guard:
                entry = self._pending.pop(key, None)
                self._backed_up.discard(key)
        if entry is not None and entry.handle is not None:
            self._scheduler.cancel(entry.handle)


# The process-wide flush seam. A module singleton and not a gl slot, because
# this protocol exists to shrink the shared namespace.
_flush = PageFlush()


def get() -> PageFlush:
    """Give the process-wide page flush. Never None."""
    return _flush
