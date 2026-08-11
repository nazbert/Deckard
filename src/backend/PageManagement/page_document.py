"""
One page file, one dict.

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

WHAT A DOCUMENT DOES NOT OWN

The write. Turning this content into bytes, and deciding when that happens,
belongs to the flush seam, which serializes writes of one file on its own
per-path lock. This lock covers this document's own content, and there is
exactly one operation that needs it: the refresh, which replaces all of it at
once. A refresh takes the flush seam's lock underneath its own (through the
read barrier inside the load) and never the other way round.

REFRESHING, AND THE INVARIANT THAT MAKES IT SAFE

``refresh_from_disk`` is for the writers that go around the document -- an
importer replacing a page wholesale, a settings write that edits the file
rather than the page, an asset sweep rewriting every page that referenced a
deleted image. It reads through the page manager, so the read barrier and the
corrupt-heal apply to it exactly as they apply to any other read of a page
file.

It can only lose an edit that was never saved, and nothing in tree makes one:
a mutator mutates the dict and saves in the same call, ``save`` marks the page
with the flush seam, and the barrier inside the load writes anything marked
out before the file is read. That standing rule is what lets a refresh be a
refresh rather than a merge.

IN PLACE, AND WHAT A RENDER THREAD SEES

The refresh mutates the dict rather than replacing it, because replacing it is
precisely what a shared document rules out: every Page holds this one object,
so a fresh dict would be seen by nobody. What that changes for a concurrent
reader is worth stating exactly, because page content is read from the media
threads under no lock at all -- always as ``.get`` chains off ``page.dict``,
evaluated where they are needed and never held across frames.

The refresh puts the new content in first and only then removes the top-level
sections the new content does not have. ``dict.update`` from another dict runs
to completion in C under the GIL, as does each ``del``, so a reader that
arrives mid-refresh sees whole values and never a half-built one; the one
anomaly available to it is a section that is about to be removed surviving a
moment longer. It cannot see a section missing that both the old and the new
content have, which is exactly what clearing first would have shown it.

Measured against the wholesale dict swap this replaces, that is the same class
of race through a narrower window. A swap was atomic for a single read of
``page.dict``, but not across two: each read re-fetched the attribute, so a
caller that read ``keys`` before the swap and ``settings`` after got precisely
the straddled pair an in-place update can produce. And nested containers are
untouched here -- only top-level keys are rebound, each to a tree the loader
has just finished building -- so a structural snapshot walking ``data["keys"]``
walks an object this never mutates.

One reader can do more than see that anomaly: it can keep it. A flush that
takes its snapshot between the two steps writes a page still carrying the
sections the refresh is about to drop, so the file and the memory disagree,
and the next read of that file puts the dropped sections back. The window is
the microseconds between one C-level update and a handful of deletes, on the
one path where a refresh and a write of the same page overlap, and the outcome
is a page with something stale in it rather than a page missing something --
the same direction every other choice here leans. Closing it means holding
this lock across the write as well, which is the writer's lock to give.
"""
from __future__ import annotations

import threading
from typing import Any

import globals as gl


class PageDocument:
    """The single in-memory copy of one page file.

    ``path`` is the file this content belongs to, as the first caller to ask
    for it named it. ``data`` is the content itself: the dict every ``Page``
    on this path holds and mutates directly, handed out as a read-only
    property so that nothing can swap it for another one and leave the Pages
    aliasing a dict the document no longer has.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        # Held only across the one operation that replaces the whole content;
        # every other mutation of a page is a mutation of some sub-tree, done
        # by whoever owns that part of the editing path.
        self._lock = threading.Lock()

    @property
    def data(self) -> dict[str, Any]:
        """This page's content. The same object for every Page on the path."""
        return self._data

    def refresh_from_disk(self) -> None:
        """Bring this document back in line with the file.

        Routed through the page manager rather than reading the file here, so
        that the read barrier (pending edits reach disk first) and the
        corrupt-heal (an unparseable primary is substituted from
        ``pages/backups/``) are the same ones every other read of a page file
        gets. Nothing else in this module knows how a page is loaded.
        """
        page_manager = gl.page_manager
        if page_manager is None:
            # Only before create_global_objects(); nothing to load from.
            return
        self.adopt(page_manager.get_page_data(self.path))

    def adopt(self, content: dict[str, Any]) -> None:
        """Make ``content`` this document's content without replacing the dict.

        New values first, removals second, for the reason the module header
        gives: a reader crossing this sees a stale top-level section one
        moment longer, never a section that both versions have go missing.
        """
        with self._lock:
            data = self._data
            if content is data:
                return
            dropped = [key for key in data if key not in content]
            data.update(content)
            for key in dropped:
                del data[key]


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
