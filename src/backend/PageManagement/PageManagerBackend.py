"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import datetime
import os
import shutil
import threading
import zipfile
from contextlib import contextmanager
from typing import Any, Iterator, TypedDict

from loguru import logger as log

from src.Signals import Signals
from src.backend.DeckManagement.deck_controller.controller import DeckController

from src.backend.PageManagement.Page import Page
from src.backend.PageManagement import page_flush
from src.backend.PageManagement.page_flush import canonical_path
from src.backend.PageManagement.page_document import PageDocument
from src.backend.PageManagement.page_pins import PagePins
from src.backend.DeckManagement.HelperMethods import natural_sort_by_filenames
from src.backend.atomic_json import atomic_write_json
from src.backend import settings_store

import globals as gl


class PageEntry(TypedDict):
    """One cached page slot, with the Page object and its LRU stamp."""
    page: "Page"
    page_number: int


class PageManagerBackend:
    def __init__(self, settings_manager):
        self.settings_manager = settings_manager

        # Guards pages, which arbitrary threads read and mutate.
        # DeckController.close() pops one controller's whole entry while
        # clear_old_cached_pages() iterates and evicts from another
        # controller's entry on that deck's media thread. An RLock, so the
        # methods below can call each other without a deadlock.
        self._pages_lock = threading.RLock()
        self.pages: dict["DeckController", dict[str, PageEntry]] = {}
        # Cache-miss constructions under way, keyed (controller, path) and
        # guarded by _pages_lock. A second caller for one page waits on the
        # first construction instead of building a twin Page whose actions
        # register live event and signal handlers.
        self._loads_in_flight: dict[tuple, tuple[threading.Thread, threading.Event]] = {}
        # One document per page file, keyed by the flush seam's
        # canonical_path, like every other per-page registry in this process.
        # A page reached by two spellings is then one document with one save
        # lock and one pending record. Nothing prunes this map. The header of
        # page_document states why an entry must stay while a Page can read
        # through it, and what one page dict per visited path costs. A page
        # deleted and recreated under one name reuses its document, which the
        # load that mints the new Page fills from the new file.
        self._documents: dict[str, PageDocument] = {}
        self._documents_guard = threading.Lock()
        # Holders of a cached page that eviction cannot see for itself.
        # Public, so the deck controller can bracket its tick and key work.
        self.pins = PagePins()
        self.custom_pages = []

        self.page_order = []

        # In the Settings UI, n-cached-pages counts the cached pages besides
        # the active one, so set_pages_to_cache() stores max_pages = value + 1
        # when the user changes the spinner. Read the setting with the same
        # +1 here, so a fresh boot gets the cache budget the Settings window
        # applies for that value. A constant here instead lets one visit to
        # the Settings window change the live budget.
        n_cached_pages = self.settings_manager.app().n_cached_pages
        self.max_pages = int(n_cached_pages) + 1
        self.page_number = 0

        self.MAX_BACKUPS = 5
        self.PAGE_PATH = os.path.join(gl.DATA_PATH, "pages")

    def load_page(self, path: str, deck_controller: "DeckController") -> Page | None:
        """Load the page into the page dict and raise the page number.

        :param path: The path to the page
        :param deck_controller: The deck controller the page belongs to
        :return: The new page object
        """
        if not path or not os.path.isfile(path):
            return None

        page = Page(json_path=path, deck_controller=deck_controller)
        with self._pages_lock:
            self.pages.setdefault(deck_controller, {})
            self.pages[deck_controller][path] = {"page": page, "page_number": self.page_number}
            self.page_number += 1

        return page

    def get_page(self, path: str, deck_controller: "DeckController") -> Page | None:
        in_flight_key = (deck_controller, path)

        while True:
            with self._pages_lock:
                entry = self.pages.get(deck_controller, {}).get(path)
                if entry is not None:
                    entry["page_number"] = self.page_number
                    page_object: Page | None = entry["page"]
                    self.page_number += 1
                    self.pins.reserve_fetch(page_object, deck_controller)
                    return page_object

                in_flight = self._loads_in_flight.get(in_flight_key)
                if in_flight is None:
                    done = threading.Event()
                    self._loads_in_flight[in_flight_key] = (threading.current_thread(), done)
                    break  # this caller is the builder

            builder_thread, done = in_flight
            if builder_thread is threading.current_thread():
                # A re-entrant miss from inside this thread's construction,
                # where a plugin loads the same page during action init. A wait
                # here self-deadlocks, so construct directly. This builds a twin
                # Page, which only this rare case can reach.
                page_object = self.load_page(path, deck_controller)
                self.pins.reserve_fetch(page_object, deck_controller)
                self.clear_old_cached_pages()
                return page_object

            done.wait()
            # The builder ended, so re-check the cache. After a failed load
            # the next pass finds no cache entry and no load under way, and
            # this caller becomes the builder.

        # Cache miss. load_page() takes the lock for the insert. The Page()
        # construction does file I/O outside that hold, so a slow load stalls
        # no lookup of another controller.
        try:
            page_object = self.load_page(path, deck_controller)
        finally:
            # Release the waiters even when the construction raises. They
            # re-check the cache and take over while it stays empty.
            with self._pages_lock:
                self._loads_in_flight.pop(in_flight_key, None)
            done.set()

        # Reserve before this fetch's own eviction pass. That pass sorts a
        # fresh page last, so it reaches one only when the excess covers all.
        self.pins.reserve_fetch(page_object, deck_controller)
        self.clear_old_cached_pages()
        return page_object

    def discard_controller(self, deck_controller: "DeckController") -> None:
        """Drop every cached page entry of a torn-down controller.

        The active_page of a dead controller is unevictable, and it distorts
        the budget of clear_old_cached_pages() for every live controller.

        The pending edits go to disk first. The deck is going away, unplugged
        or with the app, so every page it showed reached a boundary and its
        entry goes too. Eviction never writes, because it reclaims memory on a
        running deck, and the flush seam keeps an evicted page with outstanding
        edits alive.

        It tolerates a failure per page, like every other teardown step. An
        unserializable page must not abort the deregistration and leave the
        controller in the cache forever."""
        flush = page_flush.get()
        with self._pages_lock:
            paths = list(self.pages.get(deck_controller, {}))
        for path in paths:
            try:
                flush.flush_path(path)
            except Exception:
                log.opt(exception=True).warning(
                    f"Could not write pending edits of page {path} while closing a deck")

        with self._pages_lock:
            self.pages.pop(deck_controller, None)
            # The deck is gone, so its outstanding fetch has no later holder.
            self.pins.release_fetch(deck_controller)

    def pages_for_controller(self, deck_controller: "DeckController") -> list["Page"]:
        """Give a snapshot of every cached Page of one controller.

        DeckController.close() runs clear_action_objects() over the snapshot,
        which can call plugin hooks, so it holds no _pages_lock during it."""
        with self._pages_lock:
            cached = self.pages.get(deck_controller, {})
            return [entry["page"] for entry in cached.values() if entry.get("page") is not None]

    def all_cached_pages(self) -> list["Page"]:
        """Give a snapshot of every cached Page of every controller.

        The snapshot is taken under _pages_lock. A caller iterates it without
        the lock, because page teardown can call plugin hooks."""
        with self._pages_lock:
            return [
                entry["page"]
                for controller_pages in self.pages.values()
                for entry in controller_pages.values()
                if entry.get("page") is not None
            ]

    def get_pages(self, add_custom_pages: bool = True, sort: bool = True) -> list[str]:
        pages = []

        os.makedirs(self.PAGE_PATH, exist_ok=True)

        for page in os.listdir(self.PAGE_PATH):
            if not page.endswith(".json"):
                continue

            pages.append(os.path.join(self.PAGE_PATH, page))

        if add_custom_pages:
            pages.extend(self.custom_pages)

        if sort:
            pages = natural_sort_by_filenames(pages)

        return pages

    def get_page_names(self, add_custom_pages: bool = True) -> list[str]:
        page_names = []

        for page in self.get_pages(add_custom_pages=add_custom_pages):
            name = os.path.basename(page)
            name = name.split(".")[0]
            page_names.append(name)

        return page_names

    def clear_old_cached_pages(self):
        # Eviction is an in-memory teardown. It writes no disk file and
        # touches no page json. clear_action_objects() tears down the live
        # action objects and drops the cache entry. The harm of a gutted live
        # page is a use after evict. The page stays visible with dead actions,
        # so a keypress does nothing and the imagery freezes, and the next
        # get_page() mints a duplicate Page. A page reload or a replug recovers
        # it, and no data on disk is lost.
        #
        # Snapshot the eviction decision under the lock and act on it outside.
        # clear_action_objects() below can run plugin hooks, and a wedged hook
        # must not stall a close() or get_page() of another controller on this
        # lock. close() pops its whole controller entry under this lock from
        # its own thread, while this can run on any deck's media thread.
        with self._pages_lock:
            total = sum(len(controller_pages) for controller_pages in self.pages.values())
            excess = total - self.max_pages
            if excess <= 0:
                return

            # Oldest first by page_number. The active page of each controller
            # is safe, and so is every pinned page: fetched, mid-tick, or
            # mid-gesture.
            evictable = []
            for controller, controller_pages in self.pages.items():
                if controller.active_page is None:
                    continue
                for path, page_data in controller_pages.items():
                    page_obj = page_data["page"]
                    if page_obj is controller.active_page:
                        continue
                    if self.pins.is_pinned(page_obj):
                        continue
                    evictable.append((page_data["page_number"], controller_pages, path, page_obj))

            evictable.sort(key=lambda entry: entry[0])
            to_evict = evictable[:excess]

        # A concurrent discard_controller() can pop the whole entry of one of
        # these controllers first. controller_pages stays the same dict object,
        # now orphaned, so the pop below is a no-op and not a KeyError.
        for _, controller_pages, path, page_obj in to_evict:
            # Re-validate under the lock, just before the teardown. Since the
            # snapshot the page can have become live: a load_page activated it
            # (WindowGrabber cycling makes this cache pressure), a controller
            # stashed it as its screensaver-pending page, or a fetch or a
            # bracket pinned it. Pop inside the lock and before the teardown,
            # so a concurrent get_page() mints a fresh Page instead of this
            # gutted one.
            with self._pages_lock:
                page_data = controller_pages.get(path)
                if page_data is None or page_data.get("page") is not page_obj:
                    continue  # discarded or replaced already
                if self.pins.is_pinned(page_obj):
                    continue
                if self._page_is_live(page_obj):
                    continue
                controller_pages.pop(path, None)
            log.info(f"Evicting cached page {path}")
            # The teardown stays outside the lock. It can run plugin hooks,
            # and a wedged hook must not stall a close() or a get_page() that
            # waits on this lock.
            page_obj.clear_action_objects()

    def _page_is_live(self, page_obj) -> bool:
        """Answer True when a controller depends on this Page object.

        A controller depends on a page it shows, and on the page it stashed as
        its screensaver-pending page. A controller holds that stash for the
        whole screensaver duration, and the snapshot guards cannot see it. An
        eviction there makes ScreenSaver.hide() load a page whose actions are
        all dead.

        This lives beside the pins instead of inside them, because it derives
        from what the controllers hold. It is exact, and it needs no release.
        The pins cover the complement, which is every window where no
        controller field names the page while work on it continues."""
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if controller.active_page is page_obj:
                return True
            if getattr(controller, "_screensaver_pending_page", None) is page_obj:
                return True
        return False

    def get_default_page(self, deck_serial_number: str):
        page_settings = settings_store.get().read(settings_store.PAGES)
        page_path = page_settings.get("default-pages", {}).get(deck_serial_number, None)

        if page_path and os.path.isfile(page_path):
            return page_path

        return None

    # path=None is the documented value that clears this deck's default page.
    # get_all_default_page_serial_numbers skips a falsy entry for that reason.
    def set_default_page(self, deck_serial_number: str, path: str | None):
        # A read-modify-write, serialized against every other edit of
        # pages.json. The store's per-file edit lock is the only lock this
        # takes, and no caller holds it while it acquires _pages_lock.
        with settings_store.get().edit(settings_store.PAGES) as page_settings:
            page_settings.setdefault("default-pages", {})
            page_settings["default-pages"][deck_serial_number] = path

    def get_all_default_page_serial_numbers(self) -> list[str]:
        serial_numbers = []

        page_settings = settings_store.get().read(settings_store.PAGES)
        for serial_number, page_path in page_settings.get("default-pages", {}).items():
            if not page_path:
                continue
            serial_numbers.append(serial_number)

        return serial_numbers

    def get_serial_numbers_from_page(self, path: str) -> list[str]:
        serial_numbers = []

        page_settings = settings_store.get().read(settings_store.PAGES)
        for serial_number, page_path in page_settings.get("default-pages", {}).items():
            if path != page_path:
                continue
            serial_numbers.append(serial_number)

        return serial_numbers

    def set_pages_to_cache(self, amount: int):
        old_max_pages = self.max_pages

        self.max_pages = amount + 1

        if old_max_pages > self.max_pages:
            self.clear_old_cached_pages()

    def move_page(self, old_path: str, new_path: str):
        # Read barrier. The copy below reads the old file, so its pending
        # edits go to disk first, or the renamed page arrives without them.
        page_flush.get().flush_path(old_path)

        # The copy replaces the destination wholesale, so an edit pending for
        # that path lands after the copy and undoes the rename. This mirrors
        # the source discard below. It is a no-op unless the new name held a
        # page already.
        page_flush.get().discard_path(new_path)

        shutil.copy2(old_path, new_path)

        # The content follows the file, before the loop below. The loop asks
        # for the page under its old name, which mints a Page and a document
        # for a deck that did not cache it. This move gives those mints a
        # throwaway document at the old name, and keeps the document with the
        # real content, pending edits included, out of their reach.
        document = self.rename_document(old_path, new_path)

        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if controller.active_page is None:
                continue

            page = self.get_page(old_path, controller)

            if not page:
                continue

            page.json_path = new_path
            page.rebind_document(document)
            # Nothing here activates the page, so this fetch retires at once.
            self.pins.release_fetch(controller)

        # Update the path in the page-manager settings, serialized against
        # every other edit of pages.json. This takes the store's edit lock
        # after the controller loop released _pages_lock, and never while it
        # holds it. That is the one order for the two locks.
        with settings_store.get().edit(settings_store.PAGES) as page_settings:
            default_pages = page_settings.get("default-pages", {})
            for serial_number, path in default_pages.items():
                if path != old_path:
                    continue
                default_pages[serial_number] = new_path
            page_settings["default-pages"] = default_pages

        # Retire every write still pending for the file that disappears. A
        # timer that fires after the removal writes the moved-from page back
        # into existence. This runs after the json_path re-point above. A mark
        # reads json_path and inserts its entry under one lock, so once every
        # Page points at the new path, no mark lands under the old key and
        # this discard is the last one. An edit marked between the flush above
        # and the re-point leaves the pending map and stays in memory. It is
        # still in the page's dict, and the next save carries it to the new
        # path.
        page_flush.get().discard_path(old_path)

        os.remove(old_path)
        self.refresh_window_watch_state()

    def remove_page(self, page_path: str):
        # Iterate over all deck controllers to handle any that are using the page to be removed
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # A page change asked for while the screensaver owns the deck goes
            # into _screensaver_pending_page, which the screensaver guard of
            # load_page sets. The active_page checks below cannot see it. When
            # this delete names that page, drop the request and its cache
            # entry. hide() otherwise loads a page whose file is gone, and the
            # first save recreates the deleted file. The controller then stays
            # on its current page after the dismiss.
            pending = getattr(controller, "_screensaver_pending_page", None)
            if pending is not None and pending.json_path == page_path:
                controller._screensaver_pending_page = None
                with self._pages_lock:
                    controller_pages = self.pages.get(controller, {})
                    entry = controller_pages.pop(page_path, None)
                    if not controller_pages:
                        self.pages.pop(controller, None)
                if entry is not None:
                    # Outside the lock, for the reason the teardown below gives.
                    entry["page"].clear_action_objects()
                # The reservation that the stash carried to hide() has nowhere
                # to arrive. The request is gone and its entry is popped, so
                # nothing installs the page and nothing else retires it.
                self.pins.release_fetch(controller)

            active_page = controller.active_page

            # Skip controllers without an active page or not using the page to be deleted
            if not active_page or active_page.json_path != page_path:
                continue

            # Determine the default page for this controller's deck
            serial = controller.deck.get_serial_number()
            deck_default = self.get_default_page(serial)

            if deck_default and deck_default != page_path:
                # Load and switch to the default page if it's not the one being deleted
                new_page = self.get_page(deck_default, controller)
            else:
                # Load the first available page when the delete names the default.
                page_list = self.get_pages()
                if page_path in page_list:
                    page_list.remove(page_path)
                new_page = self.get_page(page_list[0], controller) if page_list else None

            if new_page:
                controller.load_page(new_page)
            else:
                # There is no replacement to install, so no install retires
                # this deck's outstanding fetch. That fetch can name the page
                # this call deletes, and it holds the page against eviction
                # until the deck asks for another one.
                self.pins.release_fetch(controller)

            # Remove the page from the created pages cache for this controller
            with self._pages_lock:
                controller_pages = self.pages.get(controller, {})
                entry = controller_pages.pop(page_path, None)
            if entry is not None:
                # No _page_is_live guard, unlike eviction. This cannot gut a
                # page another controller shows, because the cache holds one
                # Page per (controller, path) and this pops only this
                # controller's. For this controller the page is inactive, since
                # load_page above moved it to another path and both the default
                # and the fallback exclude page_path, or there was no page left
                # to move to. The file goes either way. A controller that shows
                # its screensaver stops at the path mismatch in the top guard,
                # so this is never its pending page.
                #
                # Outside the lock, because clear_action_objects() can run
                # plugin hooks, which must not stall a close() or a get_page()
                # that waits on _pages_lock from another thread.
                entry["page"].clear_action_objects()
                # Drop the controller entry when it holds no cached page.
                if not controller_pages:
                    with self._pages_lock:
                        self.pages.pop(controller, None)

        # Throw away every write still pending for this page instead of a
        # flush. A timer that fires after the removal below writes the deleted
        # page back onto disk. This runs as late as it can, after the cache
        # teardown above dropped every in-tree holder of the Page, so nothing
        # that can re-mark it still runs.
        page_flush.get().discard_path(page_path)

        # Delete the JSON file representing the page
        if os.path.exists(page_path):
            os.remove(page_path)

        # Remove every reference to this page in the default-pages setting.
        # Serialized against every other edit of pages.json. This takes the
        # store's edit lock after the controller loop released _pages_lock, and
        # never while it holds it. That is the one order for the two locks.
        with settings_store.get().edit(settings_store.PAGES) as settings:
            default_pages = settings.get("default-pages", {})
            settings["default-pages"] = {
                serial: path for serial, path in default_pages.items() if path != page_path
            }

        # A delete of the page that carried the only rule takes the rule with
        # it, so the watcher needs a new gate here too.
        self.refresh_window_watch_state()

    def add_page(self, page_name: str, page_dict: dict = None) -> str:
        page_dict = page_dict or {}

        # The app creates the pages dir at startup. A caller before that init,
        # such as a test, must not crash here.
        os.makedirs(self.PAGE_PATH, exist_ok=True)

        path = os.path.join(self.PAGE_PATH, f"{page_name}.json")
        if os.path.exists(path):
            raise FileExistsError(f"A page with the name '{page_name}' already exists.")

        # An imported or a duplicated page arrives with its settings in
        # page_dict, rule included, so this path can add a rule.
        #
        # The file is new and the pending key is not. A delete discards that
        # path's outstanding write, but the Page and the document that held it
        # can mark it again: a screensaver-pending reference, a widget, or a
        # plugin thread that finishes a save. The read barrier then turns that
        # straggler into a resurrection. The refresh below reads the page, the
        # barrier writes the outstanding edits first, and the deleted page
        # lands on top of the new file, which the document adopts. So discard
        # first, as an import does before it replaces a page wholesale, and
        # refresh after. The document survives a delete too, because the
        # registry keeps every entry while a Page can read through it, so
        # without the re-read the first edit of the new page writes the old
        # page back out under its name.
        page_flush.get().discard_path(path)
        atomic_write_json(path, page_dict)
        self.refresh_document(path)

        self.refresh_window_watch_state()
        return path

    def register_page(self, path: str):
        if not os.path.isfile(path):
            log.error(f"Page {path} does not exist")
            return

        log.trace(f"Registering page {path}")
        self.custom_pages.append(path)

        gl.signal_manager.trigger_signal(Signals.PageAdd, path)

        # A registered custom page matches like any other, so its rule counts
        # towards the watcher gate from here on.
        self.refresh_window_watch_state()

    def unregister_page(self, path: str):
        if not self.custom_pages.__contains__(path):
            return

        self.custom_pages.remove(path)
        gl.signal_manager.trigger_signal(Signals.PageDelete, path)
        self.refresh_window_watch_state()

    def get_pages_with_path(self, path: str):
        pages_set = set()

        # A read of self.pages must hold _pages_lock. discard_controller()
        # pops whole controller entries from another thread, so an unlocked
        # check-then-lookup here races it into a KeyError. Lookups only, and
        # no plugin hook runs under the lock.
        with self._pages_lock:
            for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
                page = controller.active_page
                if page is not None and page.json_path == path:
                    pages_set.add(page)

                entry = self.pages.get(controller, {}).get(path)
                if entry is not None:
                    pages_set.add(entry["page"])

        return list(pages_set)

    def reload_pages_with_path(self, path: str, brightness: bool = True, screensaver: bool = True, background: bool = True, inputs: bool = True):
        pages = self.get_pages_with_path(path)

        for page in pages:
            page.load()

            if page.deck_controller.active_page != page:
                continue

            page.deck_controller.load_page(page, allow_reload=True,
                                           load_brightness=brightness,
                                           load_screensaver=screensaver,
                                           load_background=background,
                                           load_inputs=inputs)

    @staticmethod
    def reload_all_pages() -> None:
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            active_page = controller.active_page
            if active_page is None:
                # The deck is present with no page loaded, at boot or after a
                # failed page load, so there is nothing to reload.
                log.warning(f"Deck {controller.serial_number()} has no active page; skipping reload")
                continue
            controller.load_page(active_page, allow_reload=True)

    def get_document(self, path: str) -> PageDocument:
        """Give the one document that holds the content of path.

        It mints the document on the first ask. Every Page on a path comes
        through here, which is what makes them share one dict.
        """
        key = canonical_path(path)
        with self._documents_guard:
            document = self._documents.get(key)
            if document is None:
                document = PageDocument(path)
                self._documents[key] = document
            return document

    def existing_document(self, path: str) -> PageDocument | None:
        """Give the document that holds path, or None. It mints nothing.

        For a caller that speaks to a page only while this process holds it:
        the asset sweep, which reads and writes the file otherwise, and the
        refresh below. A mint here keeps a whole page dict alive for every
        page file the sweep walks past.
        """
        with self._documents_guard:
            return self._documents.get(canonical_path(path))

    def refresh_document(self, path: str) -> None:
        """Re-read path into the document that holds it, if one exists.

        For a writer that puts bytes in a page file without the page that owns
        it: an import, or a page created under a name whose document still
        holds a deleted page's content. Every Page on that path reads the
        document, so one re-read propagates the change.

        A path this process never held stays untouched. There is no in-memory
        content to correct, and the load that mints its first Page reads the
        file.
        """
        document = self.existing_document(path)
        if document is None:
            return
        document.refresh_from_disk()

    def rename_document(self, old_path: str, new_path: str) -> PageDocument:
        """Re-file the content of old_path under new_path and return it.

        A page move re-points the json_path of every Page in place, so the
        document those Pages read through must follow the same file. The next
        page created under the freed old name otherwise gets the moved page's
        content.

        This carries the document object over and does not reload it, because
        the document can hold edits that no disk has. A save inside the move's
        window leaves the pending map, because a timer that fires after the
        move writes the moved-from file back into existence. That save stays in
        memory for the next save to carry to the new name, and a reload here
        loses it.

        There are three reachable outcomes:

        * A document at the old name, which moves. That is the usual case.
        * A document stands at the destination and another moves onto it. The
          mover takes the key, and the standing document goes to the Pages that
          hold it. Their content and their json_path stay theirs, so they write
          it as before, and nothing refreshes them again. Only a rename onto a
          page name another deck shows reaches this.
        * A document stands at the destination and nothing moves onto it,
          because no Page in this process held the moved-from page. The
          standing document is the destination, and it re-reads, because the
          move replaced the file under it.
        """
        old_key = canonical_path(old_path)
        new_key = canonical_path(new_path)
        with self._documents_guard:
            moved = self._documents.pop(old_key, None)
            if moved is not None:
                moved.json_path = new_path
                self._documents[new_key] = moved
                return moved
            document = self._documents.get(new_key)
            if document is None:
                document = PageDocument(new_path)
                self._documents[new_key] = document
        # Nothing moved onto this document, and the file under it is the moved
        # page now. A document that stood here otherwise serves the overwritten
        # content to every Page that reads through it, and puts it back on disk
        # at the next save. A document just minted here holds no content. This
        # runs outside the guard, because the re-read goes through the read
        # barrier and takes a save lock.
        document.refresh_from_disk()
        return document

    def get_page_data(self, path: str, use_backup: bool = True) -> dict:
        """Read the whole content of one page file from disk.

        pages/backups/ substitutes a missing or an unreadable primary. The
        missing case needs use_backup, and the corrupt case always heals, for
        the reason the heal below gives."""
        if path is None:
            return {}

        # Read barrier. The outstanding edits of this page go to disk before
        # anything reads the file. It is one dict lookup when there are none.
        page_flush.get().flush_path(path)

        backup_path = os.path.join(self.PAGE_PATH, "backups", os.path.basename(path))

        # Substitute the backup for a missing primary, when use_backup is set.
        if not os.path.exists(path) and os.path.exists(backup_path) and use_backup:
            path = backup_path

        data, corrupt = self.settings_manager.load_settings_reporting_corruption(path)

        # Heal a corrupt primary from the backup, whatever use_backup says, and
        # whether or not the loader could move the primary aside. Every
        # page-settings mutator reads through get_page_data(path, False) and
        # writes the result straight back through set_page_settings. An empty
        # dict for a corrupt page there guts the live page and erases its keys,
        # background and dials. The gutted file is valid JSON once written, so
        # no later read heals it. The heal belongs to the load result, which is
        # a corrupt read with a backup available, and not to the quarantine
        # rename.
        if corrupt and path != backup_path and os.path.exists(backup_path):
            healed, backup_corrupt = self.settings_manager.load_settings_reporting_corruption(backup_path)
            if not backup_corrupt:
                data = healed
        return data

    def set_page_data(self, path: str, data: dict, reload_brightness: bool = True, reload_screensaver: bool = True, reload_background: bool = True, reload_inputs: bool = True):
        """Replace a whole page with data, for the whole-page editor.

        It goes through the document and not over the file. A file write leaves
        the page holding its old content, so an edit of that page still on its
        timer writes the pre-replacement content back over this one. A
        replacement of the content makes the two one thing.
        """
        self.get_document(path).replace(data)
        if any([reload_brightness, reload_screensaver, reload_background, reload_inputs]):
            self.reload_pages_with_path(path,
                                        brightness=reload_brightness,
                                        screensaver=reload_screensaver,
                                        background=reload_background,
                                        inputs=reload_inputs)

    @staticmethod
    def _strip_asset(page_dict: dict, abs_target_path: str) -> bool:
        """Drop every reference to one asset out of one page's content.

        Returns whether the page referenced the asset. The sweep below writes
        only the pages that did.
        """
        page_had_asset = False

        # Read every section defensively, because a page json can carry no
        # keys, no states and no media.
        for key, key_data in page_dict.get("keys", {}).items():
            for state, state_data in key_data.get("states", {}).items():
                dict_path = state_data.get("media", {}).get("path")
                if dict_path is None:
                    continue

                # Compare absolute paths, and drop the reference on a match.
                if os.path.abspath(dict_path) == abs_target_path:
                    page_had_asset = True
                    state_data["media"]["path"] = None  # Remove the asset path

        return page_had_asset

    def remove_asset_from_all_pages(self, path: str):
        if not path:
            raise ValueError("Invalid path")

        abs_target_path = os.path.abspath(path)

        for page_path in self.get_pages():
            # A page this process holds is edited where it lives. A read of
            # the file plus a write back goes round the outside of the content
            # a deck edits, and whichever of the two ends second wins the whole
            # file. Only a page held already goes this way. A document per page
            # file the sweep walks keeps every page in memory for the session,
            # to strip an asset out of a few.
            document = self.existing_document(page_path)
            if document is not None:
                # Ask a snapshot and not the live content. This walks every key
                # of a page a deck can edit at the same moment, and an iteration
                # over a dict another thread adds a key to raises. Only a page
                # that answers yes is edited, so the sweep marks no page that a
                # deck shows. Both passes are idempotent, and the second finds
                # whatever the first left.
                page_had_asset = self._strip_asset(
                    document.get_without_action_objects(), abs_target_path)
                if page_had_asset:
                    with document.edit() as page_dict:
                        self._strip_asset(page_dict, abs_target_path)
            else:
                # There is no Page and no document, so this process holds none
                # of this page's content. There is nothing to serialize
                # against, and the file is the only copy. Take the read barrier anyway,
                # because this sweep reads past get_page_data.
                #
                # It goes through the settings loader, so one corrupt page
                # loads an empty dict and skips, instead of raising and
                # aborting the sweep for every page left.
                #
                # The loader quarantines a corrupt file as a side effect of any
                # read, so this read-oriented sweep can move a corrupt page to
                # <path>.corrupt although it changes no asset of that page.
                # That destroys nothing. The sidecar keeps the corrupt bytes,
                # the last good copy stays in pages/backups/ for the next
                # get_page_data to heal from, and page_had_asset stays False
                # here, so nothing writes over the corrupt page.
                page_flush.get().flush_path(page_path)
                page_dict = self.settings_manager.load_settings_from_file(page_path)
                page_had_asset = self._strip_asset(page_dict, abs_target_path)
                if page_had_asset:
                    atomic_write_json(page_path, page_dict)

                    # A deck can start to show this page while the bytes go
                    # down. The mint that did it read the file, so tell the
                    # holder about the write.
                    self.refresh_document(page_path)

            # Reload every loaded Page object of this file. This runs outside
            # the edits above. A reload takes the controller's page-load lock
            # and reads the page file, and both sit above the lock of an
            # edit.
            if page_had_asset:
                pages = self.get_pages_with_path(page_path)
                for page in pages:
                    # Reload the page while it is active on its controller.
                    if page.deck_controller.active_page == page:
                        page.deck_controller.load_page(page, allow_reload=True)

    def find_matching_page_path(self, name: str) -> str | None:
        if not name:
            return None

        # A name that is a valid full file path goes back unchanged.
        if os.path.isfile(name):
            return name

        # Normalize the name for the comparison.
        target_name = name.lower()

        for page_path in self.get_pages():
            base = os.path.basename(page_path).lower()
            base_no_ext = os.path.splitext(base)[0]

            # Check the filename with and without the extension.
            if base == target_name or base_no_ext == target_name:
                return page_path

        return None

    def backup_pages(self) -> None:
        # A timestamp string that is safe in a filename.
        time_stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

        # The path of the backup zip file.
        backup_zip_path = os.path.join(self.PAGE_PATH, "backups", f"backup_{time_stamp}.zip")

        # Make sure the backup directory exists.
        os.makedirs(os.path.dirname(backup_zip_path), exist_ok=True)

        # Read barrier for every page at once. This zips the files, so a page
        # with pending edits goes into the archive without them. The only
        # caller runs at boot, before a page can be dirty, and a zip of every
        # page is a read like any other. The flush costs nothing when there is
        # nothing to write.
        page_flush.get().flush_all()

        # Create the zip archive and add every page file.
        with zipfile.ZipFile(backup_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as backup_zip:
            for page_path in self.get_pages():
                # Add each file under its basename, with no folder in the zip.
                backup_zip.write(page_path, arcname=os.path.basename(page_path))

    def remove_old_backups(self) -> None:
        backup_dir = os.path.join(self.PAGE_PATH, "backups")

        # Return early while the backup directory is absent, or os.listdir
        # raises FileNotFoundError.
        if not os.path.exists(backup_dir):
            return

        # List every zip file in the backup directory.
        backup_files = [file for file in os.listdir(backup_dir) if file.endswith(".zip")]

        # Sort the backups by the timestamp in the filename, newest first.
        # The filename format is backup_YYYYMMDDTHHMMSS.zip.
        def extract_timestamp(filename: str) -> str:
            return filename.removeprefix("backup_").removesuffix(".zip")

        sorted_backups = sorted(backup_files, key=extract_timestamp, reverse=True)

        # Delete nothing while the count stays under the keep count.
        if len(sorted_backups) < self.MAX_BACKUPS:
            return

        # Delete the oldest backups past the number to keep.
        for old_backup in sorted_backups[self.MAX_BACKUPS-1:]:
            backup_path = os.path.join(backup_dir, old_backup)
            try:
                os.remove(backup_path)
                log.info(f"Removed old page backup file: {old_backup}")
            except Exception as e:
                log.error(f"Failed to remove backup file {old_backup}: {e}")

    def get_page_settings(self, path: str) -> dict:
        data = self.get_page_data(path, False)
        return data.get("settings", {})

    @contextmanager
    def edit_page_settings(self, path: str) -> Iterator[dict[str, Any]]:
        """Change the settings section of one page, in the page itself.

        Every override row comes through this funnel. A read of the whole file,
        a change to the settings dict, and a write of the whole file lose a
        page edit that lands in between, such as a plugin call to set_settings
        or a key edit. Here the settings are the page's settings. The mutation
        happens in the content every deck on this page reads, under the file's
        lock, and the file catches up through the flush seam.

        The read and the write share one block. An override is a partial edit,
        such as the brightness value alone, over a section that must survive.
        One hold for the read and the write leaves no gap to lose the rest in.
        """
        with self.get_document(path).edit() as data:
            settings = data.get("settings")
            if not isinstance(settings, dict):
                settings = {}
                data["settings"] = settings
            yield settings

    def set_page_settings(self, path: str, settings: dict):
        """Set the whole settings section of the page json.

        :param path: Path to the file
        :param settings: The settings dict for the settings section
        :return: None
        """
        if path is None:
            return

        with self.get_document(path).edit() as data:
            data["settings"] = settings

    def any_auto_change_rule_enabled(self) -> bool:
        """True when at least one page has its window auto-change switched on.

        Gates the active-window watcher, which is otherwise a permanent
        background poll for every user who never touches the feature.

        Reads through the same per-page accessor the matcher itself uses, so
        the gate and the matcher can never disagree about what counts as a
        rule, and stops at the first hit. Pages are the only place these
        rules live -- there is no index to consult instead -- but they are
        small (a fully populated deck's page is ~16 KB) and boot already
        reads every one of them for the startup backup. The worst case is
        therefore no rule anywhere, which parses the lot. That stays well under
        a millisecond for a normal handful of pages, and near two milliseconds
        for fifty of them, on a path that reads those bytes already.
        """
        for page_path in self.get_pages():
            try:
                if self.get_auto_change_settings(page_path).get("enable", False):
                    return True
            except Exception:
                # One unreadable page must not decide the gate for the rest.
                log.opt(exception=True).warning(f"Could not read the auto-change settings of {page_path}")
        return False

    def refresh_window_watch_state(self) -> None:
        """Re-gates the active-window watcher after anything that can add or
        remove a rule.

        Public because rules also arrive by routes that bypass every setter
        here -- an importer writing page files wholesale is the live one --
        and such a writer must be able to re-gate without reaching into the
        window grabber itself.

        It never raises into the page operation that triggered it. A watcher
        left in the wrong state costs a wasted poll or a dead auto-switch, and
        a raise here would abort a page write. It returns at once, and the
        grabber applies the decision on its own worker.
        """
        window_grabber = gl.window_grabber
        if window_grabber is None:
            return
        try:
            window_grabber.refresh_watch_state()
        except Exception:
            log.opt(exception=True).warning("Could not update the active window watcher state")

    def get_auto_change_settings(self, path: str) -> dict:
        """
        Returns the auto change settings section of the page settings
        :param path: Path to the file
        :return: dict
        """
        page_settings = self.get_page_settings(path)
        return page_settings.get("auto-change", {})

    def set_auto_change_settings(self, path: str, enable: bool = False, wm_class: str = "", regex_title: str = "", stay_on_page: bool = False, decks: list[str] = None):
        decks = decks or []

        with self.edit_page_settings(path) as settings:
            settings["auto-change"] = {
                "enable": enable,
                "wm-class": wm_class,
                "title": regex_title,
                "stay-on-page": stay_on_page,
                "decks": decks
            }

        # Outside the block, because the watcher gate re-reads every page, and
        # every read of a page file takes the lock the block above holds.
        self.refresh_window_watch_state()

    def overwrite_auto_change_settings(self, path: str, enable: bool = None, wm_class: str = None, regex_title: str = None, stay_on_page: bool = None, decks: list[str] = None):
        with self.edit_page_settings(path) as settings:
            auto_change_settings = settings.setdefault("auto-change", {})

            if enable is not None:
                auto_change_settings["enable"] = enable
            if wm_class is not None:
                auto_change_settings["wm-class"] = wm_class
            if regex_title is not None:
                auto_change_settings["title"] = regex_title
            if stay_on_page is not None:
                auto_change_settings["stay-on-page"] = stay_on_page
            if decks is not None:
                auto_change_settings["decks"] = decks

        self.refresh_window_watch_state()

    def get_screensaver_settings(self, path: str):
        page_settings = self.get_page_settings(path)
        return page_settings.get("screensaver", {})

    def set_screensaver_settings(self, path: str, overwrite: bool = False, enable: bool = False, time_delay: int = 5, loop: bool = True, fps: int = 30, brightness: float = 30, media_path: str = ""):
        with self.edit_page_settings(path) as settings:
            settings["screensaver"] = {
                "overwrite": overwrite,
                "enable": enable,
                "time-delay": time_delay,
                "loop": loop,
                "fps": fps,
                "brightness": brightness,
                "media-path": media_path
            }

    def overwrite_screensaver_settings(self, path: str, overwrite: bool = None, enable: bool = None, time_delay: int = None, loop: bool = None, fps: int = None, brightness: float = None, media_path: str = None):
        with self.edit_page_settings(path) as settings:
            screensaver_settings = settings.setdefault("screensaver", {})

            if overwrite is not None:
                screensaver_settings["overwrite"] = overwrite
            if enable is not None:
                screensaver_settings["enable"] = enable
            if time_delay is not None:
                screensaver_settings["time-delay"] = time_delay
            if loop is not None:
                screensaver_settings["loop"] = loop
            if fps is not None:
                screensaver_settings["fps"] = fps
            if brightness is not None:
                screensaver_settings["brightness"] = brightness
            if media_path is not None:
                screensaver_settings["media-path"] = media_path

    def get_brightness_settings(self, path: str):
        page_settings = self.get_page_settings(path)
        return page_settings.get("brightness", {})

    def set_brightness_settings(self, path: str, overwrite: bool = False, brightness: float = 75):
        with self.edit_page_settings(path) as settings:
            settings["brightness"] = {
                "overwrite": overwrite,
                "value": brightness
            }

    def overwrite_brightness_settings(self, path: str, overwrite: bool = None, brightness: float = None):
        with self.edit_page_settings(path) as settings:
            brightness_settings = settings.setdefault("brightness", {})

            if overwrite is not None:
                brightness_settings["overwrite"] = overwrite
            if brightness is not None:
                brightness_settings["value"] = brightness

    def get_background_settings(self, path: str):
        page_settings = self.get_page_settings(path)
        return page_settings.get("background", {})

    def set_background_settings(self, path: str, overwrite: bool = False, show: bool = False, fps: int = 30, loop: bool = False, media_path: str = "", extend_to_touchscreen: bool = False):
        with self.edit_page_settings(path) as settings:
            settings["background"] = {
                "overwrite": overwrite,
                "show": show,
                "fps": fps,
                "loop": loop,
                "media-path": media_path,
                "extend-to-touchscreen": extend_to_touchscreen
            }

    def overwrite_background_settings(self, path: str, overwrite: bool = None, show: bool = None, fps: int = None, loop: bool = None, media_path: str = None, extend_to_touchscreen: bool = None):
        with self.edit_page_settings(path) as settings:
            background_settings = settings.setdefault("background", {})

            if overwrite is not None:
                background_settings["overwrite"] = overwrite
            if show is not None:
                background_settings["show"] = show
            if fps is not None:
                background_settings["fps"] = fps
            if loop is not None:
                background_settings["loop"] = loop
            if media_path is not None:
                background_settings["media-path"] = media_path
            if extend_to_touchscreen is not None:
                background_settings["extend-to-touchscreen"] = extend_to_touchscreen
