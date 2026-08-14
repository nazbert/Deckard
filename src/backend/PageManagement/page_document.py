"""
One page file, one dict, one lock.

Many decks can show one page json, and each deck holds its own Page object for
it. A PageDocument holds that content once. Every Page on a path reads and
writes the same dict object, so an edit through one Page is an edit for all of
them, with no file involved.

The page manager owns the registry that hands documents out. It keys them
through the flush seam's canonical_path, so a page named two ways is one
document with one save lock and one pending write. A document is minted on
first use and never dropped. Each one holds a whole page dict, tens of
kilobytes, so a long session across many pages holds about a megabyte that it
does not give back. Only the registry can promise one content per file. An
entry dropped while a Page still reads through it gives the next Page a second
copy, silently.

The mutation seam

edit() is how anything other than a Page changes a page: the page settings, the
whole-file editor, and the sweep that strips a deleted asset out of every page.
A read-modify-write of the file instead loses whichever of two concurrent edits
ends second. Inside edit() the mutation happens in the content that every
reader already holds. The edit is complete when the block ends, and the file
catches up through the flush seam.

The lock, and what it orders

There is one lock per page file, and it is the flush seam's save lock. The
write, the backup and the discard take that same lock. edit() holds it for a
mutation, and adopt() holds it for a whole-content replacement. A flush
therefore cannot snapshot a half-applied edit, and cannot catch a refresh
between the insert of the new content and the removal of the old sections. A
page file must never be written from either state.

The lock is a leaf. While a caller holds it, only stdlib file I/O and the seam's
pending-record guard run under it. So no edit() block may read a page file (the
read barrier takes this lock), reload a page (the controller's page-load lock
sits above this one), or marshal to the GTK main thread. Every writer here
mutates the dict and nothing else. A reload that follows a settings change runs
after the block closes.

The load takes the two locks the other way round. ensure_loaded and
refresh_from_disk take this document's load guard, then take the save lock
under it through the page manager's read barrier. The reverse order is
forbidden, so edit() loads before it locks.

Refreshing

refresh_from_disk serves the writers that go around the document: an importer
that replaces a page wholesale, a migrator that runs before the page manager
exists, and a page created under a name whose document still holds a deleted
page. It reads through the page manager, so the read barrier and the
corrupt-heal apply as they do to any read of a page file.

A refresh can lose only an edit that the file does not know about. A mutator
mutates the dict and saves in the same call, save() marks the page with the
flush seam, and the barrier inside the load writes every marked edit out before
it reads the file.

One window stays open. An edit made and marked after the load returns, but
before the swap below, is lost from memory and from the file. The mark survives
the swap, but it points at content the swap reverted, so the write that follows
puts the pre-edit page on disk. The window is one function return plus one lock
acquisition wide, on a page that somebody edits while it is re-read. To close
it, the caller must hold the file's lock across the read, which the read
barrier inside that read takes for itself.

In place, and what a render thread sees

A refresh mutates the dict and does not replace it. Every Page holds this one
object, so nobody would see a fresh dict. The media threads read page content
under no lock, always as .get chains off page.dict, evaluated where they are
needed and never held across frames.

The new content goes in first. Only then do the top-level sections that it does
not have come out. dict.update from another dict runs to completion in C under
the GIL, and so does each del. A reader that arrives mid-refresh sees whole
values, never a half-built one. Its one anomaly is a section that survives a
moment past its removal. It cannot see a section missing that both the old and
the new content hold, which is what a clear-first order would show it.

Nested containers stay untouched. Only top-level keys are rebound, each to a
tree the loader just built, so a structural snapshot that walks data["keys"]
walks an object this never mutates. One reader could keep the anomaly. A flush
that snapshots between the two steps writes sections the refresh is about to
drop. The replacement holds the save lock for that reason, so it cannot run
while a write does.

What a document owns

Turning this content into the bytes of a page file: the json-shaped snapshot,
the removal of the live action objects from it, the key ordering, and the copy
into pages/backups/. All four are properties of the content and its path, not
of one deck's Page. The seam needs them for a page no deck shows. A
page-settings edit on a page nobody has open has no Page object to ask. Page
keeps the same four names and delegates them here.
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
    """Copy a json-shaped tree structurally, and share the leaves.

    dict.copy() and list() run in C under the GIL, so each container snapshots
    atomically while another thread mutates it. This walks its own copy, so it
    never raises RuntimeError: dict changed size during iteration, as json.dump
    and copy.deepcopy do over a live page document. It shares the leaves,
    because an action entry holds a live ActionCore under "object" that must
    stay unique. The caller strips those from the copy."""
    if isinstance(value, dict):
        return {key: snapshot_json_tree(item) for key, item in value.copy().items()}
    if isinstance(value, list):
        return [snapshot_json_tree(item) for item in list(value)]
    return value


def content_without_action_objects(data: dict[str, Any]) -> dict[str, Any]:
    """Give a json-serializable copy of page content, without action objects.

    It works from a snapshot. json.dump over the live tree raises mid-dump
    against a concurrent mutation, and a shallow copy would let the del below
    mutate the original action dicts.
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
    """Re-file key last in dictionary, if it is there at all.

    It operates on the caller's snapshot. A pop and reinsert on the live
    content mutates the page mid-save and reorders the wrong dict.
    """
    if key in dictionary:
        dictionary[key] = dictionary.pop(key)


def back_up_page_file(src_path: str) -> None:
    """Copy a page file into pages/backups/, which heals a corrupt primary.

    Only a primary that reads back as a whole page is worth a copy. Both
    refusals below keep pages/backups/ untouched and let the write go ahead. A
    page dies two ways here: damage on top of the surviving copy, and a refusal
    to write at all.
    """
    os.makedirs(os.path.join(gl.DATA_PATH, "pages", "backups"), exist_ok=True)
    dst_path = os.path.join(gl.DATA_PATH, "pages", "backups", os.path.basename(src_path))

    try:
        with open(src_path) as f:
            json.load(f)
    except FileNotFoundError:
        # Nothing to copy. A page whose file the loader quarantined is live in
        # memory with no primary behind it, because get_page_data substituted
        # the backup. The write this guards recreates the file from that page, and
        # it is the one thing that gives the user back a writable page.
        log.warning(f"No page file at {src_path} to back up; the write recreates it")
        return
    except ValueError as e:
        # ValueError covers JSONDecodeError and UnicodeDecodeError. A file of
        # garbage bytes fails while it decodes, before the parser sees it, so a
        # JSONDecodeError clause lets it escape and kills the write.
        log.error(f"Invalid json in {src_path}: {e}")
        return

    shutil.copy2(src_path, dst_path)


def _apply(data: dict[str, Any], content: dict[str, Any]) -> None:
    """Make data hold content without replacing the dict object.

    New values first, removals second, for the reason the module header gives.
    A reader that crosses this sees a stale top-level section one moment
    longer, never a missing section that both versions hold. The caller holds
    the page file's lock.
    """
    if content is data:
        return
    dropped = [key for key in data if key not in content]
    data.update(content)
    for key in dropped:
        del data[key]


class PageDocument:
    """The single in-memory copy of one page file.

    json_path is the file this content belongs to, spelled as the first caller
    named it. Page carries the same attribute name, because the flush seam
    takes either object as the holder of a page's unwritten edits. data is the
    content, the dict every Page on this path mutates directly. It is a
    read-only property, so nothing can swap it and leave the Pages aliasing a
    dict this document dropped.
    """

    def __init__(self, path: str) -> None:
        self.json_path = path
        self._data: dict[str, Any] = {}
        # Serializes the one-time fill below, and nothing else. Taken above the
        # page file's save lock, never below it, because the load reads the
        # file through the page manager's read barrier.
        self._load_guard = threading.Lock()
        self._loaded = False

    @property
    def data(self) -> dict[str, Any]:
        """This page's content. The same object for every Page on the path."""
        return self._data

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Change this page's content, and send the change to its file.

        Every writer that is not a Page goes through this seam. Inside the
        block the content is the process's one copy of the page, held against
        the flush, the discard and every other edit of the same file. On the
        way out the block marks the page with the flush seam, as Page.save()
        marks it, so the write is the same debounced atomic one.

        Nothing inside the block may touch a page file. The lock held here is
        the file's only lock, and it is a leaf. A read of a page (the read
        barrier takes the lock), a reload onto a deck (the controller takes its
        page-load lock and then this one), and a marshal to the GTK main thread
        each deadlock from in here. Mutate the dict, and do the rest after the
        block.

        The content comes from the file first, before the lock. A document
        minted for a page no deck shows starts empty, and an edit into an empty
        dict writes a page that holds that edit alone.

        The block marks the page even when it raises. A mutator that stopped
        halfway already changed the content every reader holds, so an unwritten
        mark only puts the file out of step with the page.
        """
        self.ensure_loaded()
        with page_flush.save_lock(self.json_path):
            try:
                yield self._data
            finally:
                page_flush.get().mark_dirty(self)

    def replace(self, content: dict[str, Any]) -> None:
        """Make content this page's whole content, as one edit.

        For the editor that hands back a whole page json. A replacement drops
        whatever the caller's content does not carry, and it drops it from the
        page and the file together. A file-only replacement loses to a page
        edit still on its timer, which writes the old content back over it.

        The document takes content by reference. It adopts the top-level values
        as they are, so the caller must hand over a tree that it drops and never
        touches again. A later change to that tree changes the live page from
        any thread, under no lock. The caller today parses fresh json and drops
        it, which is the shape to keep.
        """
        with self.edit() as data:
            _apply(data, content)

    def ensure_loaded(self) -> None:
        """Fill this document from its file unless it already holds it.

        A mint reads nothing, because the page manager hands documents out for
        paths a Page is about to load. Every other entry needs the content first, and
        needs it once. If two threads read the file into one unfilled document,
        the second read lands after the first thread's edit and drops it.
        """
        with self._load_guard:
            if self._loaded:
                return
            self._load()

    def refresh_from_disk(self) -> None:
        """Bring this document back in line with the file.

        It goes through the page manager, so the read barrier (pending edits
        reach disk first) and the corrupt-heal (an unparseable primary comes
        from pages/backups/) are the ones every read of a page file gets.
        Nothing else in this module knows how a page loads.
        """
        with self._load_guard:
            self._load()

    def _load(self) -> None:
        page_manager = gl.page_manager
        if page_manager is None:
            # Only before create_global_objects(), with nothing to load from.
            return
        self.adopt(page_manager.get_page_data(self.json_path))
        self._loaded = True

    def adopt(self, content: dict[str, Any]) -> None:
        """Make content this document's content without replacing the dict.

        It holds the page file's lock, so no write takes its snapshot during
        the swap. A flush caught between the two steps writes the sections this
        drops, and the next read of that file puts them back.
        """
        with page_flush.save_lock(self.json_path):
            _apply(self._data, content)

    # What the flush seam needs from the holder of a page's content.

    def get_without_action_objects(self) -> dict[str, Any]:
        """This page's content as it goes into its file."""
        return content_without_action_objects(self._data)

    def move_key_to_end(self, dictionary: dict[str, Any], key: str) -> None:
        move_key_to_end(dictionary, key)

    def make_backup(self, json_path: str | None = None) -> None:
        """Copy this page's file into pages/backups/.

        The flush passes the path it holds the save lock for. A page move
        re-points this document in place while a write for the old path is
        still pending, and a backup of any file but the one about to be
        overwritten copies the wrong page over the wrong backup.
        """
        back_up_page_file(json_path if json_path is not None else self.json_path)


def document_for(path: str) -> PageDocument:
    """Return the page manager's document for path, when a manager exists.

    A Page built before create_global_objects() has no registry to join and
    gets its own document. That page has no sibling yet, so it shares with
    nothing.
    """
    page_manager = gl.page_manager
    if page_manager is None:
        return PageDocument(path)
    return page_manager.get_document(path)
