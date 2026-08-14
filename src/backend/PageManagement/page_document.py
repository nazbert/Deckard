"""
One page file, one dict, one lock.

Many decks can show one page json, and each deck holds its own Page object for
it. A PageDocument holds that content once, so an edit through one Page is an
edit for all of them, with no file involved.

The page manager owns the registry that hands documents out. It keys them
through the flush seam's canonical_path, so a page named two ways is one
document with one save lock and one pending write. A document is minted on
first use and never dropped, and each one holds a whole page dict, tens of
kilobytes. Only the registry can promise one content per file. An entry dropped
while a Page still reads through it gives the next Page a second copy, silently.

edit() is the mutation seam for everything that is not a Page: the page
settings, the whole-file editor, and the sweep that strips a deleted asset out
of every page. The lock rules live at edit() and at adopt(), and the refresh
rules at refresh_from_disk() and _apply().

Page delegates four names here: the json-shaped snapshot, the removal of live
action objects, the key ordering, and the copy into pages/backups/. All four
belong to the content and its path, and the seam needs them for a page no deck
shows.
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
    atomically while another thread mutates it, where json.dump and
    copy.deepcopy raise over a live page document. It shares the leaves,
    because an action entry holds a live ActionCore that must stay unique."""
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

    New values first, removals second. The caller holds the page file's lock.
    """
    # A refresh mutates the dict, because every Page holds this one object and
    # nobody would see a fresh one. The media threads read page content under
    # no lock, as .get chains off page.dict. dict.update and del each run to
    # completion in C under the GIL, so such a reader sees whole values. Its
    # one anomaly is a section that survives a moment past its removal, and it
    # never sees a section missing that both versions hold, which a clear-first
    # order would show it. Only top-level keys are rebound, each to a tree the
    # loader just built, so a snapshot that walks data["keys"] walks an object
    # this never mutates.
    if content is data:
        return
    dropped = [key for key in data if key not in content]
    data.update(content)
    for key in dropped:
        del data[key]


class PageDocument:
    """The single in-memory copy of one page file.

    json_path is the file this content belongs to, and Page carries the same
    attribute name, because the flush seam takes either as the holder of a
    page's unwritten edits. data is the dict every Page on this path mutates.
    It is read-only, so nothing can leave the Pages aliasing a dropped dict.
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

        Nothing inside the block may touch a page file or the GTK main loop.
        Mutate the dict, and do the rest after the block.
        """
        # The lock held here is the file's only lock, and it is a leaf. A read
        # of a page (the read barrier takes the lock), a reload onto a deck
        # (the controller takes its page-load lock and then this one), and a
        # marshal to the GTK main thread each deadlock from in here.
        #
        # The content comes from the file before the lock. A document minted
        # for a page no deck shows starts empty, and an edit into an empty dict
        # writes a page that holds that edit alone.
        #
        # The block marks the page even when it raises, as Page.save() marks
        # it. A mutator that stopped halfway already changed the content every
        # reader holds, so an unwritten mark only puts the file out of step.
        self.ensure_loaded()
        with page_flush.save_lock(self.json_path):
            try:
                yield self._data
            finally:
                page_flush.get().mark_dirty(self)

    def replace(self, content: dict[str, Any]) -> None:
        """Make content this page's whole content, as one edit.

        For the editor that hands back a whole page json. The document takes
        content by reference, so the caller must drop the tree it passes.
        """
        # A replacement drops whatever the caller's content does not carry, and
        # it drops it from the page and the file together. A file-only
        # replacement loses to a page edit still on its timer, which writes the
        # old content back over it. A later change to the passed tree changes
        # the live page from any thread, under no lock, so the caller parses
        # fresh json and drops it.
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

        It goes through the page manager, so the read barrier and the
        corrupt-heal are the ones every read of a page file gets.
        """
        # It serves the writers that go around the document: an importer that
        # replaces a page wholesale, a migrator before the page manager exists,
        # and a page created under a name whose document holds a deleted page.
        #
        # It can lose only an edit the file does not know about. A mutator
        # mutates and saves in one call, save() marks the page, and the barrier
        # writes every marked edit out before the read. One window stays open.
        # An edit marked after the load returns, but before the swap, is lost
        # from memory and from the file, because the mark then points at
        # reverted content. Closing it needs the file's lock across the read,
        # which the barrier inside that read takes for itself.
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
