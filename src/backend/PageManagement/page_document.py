"""
One page file, one dict, one lock.

A page json is shown by as many decks as the user points at it, and each of
those decks holds its own ``Page`` object for it. Those Page objects used to
hold their own *copies* of the file's content: N decks on one page meant N
dicts, brought back into agreement after every edit by writing the file and
reading it back into each of them. The round trip was the convergence
mechanism, which made it something every editing path had to remember to
trigger -- and the paths that forgot left one deck showing a page the others
had stopped agreeing with.

A ``PageDocument`` is that content, once. Every Page on a path reads and
writes through the same dict object, so an edit made through one of them is
already made for all of them, with no file involved. The registry that hands
documents out lives on the page manager, keyed through the flush seam's
``canonical_path`` so that a page named two ways is one document with one save
lock and one pending write rather than two of each. Documents are minted on
first use and never dropped, and what is being kept is a whole page dict --
tens of kilobytes each, so a long session across many pages holds on the order
of a megabyte it will not give back. The registry is the only thing that can
promise a file has one content; an entry dropped while a Page still reads
through it hands the next Page a second copy, silently.

THE MUTATION SEAM

``edit()`` is how anything that is not a Page changes a page: the page
settings, the whole-file editor, the sweep that strips a deleted asset out of
every page that referenced it. Each of those used to read the file, change
the dict it got back, and write the whole file again -- a read-modify-write
against a file that a plugin thread could save in between, which lost
whichever of the two edits finished second. Inside ``edit()`` there is
nothing to lose: the mutation happens IN the content every reader already
holds, so it is complete the moment the block ends, and the file catches up
through the same flush seam every other page edit goes through.

THE LOCK, AND WHAT IS ORDERED AGAINST WHAT

There is exactly one lock per page file and it is the flush seam's save lock
-- the same one the write, the backup and the discard take. This document's
serialization IS that lock rather than a second one beside it, because two
locks over one file only pose the question of which comes first. Held here
by ``edit()`` (for the length of a mutation) and by ``adopt()`` (for the
length of a whole-content replacement); held there by the flush (backup +
snapshot + write) and by the discard.

What that buys: a flush can never snapshot a half-applied edit, and can never
catch a refresh between putting the new content in and taking the old
sections out -- the two states a page file must never be written from.

What it costs, and where the ordering rules are: the lock is a LEAF. While it
is held, only stdlib file I/O and the seam's own pending-record guard are
taken. So nothing inside an ``edit()`` block may read a page file (the read
barrier takes this lock), reload a page (the controller's page-load lock is
taken ABOVE this one, at the flush on page switch), or marshal to the GTK
main thread. Every writer here mutates the dict and nothing else; the reloads
that follow a settings change happen after the block closes.

The load ordering is the other way round and stays that way: ``ensure_loaded``
and ``refresh_from_disk`` take this document's load guard and then, through
the page manager's read barrier, the save lock underneath it -- never the
reverse, which is why ``edit()`` loads BEFORE it locks.

REFRESHING, AND THE INVARIANT THAT MAKES IT SAFE

``refresh_from_disk`` is for the writers that still go around the document --
an importer replacing a page wholesale, a migrator running before there is a
page manager, a page created under a name whose document is still holding the
content of a page that was deleted. It reads through the page manager, so the
read barrier and the corrupt-heal apply to it exactly as they apply to any
other read of a page file.

It can only lose an edit the file has not been told about, and nothing in
tree leaves one for long: a mutator mutates the dict and saves in the same
call, ``save`` marks the page with the flush seam, and the barrier inside the
load writes anything marked out before the file is read. That standing rule
is what lets a refresh be a refresh rather than a merge.

One window is left, and it is worth naming rather than implying away: an edit
made and marked AFTER that load returns but before the swap below is lost
from memory and from the file alike -- the mark survives the swap, but it now
points at content the swap has reverted, so the write that follows puts the
pre-edit page on disk. The window is a function return plus a lock
acquisition wide, on a page being re-read at the moment somebody edits it,
and closing it would mean holding the file's lock across the read -- which
the read barrier inside that read takes for itself.

IN PLACE, AND WHAT A RENDER THREAD SEES

A refresh mutates the dict rather than replacing it, because replacing it is
precisely what a shared document rules out: every Page holds this one object,
so a fresh dict would be seen by nobody. What that changes for a concurrent
reader is worth stating exactly, because page content is read from the media
threads under no lock at all -- always as ``.get`` chains off ``page.dict``,
evaluated where they are needed and never held across frames.

The new content goes in first and only then the top-level sections it does not
have come out. ``dict.update`` from another dict runs to completion in C under
the GIL, as does each ``del``, so a reader that arrives mid-refresh sees whole
values and never a half-built one; the one anomaly available to it is a
section that is about to be removed surviving a moment longer. It cannot see a
section missing that both the old and the new content have, which is exactly
what clearing first would have shown it.

Measured against the wholesale dict swap this replaces, that is the same class
of race through a narrower window. A swap was atomic for a single read of
``page.dict``, but not across two: each read re-fetched the attribute, so a
caller that read ``keys`` before the swap and ``settings`` after got precisely
the straddled pair an in-place update can produce. And nested containers are
untouched here -- only top-level keys are rebound, each to a tree the loader
has just finished building -- so a structural snapshot walking ``data["keys"]``
walks an object this never mutates. The one reader that could have KEPT the
anomaly -- a flush snapshotting between the two steps, writing sections the
refresh was about to drop -- is the reason the replacement holds the save
lock: it cannot run while a write does.

WHAT A DOCUMENT OWNS THAT A PAGE USED TO

Turning this content into the bytes of a page file: the json-shaped snapshot,
the stripping of live action objects out of it, the key ordering, and the copy
into ``pages/backups/``. All four are properties of the content and its path,
not of any one deck's Page, and the seam needs them for a page no deck is
showing -- a page-settings edit on a page nobody has open has no Page object
to ask. ``Page`` keeps the same four names and delegates them here.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PageManagement import page_flush


def snapshot_json_tree(value):
    """Structural copy of a json-shaped tree: dicts/lists are re-created,
    leaves are shared by reference. dict.copy() and list() run entirely in C
    under the GIL, so each container snapshots atomically even while another
    thread is mutating it -- unlike json.dump (or copy.deepcopy), this can
    never raise `RuntimeError: dict changed size during iteration`. That is
    what makes it safe over a page document, whose content every deck showing
    that page edits through directly, and which a refresh rebinds top-level
    keys of in place: whatever this walks, it walks its own copy of. Leaves
    are deliberately NOT deep-copied: action entries hold live ActionCore
    objects under "object" (the caller strips those from the copy), which
    must never be duplicated."""
    if isinstance(value, dict):
        return {key: snapshot_json_tree(item) for key, item in value.copy().items()}
    if isinstance(value, list):
        return [snapshot_json_tree(item) for item in list(value)]
    return value


def content_without_action_objects(data: dict[str, Any]) -> dict[str, Any]:
    """A json-serializable copy of page content, live action objects removed.

    Serialized from a snapshot, never the live tree: json.dump over the
    content raced concurrent mutations (a RuntimeError mid-dump lost the
    whole save), and a shallow copy meant the `del action["object"]` below
    mutated the ORIGINAL action dicts.
    """
    dictionary = snapshot_json_tree(data)
    for input_type in Input.KeyTypes:
        for key in dictionary.get(input_type, {}):
            for state in dictionary[input_type][key].get("states", {}):
                if "actions" not in dictionary[input_type][key]["states"][state]:
                    continue
                for action in dictionary[input_type][key]["states"][state]["actions"]:
                    if "object" in action:
                        del action["object"]

    return dictionary


def move_key_to_end(dictionary: dict[str, Any], key: str) -> None:
    """Re-file `key` last in `dictionary`, if it is there at all.

    Operates on the caller's snapshot. This used to pop/reinsert on the live
    content instead -- mutating the page mid-save while never reordering the
    dict actually being written.
    """
    if key in dictionary:
        dictionary[key] = dictionary.pop(key)


def back_up_page_file(src_path: str) -> None:
    """Copy a page file into ``pages/backups/`` -- the copy a corrupt primary
    is healed from.

    Only a primary that reads back as a whole page is worth copying. Both
    refusals below leave ``pages/backups/`` untouched and let the write that
    follows go ahead, because the two ways to lose a page here are to put the
    damage on top of the copy that survives it, and to make the page
    unwritable by refusing to write at all.
    """
    os.makedirs(os.path.join(gl.DATA_PATH, "pages", "backups"), exist_ok=True)
    dst_path = os.path.join(gl.DATA_PATH, "pages", "backups", os.path.basename(src_path))

    try:
        with open(src_path) as f:
            json.load(f)
    except FileNotFoundError:
        # Nothing to copy. A page whose file the loader quarantined is live
        # in memory (get_page_data substituted the backup) with no primary
        # behind it, and the write this guards recreates the file from that
        # page -- the only thing that hands the user back a writable page.
        # Raising instead made every write of it fail on a timer thread,
        # dropping the edits with nothing but a log line.
        log.warning(f"No page file at {src_path} to back up; the write recreates it")
        return
    except ValueError as e:
        # ValueError, not JSONDecodeError: a file of garbage bytes raises
        # UnicodeDecodeError while decoding, before the parser ever sees it --
        # a ValueError but not a JSON error, so the narrower clause let it
        # escape and take the write down with it. The settings loader catches
        # the pair as one for the same reason.
        log.error(f"Invalid json in {src_path}: {e}")
        return

    shutil.copy2(src_path, dst_path)


def _apply(data: dict[str, Any], content: dict[str, Any]) -> None:
    """Make `data` hold `content` without replacing the dict object.

    New values first, removals second, for the reason the module header
    gives: a reader crossing this sees a stale top-level section one moment
    longer, never a section that both versions have go missing. The caller
    holds the page file's lock.
    """
    if content is data:
        return
    dropped = [key for key in data if key not in content]
    data.update(content)
    for key in dropped:
        del data[key]


class PageDocument:
    """The single in-memory copy of one page file.

    ``json_path`` is the file this content belongs to, as the first caller to
    ask for it named it -- the same attribute name a ``Page`` carries, because
    the flush seam takes either as the holder of a page's unwritten edits.
    ``data`` is the content itself: the dict every ``Page`` on this path holds
    and mutates directly, handed out as a read-only property so that nothing
    can swap it for another one and leave the Pages aliasing a dict the
    document no longer has.
    """

    def __init__(self, path: str) -> None:
        self.json_path = path
        self._data: dict[str, Any] = {}
        # Serializes the one-time fill below, and nothing else. Taken ABOVE
        # the page file's save lock (the load reads the file through the
        # page manager's read barrier), never below it.
        self._load_guard = threading.Lock()
        self._loaded = False

    @property
    def data(self) -> dict[str, Any]:
        """This page's content. The same object for every Page on the path."""
        return self._data

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Change this page's content, and have the change reach its file.

        The seam every writer that is not a Page goes through. Inside the
        block the content is this process's one copy of the page, held
        against the flush, the discard and any other edit of the same file;
        on the way out the page is marked with the flush seam exactly as
        ``Page.save()`` marks it, so the write is the same debounced,
        atomic one every page edit gets.

        NOTHING INSIDE THE BLOCK MAY TOUCH A PAGE FILE. The lock held here is
        the file's only lock, and it is a leaf: reading a page (the read
        barrier takes it), reloading a page onto a deck (the controller takes
        its page-load lock and then this one) or marshalling to the GTK main
        thread from in here is a deadlock, not a slow path. Mutate the dict;
        do the rest after the block.

        The content is filled from the file first, before the lock: a
        document minted for a page no deck is showing starts empty, and an
        edit applied to an empty dict would write a page consisting of just
        that edit.

        The mark is made even when the block raises. A mutator that got
        halfway has already changed the content every reader holds, so
        leaving it unwritten would not undo it -- it would only put the file
        out of step with the page.
        """
        self.ensure_loaded()
        with page_flush.save_lock(self.json_path):
            try:
                yield self._data
            finally:
                page_flush.get().mark_dirty(self)

    def replace(self, content: dict[str, Any]) -> None:
        """Make `content` this page's whole content, as one edit.

        For the editor that hands back a whole page json rather than a
        change to one. Anything the caller's content does not carry is gone
        by definition -- that is what replacing a page means -- but it goes
        from the page and the file together, which is the part that used to
        fail: the write landed on disk and a page edit still on its timer
        wrote the pre-replacement content straight back over it.

        THE DOCUMENT TAKES `content` BY REFERENCE. Its top-level values are
        adopted as they are, not copied, so the caller must hand over a tree
        it will not keep and will not touch again: whatever it changed
        afterwards it would be changing in the live page, under no lock,
        from wherever it happened to be. The old path could not have this
        because it round-tripped through the file. Today's caller parses
        fresh json and drops it, which is the shape to keep.
        """
        with self.edit() as data:
            _apply(data, content)

    def ensure_loaded(self) -> None:
        """Fill this document from its file unless it already holds it.

        Minting a document does not read anything -- the page manager hands
        them out for paths a Page is about to load anyway. Every other way in
        needs the content first, and needs it exactly once: two threads
        reaching an unfilled document must not both read the file, because
        the second read would land after the first thread's edit and drop it.
        """
        with self._load_guard:
            if self._loaded:
                return
            self._load()

    def refresh_from_disk(self) -> None:
        """Bring this document back in line with the file.

        Routed through the page manager rather than reading the file here, so
        that the read barrier (pending edits reach disk first) and the
        corrupt-heal (an unparseable primary is substituted from
        ``pages/backups/``) are the same ones every other read of a page file
        gets. Nothing else in this module knows how a page is loaded.
        """
        with self._load_guard:
            self._load()

    def _load(self) -> None:
        page_manager = gl.page_manager
        if page_manager is None:
            # Only before create_global_objects(); nothing to load from.
            return
        self.adopt(page_manager.get_page_data(self.json_path))
        self._loaded = True

    def adopt(self, content: dict[str, Any]) -> None:
        """Make `content` this document's content without replacing the dict.

        Under the page file's lock, so that no write can be taking its
        snapshot while the content is being swapped over -- a flush caught
        between the two steps writes sections this is in the middle of
        dropping, and the next read of that file puts them back.
        """
        with page_flush.save_lock(self.json_path):
            _apply(self._data, content)

    # -- what the flush seam needs from whoever holds a page's content --

    def get_without_action_objects(self) -> dict[str, Any]:
        """This page's content as it goes into its file."""
        return content_without_action_objects(self._data)

    def move_key_to_end(self, dictionary: dict[str, Any], key: str) -> None:
        move_key_to_end(dictionary, key)

    def make_backup(self, json_path: str | None = None) -> None:
        """Copy this page's file into ``pages/backups/``.

        `json_path`, when the flush passes one, is the file it holds the save
        lock for: a page move re-points this document in place while a write
        for the old path may still be pending, and backing up a file other
        than the one about to be overwritten would copy the wrong page over
        the wrong backup.
        """
        back_up_page_file(json_path if json_path is not None else self.json_path)


def document_for(path: str) -> PageDocument:
    """The document for ``path`` -- the page manager's, when there is one.

    A Page built before ``create_global_objects()`` has no registry to join
    and gets a document of its own. That page is nobody's sibling yet, so the
    sharing it misses is sharing with nothing.
    """
    page_manager = gl.page_manager
    if page_manager is None:
        return PageDocument(path)
    return page_manager.get_document(path)
