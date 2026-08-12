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
# Import Python modules
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

# Import own modules
from src.backend.PageManagement.Page import Page
from src.backend.PageManagement import page_flush
from src.backend.PageManagement.page_flush import canonical_path
from src.backend.PageManagement.page_document import PageDocument
from src.backend.PageManagement.page_pins import PagePins
from src.backend.DeckManagement.HelperMethods import natural_sort_by_filenames
from src.backend.atomic_json import atomic_write_json
from src.backend import settings_store

# Import globals
import globals as gl


class PageEntry(TypedDict):
    """One cached page slot: the Page object plus its LRU stamp."""
    page: "Page"
    page_number: int


class PageManagerBackend:
    def __init__(self, settings_manager):
        self.settings_manager = settings_manager

        # Guards `pages`: it's read/mutated from arbitrary threads --
        # DeckController.close() pops one controller's whole entry (P1.3
        # step 8) while clear_old_cached_pages() may concurrently be
        # iterating (and evicting from) a *different* controller's entry on
        # that deck's own media/tick thread (design doc M5). An RLock so the
        # methods below can call each other without deadlocking.
        self._pages_lock = threading.RLock()
        self.pages: dict["DeckController", dict[str, PageEntry]] = {}
        # In-flight cache-miss constructions, keyed (controller, path) and
        # guarded by _pages_lock: a second caller for the same page waits on
        # the first construction instead of building a twin Page whose
        # actions register live event/signal handlers.
        self._loads_in_flight: dict[tuple, tuple[threading.Thread, threading.Event]] = {}
        # One document per page file, keyed the way every other per-page
        # registry in the process is keyed (the flush seam's canonical_path),
        # so a page reached by two spellings is one document with one save
        # lock and one pending record rather than two of each. Never pruned:
        # page_document's header carries why an entry can never be dropped
        # while a Page might still read through it, and what holding a whole
        # page dict per visited path costs. A page deleted and later recreated
        # under the same name reuses its document, which the load that mints
        # the new Page fills from the new file.
        self._documents: dict[str, PageDocument] = {}
        self._documents_guard = threading.Lock()
        # Holders of a cached page that eviction cannot see for itself.
        # Public: the deck controller brackets its tick and key work with it.
        self.pins = PagePins()
        self.custom_pages = []

        self.page_order = []

        # `n-cached-pages` means "cached pages besides the active one" in the
        # Settings UI; set_pages_to_cache() (called when the user changes the
        # spinner) accordingly stores max_pages = value + 1. Read the same
        # setting with the same +1 here so a fresh boot gets the identical
        # cache budget the Settings window would apply for the same value --
        # before this fix, merely *opening* Settings once (even without
        # touching the spinner) silently grew the live budget by one, since
        # this constructor hardcoded 3 instead of applying the +1 (design
        # doc bug 35).
        n_cached_pages = self.settings_manager.app().n_cached_pages
        self.max_pages = int(n_cached_pages) + 1
        self.page_number = 0

        self.MAX_BACKUPS = 5
        self.PAGE_PATH = os.path.join(gl.DATA_PATH, "pages")

    def load_page(self, path: str, deck_controller: "DeckController") -> Page | None:
        """
        This loads the page into the page dict and increases the current page number.
        :param path: The path to the page
        :param deck_controller: The deck controller instance that the page belongs to
        :return: The newly created page object
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
                # Re-entrant miss from inside our own construction (a plugin
                # loading this same page during action init): waiting would
                # self-deadlock, so fall back to constructing directly --
                # the pre-guard twin-page tradeoff, now confined to this
                # pathological case.
                page_object = self.load_page(path, deck_controller)
                self.pins.reserve_fetch(page_object, deck_controller)
                self.clear_old_cached_pages()
                return page_object

            done.wait()
            # The builder finished (or failed): re-check the cache. On a
            # failed/None load the next pass finds neither a cache entry nor
            # an in-flight load and this caller becomes the builder itself.

        # Cache miss: load_page() takes the lock itself for the actual
        # insert -- constructing Page() (file I/O) outside our hold here
        # keeps a slow load from stalling unrelated controllers' lookups.
        try:
            page_object = self.load_page(path, deck_controller)
        finally:
            # Always release waiters, even when construction raises --
            # they re-check the cache and take over if it's still empty.
            with self._pages_lock:
                self._loads_in_flight.pop(in_flight_key, None)
            done.set()

        # Reserved BEFORE this fetch's own eviction pass, which reaches a
        # fresh page (it sorts last) only when the excess covers all of them.
        self.pins.reserve_fetch(page_object, deck_controller)
        self.clear_old_cached_pages()
        return page_object

    def discard_controller(self, deck_controller: "DeckController") -> None:
        """Drops every cached page entry for a torn-down controller (plan
        docs/memory-footprint-impl-plan.md P1.3 step 8; design doc bug 1):
        the dead controller's active_page was otherwise permanently
        unevictable and kept distorting clear_old_cached_pages()'s budget
        for every other live controller.

        Pending edits are written first. The deck this ran for is going away
        -- unplugged, or the app quitting -- so every page it was showing has
        reached a boundary, and the entries that hold those pages are about
        to go. That is the difference from eviction, which never writes: it
        is a memory reclaim on a running deck, and a page evicted with edits
        outstanding is kept alive by the flush seam itself.

        Best-effort per page, like every other step of a teardown: a page
        that will not serialize must not abort the deregistration and leave
        the controller in the cache forever."""
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
            # The deck is gone, so its outstanding fetch has no future holder.
            self.pins.release_fetch(deck_controller)

    def pages_for_controller(self, deck_controller: "DeckController") -> list["Page"]:
        """Snapshot of every cached Page object for one controller. Used by
        DeckController.close() step 6 so it can run clear_action_objects()
        (which may invoke plugin hooks) without holding `_pages_lock` while
        doing so."""
        with self._pages_lock:
            cached = self.pages.get(deck_controller, {})
            return [entry["page"] for entry in cached.values() if entry.get("page") is not None]

    def all_cached_pages(self) -> list["Page"]:
        """Snapshot of every cached Page object across ALL controllers,
        taken under `_pages_lock` -- the all-controllers sibling of
        pages_for_controller(). Callers iterate the snapshot without
        holding the lock (page teardown may invoke plugin hooks)."""
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
        # Eviction is an IN-MEMORY teardown ONLY: it never writes to disk
        # (no Page.save/atomic_write_json here) and never touches the page's
        # JSON. clear_action_objects() tears down live action objects and
        # drops the cache entry -- nothing on disk is mutated. So the harm of
        # gutting a live page is USE-AFTER-EVICT -- a visible page
        # whose actions are all dead (keypresses do nothing, imagery frozen)
        # plus a duplicate Page minted on the next get_page() -- recoverable
        # by a page reload/replug; it is NOT on-disk data loss.
        #
        # Snapshot the eviction decision under the lock, then act on it
        # outside: clear_action_objects() below can run plugin hooks (D1),
        # and a wedged one must not stall a concurrent close()/get_page()
        # from some other controller waiting on this same lock (design doc
        # M5 -- close() pops its whole controller entry under this lock from
        # its own dedicated thread while this can run on any deck's
        # media/tick thread).
        with self._pages_lock:
            total = sum(len(controller_pages) for controller_pages in self.pages.values())
            excess = total - self.max_pages
            if excess <= 0:
                return

            # Oldest first by page_number; the active page of each controller
            # and every pinned page (fetched, mid-tick, mid-gesture) are safe.
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

        # A concurrent discard_controller() may have already popped one of
        # these controllers' whole entry out from under us; controller_pages
        # is still the same (now possibly orphaned) dict object, so the pop
        # below is a harmless no-op in that case rather than a KeyError.
        for _, controller_pages, path, page_obj in to_evict:
            # Re-validate under the lock immediately before teardown:
            # between the snapshot and this point the page may have
            # become live -- activated by a load_page (WindowGrabber cycling
            # generates exactly this cache pressure), stashed as a
            # controller's screensaver-pending page, or newly pinned by a
            # fetch or a bracket. Pop INSIDE the lock and BEFORE the teardown,
            # so a concurrent get_page() mints a fresh Page instead of
            # receiving this gutted one.
            with self._pages_lock:
                page_data = controller_pages.get(path)
                if page_data is None or page_data.get("page") is not page_obj:
                    continue  # already discarded/replaced
                if self.pins.is_pinned(page_obj):
                    continue
                if self._page_is_live(page_obj):
                    continue
                controller_pages.pop(path, None)
            log.info(f"Evicting cached page {path}")
            # Teardown stays OUTSIDE the lock: it can run plugin hooks (D1),
            # and a wedged one must not stall close()/get_page() waiting on
            # this lock.
            page_obj.clear_action_objects()

    def _page_is_live(self, page_obj) -> bool:
        """True when any controller currently depends on this Page object:
        active, or stashed as the screensaver-pending page (held for the
        whole screensaver duration and invisible to the snapshot guards --
        evicting it made ScreenSaver.hide() load a page whose every action
        was dead).

        Kept alongside the pins, not folded into them: this is DERIVED from
        what the controllers hold, so it is exact and has no release to drop.
        Pins cover the complement -- the windows where no controller field
        names the page yet, or no longer does while work on it is in flight."""
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

    # path=None is the documented "clear this deck's default page" value --
    # get_all_default_page_serial_numbers skips falsy entries by design.
    def set_default_page(self, deck_serial_number: str, path: str | None):
        # Read-modify-write serialized against every other edit of pages.json:
        # the store's per-file edit lock is the ONLY lock this takes, and it is
        # never held while _pages_lock is acquired (see remove_page/move_page).
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
        # Read barrier: the copy below is a read of the old file, so edits
        # still pending for it belong on disk first or the renamed page
        # arrives without them.
        page_flush.get().flush_path(old_path)

        # And the destination is about to be replaced wholesale, so anything
        # pending for THAT path would land after the copy and undo the
        # rename. Symmetric with the source discard below; a no-op unless the
        # new name already had a page.
        page_flush.get().discard_path(new_path)

        shutil.copy2(old_path, new_path)

        # The content follows the file, before the loop below rather than
        # after it: the loop asks for the page under its OLD name, which mints
        # a Page (and with it a document) for any deck that did not have it
        # cached. Moving first leaves those mints to a throwaway document at
        # the old name and keeps the one carrying the real content -- pending
        # edits included -- out of their reach.
        document = self.rename_document(old_path, new_path)

        # Update Path in Objects
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if controller.active_page is None:
                continue

            page = self.get_page(old_path, controller)

            if not page:
                continue

            page.json_path = new_path
            page.rebind_document(document)
            # A rename is not a handoff -- nothing here activates the page --
            # so this fetch retires on the spot.
            self.pins.release_fetch(controller)

        # Update the path in the page-manager settings, serialized against every
        # other edit of pages.json. The store's edit lock is taken AFTER the
        # controller loop above has released _pages_lock, never while holding it:
        # this is the one lock order for the two locks.
        with settings_store.get().edit(settings_store.PAGES) as page_settings:
            default_pages = page_settings.get("default-pages", {})
            for serial_number, path in default_pages.items():
                if path != old_path:
                    continue
                default_pages[serial_number] = new_path
            page_settings["default-pages"] = default_pages

        # Retire any write still pending for the file about to disappear --
        # a timer firing after the removal would write the moved-from page
        # back into existence. Deliberately AFTER the json_path re-point
        # above: a mark reads json_path and inserts its entry under one lock,
        # so once every Page points at the new path no further mark can land
        # under the old key, and this discard is the last one that can.
        # Edits marked between the flush above and the re-point are dropped
        # from the pending map only, not from memory -- they are still in the
        # page's dict, and the next save carries them to the new path.
        page_flush.get().discard_path(old_path)

        os.remove(old_path)
        self.refresh_window_watch_state()

    def remove_page(self, page_path: str):
        # Iterate over all deck controllers to handle any that are using the page to be removed
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # A page change requested while the screensaver owns the deck is
            # stashed as _screensaver_pending_page (load_page's screensaver
            # guard) -- invisible to the active_page checks below. If THAT
            # page is being deleted, drop the request and its cache entry:
            # hide() would otherwise load a page whose file is gone, and the
            # first save would resurrect the deleted file. The
            # controller then simply stays on its current page on dismiss.
            pending = getattr(controller, "_screensaver_pending_page", None)
            if pending is not None and pending.json_path == page_path:
                controller._screensaver_pending_page = None
                with self._pages_lock:
                    controller_pages = self.pages.get(controller, {})
                    entry = controller_pages.pop(page_path, None)
                    if not controller_pages:
                        self.pages.pop(controller, None)
                if entry is not None:
                    # Outside the lock, same rationale as the teardown below.
                    entry["page"].clear_action_objects()
                # The reservation the stash was carrying to hide() has nowhere
                # left to arrive: the request is dropped and its entry popped,
                # so nothing installs the page and nothing else retires it.
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
                # Fallback: load the first available page if default is being deleted
                page_list = self.get_pages()
                if page_path in page_list:
                    page_list.remove(page_path)
                new_page = self.get_page(page_list[0], controller) if page_list else None

            if new_page:
                controller.load_page(new_page)
            else:
                # No replacement to install, so the install that would have
                # retired this deck's outstanding fetch never happens -- and
                # that fetch may be the page being deleted, held against
                # eviction until the deck asks for another one.
                self.pins.release_fetch(controller)

            # Remove the page from the created pages cache for this controller
            with self._pages_lock:
                controller_pages = self.pages.get(controller, {})
                entry = controller_pages.pop(page_path, None)
            if entry is not None:
                # No _page_is_live guard, unlike eviction: this cannot gut a
                # page another controller is showing, because the cache holds
                # a distinct Page per (controller, path) and this pops only
                # THIS controller's. For this controller the page is either no
                # longer active (load_page above moved it to a different path
                # -- the default and the fallback both exclude page_path) or
                # there was no page left to move it to, and the file is being
                # deleted either way. A controller showing its screensaver
                # never reaches here (the top guard's path mismatch), so this
                # is never its pending page either.
                #
                # Outside the lock: clear_action_objects() may run plugin
                # hooks, which must not stall a concurrent close()/get_page()
                # waiting on _pages_lock from another thread.
                entry["page"].clear_action_objects()
                # Remove the controller entry entirely if it no longer has cached pages
                if not controller_pages:
                    with self._pages_lock:
                        self.pages.pop(controller, None)

        # Throw away any write still pending for this page rather than
        # flushing it: a timer firing after the removal below would write the
        # deleted page straight back onto disk. Placed as late as possible,
        # after the cache teardown above has dropped every in-tree holder of
        # the Page, so nothing that could re-mark it is still running.
        page_flush.get().discard_path(page_path)

        # Delete the JSON file representing the page
        if os.path.exists(page_path):
            os.remove(page_path)

        # Remove any references to this page in the default-pages setting.
        # Serialized against every other edit of pages.json; taken here, after
        # the controller loop above has released _pages_lock, never while it is
        # held -- the one lock order for the store edit lock and _pages_lock.
        with settings_store.get().edit(settings_store.PAGES) as settings:
            default_pages = settings.get("default-pages", {})
            settings["default-pages"] = {
                serial: path for serial, path in default_pages.items() if path != page_path
            }

        # Deleting the page that carried the only rule takes the rule with
        # it, so the watcher has to be re-gated here too.
        self.refresh_window_watch_state()

    def add_page(self, page_name: str, page_dict: dict = None) -> str:
        page_dict = page_dict or {}

        # The app creates the pages dir at startup, but callers before/
        # outside that init (tests, future code paths) must not crash here.
        os.makedirs(self.PAGE_PATH, exist_ok=True)

        path = os.path.join(self.PAGE_PATH, f"{page_name}.json")
        if os.path.exists(path):
            raise FileExistsError(f"A page with the name '{page_name}' already exists.")

        # An imported or duplicated page arrives with its settings already in
        # page_dict, rule included, so this is a rule-adding path.
        #
        # The FILE is new; the pending key is not. A delete discards that
        # path's outstanding write, but cannot stop the Page and document
        # that held it from marking it again afterwards (a screensaver-pending
        # reference, a widget, a plugin thread finishing a save) -- and the
        # read barrier then turns that straggler into a resurrection: the
        # refresh below reads the page, the barrier writes the outstanding
        # edits out first, and the deleted page lands on top of the file just
        # created, which the document adopts. So discard first, the way an
        # import does before replacing a page wholesale, and refresh after:
        # the document survives a delete too (the registry never drops one --
        # a Page may still be reading through it), so without the re-read the
        # new page's first edit writes the old page back out under its name.
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

        # A registered custom page is matched like any other, so its rule
        # counts towards the watcher gate from here on.
        self.refresh_window_watch_state()

    def unregister_page(self, path: str):
        if not self.custom_pages.__contains__(path):
            return

        self.custom_pages.remove(path)
        gl.signal_manager.trigger_signal(Signals.PageDelete, path)
        self.refresh_window_watch_state()

    def get_pages_with_path(self, path: str):
        pages_set = set()

        # Reads of self.pages must hold _pages_lock: discard_controller()
        # pops whole controller entries from another thread, so the
        # unlocked membership-check/lookup pair here raced it into a
        # KeyError. Lookups only -- no plugin hooks run under the lock.
        with self._pages_lock:
            for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
                # Check active_page
                page = controller.active_page
                if page is not None and page.json_path == path:
                    pages_set.add(page)

                # Check in page cache for the same controller
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
                # Deck present but no page loaded yet (boot, or a page load that
                # failed) -- nothing to reload.
                log.warning(f"Deck {controller.serial_number()} has no active page; skipping reload")
                continue
            controller.load_page(active_page, allow_reload=True)

    def get_document(self, path: str) -> PageDocument:
        """The one document holding `path`'s content, minted on first ask.

        Every Page on a path goes through here, which is what makes them
        share a dict instead of each holding a copy of the file.
        """
        key = canonical_path(path)
        with self._documents_guard:
            document = self._documents.get(key)
            if document is None:
                document = PageDocument(path)
                self._documents[key] = document
            return document

    def existing_document(self, path: str) -> PageDocument | None:
        """The document holding `path`, or None -- never minting one.

        For callers that have something to say to a page only if this process
        is holding it: the asset sweep, which otherwise reads and writes the
        file, and the refresh below. Minting here instead would keep a whole
        page dict alive for every page file the sweep walks past.
        """
        with self._documents_guard:
            return self._documents.get(canonical_path(path))

    def refresh_document(self, path: str) -> None:
        """Re-read `path` into the document holding it, if there is one.

        For the writers that put bytes in a page file without going through
        the page that owns it -- an import, a page created under a name whose
        document still holds a deleted page's content. Every Page on that path
        is looking at the document, so one re-read is the whole propagation.

        A path this process has never held is left alone rather than loaded:
        there is no in-memory content to correct, and the load that mints its
        first Page reads the file anyway.
        """
        document = self.existing_document(path)
        if document is None:
            return
        document.refresh_from_disk()

    def rename_document(self, old_path: str, new_path: str) -> PageDocument:
        """Re-file `old_path`'s content under `new_path` and return it.

        A page move re-points every Page's json_path in place, so the document
        those Pages read through has to follow the same file -- otherwise the
        next page created under the freed-up old name would be handed the
        moved page's content.

        The document object itself is carried over rather than reloaded,
        because it can hold edits that are on no disk yet: a save landing in
        the move's own window is dropped from the pending map (a timer firing
        after the move would write the moved-from file back into existence)
        but stays in memory for the next save to carry to the new name. A
        reload here would be exactly the thing that loses it.

        Three outcomes, all of them reachable:

        * The usual one -- a document at the old name, which moves.
        * A document already standing at the destination while one also moves
          onto it: the mover wins the key and the standing one is DISPLACED,
          left to the Pages that hold it. Their content is their own and their
          json_path is unchanged, so they carry on writing it exactly as they
          did before, but nothing refreshes them any more. Only a rename onto
          a page name another deck is showing produces this.
        * A document standing at the destination with nothing moving onto it,
          because no Page in this process held the moved-from page. Nothing is
          displaced; the standing document IS the destination and is re-read,
          because the move has just replaced the file underneath it.
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
        # Nothing moved onto this document, and the file underneath it is now
        # the moved page: a document already standing here would otherwise
        # keep serving the overwritten content to every Page reading through
        # it (and put it back on disk at the next save), and a document just
        # minted here has no content at all. Outside the guard, because the
        # re-read goes through the read barrier and so takes a save lock.
        document.refresh_from_disk()
        return document

    def get_page_data(self, path: str, use_backup: bool = True) -> dict:
        """One page file's whole content, read from disk. A primary that is
        missing or unreadable is substituted from ``pages/backups/`` -- the
        missing case only when `use_backup`, the corrupt case always, for the
        reason spelled out at the heal below."""
        if path is None:
            return {}

        # Read barrier: edits still in flight for this page belong on disk
        # before anything reads the file. One dict lookup when there are none.
        page_flush.get().flush_path(path)

        backup_path = os.path.join(self.PAGE_PATH, "backups", os.path.basename(path))

        # Missing primary: substitute the backup (only when use_backup).
        if not os.path.exists(path) and os.path.exists(backup_path) and use_backup:
            path = backup_path

        data, corrupt = self.settings_manager.load_settings_reporting_corruption(path)

        # Corrupt primary: heal from the backup REGARDLESS of use_backup and
        # regardless of whether the loader could move the primary aside. This
        # is the crux of the corrupt-read problem: every page-settings mutator
        # reads via get_page_data(path, False) and then writes the result straight back
        # (set_page_settings). Returning {} for a corrupt page there guts the
        # live page (keys/background/dials erased) and, once written, the
        # gutted file is valid JSON so auto-heal never recovers it. The heal
        # is a property of the load RESULT (corrupt + backup available), not
        # of the quarantine rename having happened to remove the primary.
        if corrupt and path != backup_path and os.path.exists(backup_path):
            healed, backup_corrupt = self.settings_manager.load_settings_reporting_corruption(backup_path)
            if not backup_corrupt:
                data = healed
        return data

    def set_page_data(self, path: str, data: dict, reload_brightness: bool = True, reload_screensaver: bool = True, reload_background: bool = True, reload_inputs: bool = True):
        """Replace a whole page with `data` -- the editor that hands back a
        page json rather than a change to one.

        Through the document, not over the file: writing the file left the
        page itself holding whatever it held before, so an edit of that page
        still on its timer wrote the pre-replacement content back over this
        one moments later. Replacing the content instead makes the two the
        same thing.
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

        Returns whether the page referenced it at all -- the sweep below only
        writes the pages that did.
        """
        page_had_asset = False

        # Every section is read defensively: a page json need not carry keys,
        # states or media at all.
        for key, key_data in page_dict.get("keys", {}).items():
            for state, state_data in key_data.get("states", {}).items():
                dict_path = state_data.get("media", {}).get("path")
                if dict_path is None:
                    continue

                # Compare absolute paths; if match, remove asset reference
                if os.path.abspath(dict_path) == abs_target_path:
                    page_had_asset = True
                    state_data["media"]["path"] = None  # Remove the asset path

        return page_had_asset

    def remove_asset_from_all_pages(self, path: str):
        # Validate input path; reject empty or None
        if not path:
            raise ValueError("Invalid path")

        # Compute absolute path once for comparison
        abs_target_path = os.path.abspath(path)

        # Iterate over all page files (paths)
        for page_path in self.get_pages():
            # A page this process is holding is edited where it lives. The
            # sweep used to read every page file and write the changed ones
            # back, which for a page a deck is showing meant writing round
            # the outside of the very content that page keeps editing --
            # whichever of the two finished second won the whole file. Only
            # pages already held go this way: minting a document for every
            # page file the sweep walks would keep all of them in memory for
            # the rest of the session to strip an asset out of a handful.
            document = self.existing_document(page_path)
            if document is not None:
                # Asked of a snapshot, not of the live content: this walks
                # every key of a page a deck may be editing at the same
                # moment, and iterating a dict another thread adds a key to
                # raises. Only the pages that answer yes are then edited, so a
                # sweep does not mark every page a deck happens to be showing
                # as needing a write; both passes are idempotent, the second
                # finding whatever the first left.
                page_had_asset = self._strip_asset(
                    document.get_without_action_objects(), abs_target_path)
                if page_had_asset:
                    with document.edit() as page_dict:
                        self._strip_asset(page_dict, abs_target_path)
            else:
                # No Page and no document: nothing in this process is holding
                # this page's content, so there is nothing to serialize
                # against and the file is the only copy. Read barrier first
                # anyway, because this sweep reads past get_page_data.
                #
                # Via the settings loader so one corrupt page (loads {})
                # skips instead of raising and aborting the sweep for every
                # remaining page.
                #
                # NOTE: the loader quarantines a corrupt file as a side effect
                # of ANY read -- so this read-oriented sweep may move a
                # corrupt page aside (to <path>.corrupt) even though it
                # changes nothing about that page's assets. That is
                # non-destructive: the sidecar keeps the corrupt bytes, the
                # last good copy survives in pages/backups/ (the next
                # get_page_data heals from it), and page_had_asset stays False
                # here so nothing is written back over the poison page.
                page_flush.get().flush_path(page_path)
                page_dict = self.settings_manager.load_settings_from_file(page_path)
                page_had_asset = self._strip_asset(page_dict, abs_target_path)
                if page_had_asset:
                    # Write updated page data back to file (atomically)
                    atomic_write_json(page_path, page_dict)

                    # A deck can have started showing this page while the
                    # bytes were going down; the mint that did it read the
                    # file, so tell whatever now holds it about the write.
                    self.refresh_document(page_path)

            # Reload any loaded Page objects corresponding to this file.
            # Outside any edit above: a reload takes the controller's
            # page-load lock and reads the page file, both of which sit above
            # the lock an edit holds.
            if page_had_asset:
                pages = self.get_pages_with_path(page_path)
                for page in pages:
                    # Reload the page if it is currently active on its controller
                    if page.deck_controller.active_page == page:
                        page.deck_controller.load_page(page, allow_reload=True)

    def find_matching_page_path(self, name: str) -> str | None:
        if not name:
            return None

        # If 'name' is already a valid full file path, return it directly
        if os.path.isfile(name):
            return name

        # Normalize the name for comparison
        target_name = name.lower()

        for page_path in self.get_pages():
            base = os.path.basename(page_path).lower()
            base_no_ext = os.path.splitext(base)[0]

            # Check exact filename or filename without extension
            if base == target_name or base_no_ext == target_name:
                return page_path

        return None

    def backup_pages(self) -> None:
        # Create a timestamp string safe for filenames
        time_stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

        # Create backup zip file path
        backup_zip_path = os.path.join(self.PAGE_PATH, "backups", f"backup_{time_stamp}.zip")

        # Ensure backup directory exists
        os.makedirs(os.path.dirname(backup_zip_path), exist_ok=True)

        # Read barrier for every page at once: this zips the files, so a page
        # with edits still pending would go into the archive without them.
        # The only caller today runs at boot, before any page can be dirty --
        # but a zip of every page is a read like any other, and asking for
        # the flush costs nothing when there is nothing to write.
        page_flush.get().flush_all()

        # Create a zip archive and add all page files
        with zipfile.ZipFile(backup_zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as backup_zip:
            for page_path in self.get_pages():
                # Add each file with only its basename (no folders inside zip)
                backup_zip.write(page_path, arcname=os.path.basename(page_path))

    def remove_old_backups(self) -> None:
        backup_dir = os.path.join(self.PAGE_PATH, "backups")

        # early return if backup directory doesn't exist yet
        # otherwise os.listdir will throw a FileNotFoundError
        if not os.path.exists(backup_dir):
            return

        # List all zip files in the backup directory
        backup_files = [file for file in os.listdir(backup_dir) if file.endswith(".zip")]

        # Sort backups by timestamp embedded in filename, descending (newest first)
        # Assuming filename format: backup_YYYYMMDDTHHMMSS.zip
        def extract_timestamp(filename: str) -> str:
            # Extract the timestamp part, e.g. "20250530T142530" from "backup_20250530T142530.zip"
            return filename.removeprefix("backup_").removesuffix(".zip")

        sorted_backups = sorted(backup_files, key=extract_timestamp, reverse=True)

        # If backups are fewer than or equal to keep count, no deletion needed
        if len(sorted_backups) < self.MAX_BACKUPS:
            return

        # Delete oldest backups beyond the number to keep
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
        """Change one page's settings section, in the page itself.

        The funnel every override row goes through. Each of them used to read
        the whole page file, change the settings dict it got back, and write
        the whole file again -- so a page edit landing in between (a plugin
        calling set_settings, a key edit, anything reaching Page.save) either
        lost its own change or, once the page was told to re-read, took this
        one down with it. Here the settings ARE the page's settings: the
        mutation happens in the content every deck showing this page is
        already reading, under the file's lock, and the file catches up
        through the flush seam like any other page edit.

        Read-modify-write inside one block on purpose. The overrides are
        partial edits ("just the brightness value") over a section the rest of
        which has to survive, and doing the reading in the same hold as the
        writing is what leaves no gap to lose the other half in.
        """
        with self.get_document(path).edit() as data:
            settings = data.get("settings")
            if not isinstance(settings, dict):
                settings = {}
                data["settings"] = settings
            yield settings

    def set_page_settings(self, path: str, settings: dict):
        """
        Sets the whole settings section of the page json
        :param path: Path to the file
        :param settings: Settings dictionary to write into settings section of the file.
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
        therefore no rule anywhere, which parses the lot: well under a
        millisecond for a normal handful of pages, a couple of milliseconds
        for fifty of them, on a path already touching those bytes.
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

        Never raises into the page operation that triggered it: a watcher
        left in the wrong state is a wasted poll or a dead auto-switch, but
        a raise here would abort a page write. Returns immediately; the
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

        # Outside the block: the watcher gate re-reads every page, and every
        # read of a page file takes the lock the block above is holding.
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
