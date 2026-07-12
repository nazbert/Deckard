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
import dataclasses
# Import Python modules
import datetime
import gc
import os
import shutil
import json
import threading
import zipfile
from copy import copy
from signal import Signals
import time
from typing import Union

from loguru import logger as log

from src.Signals import Signals
from src.backend.DeckManagement.DeckController import DeckController

# Import own modules
from src.backend.PageManagement.Page import Page
from src.backend.PageManagement.DummyPage import DummyPage
from src.backend.DeckManagement.HelperMethods import get_sub_folders, natural_sort, natural_sort_by_filenames, recursive_hasattr, sort_times
from src.backend.atomic_json import atomic_write_json

# Import globals
import globals as gl


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
        self.pages: dict["DeckController", dict[str, dict[str, Union["Page", int]]]] = {}
        # In-flight cache-miss constructions, keyed (controller, path) and
        # guarded by _pages_lock: a second caller for the same page waits on
        # the first construction instead of building a twin Page whose
        # actions register live event/signal handlers (issue #55).
        self._loads_in_flight: dict[tuple, tuple[threading.Thread, threading.Event]] = {}
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
        n_cached_pages = self.settings_manager.get_app_settings().get("performance", {}).get("n-cached-pages", 3)
        self.max_pages = int(n_cached_pages) + 1
        self.page_number = 0

        self.MAX_BACKUPS = 5
        self.PAGE_PATH = os.path.join(gl.DATA_PATH, "pages")
        self.PAGE_SETTINGS_PATH = os.path.join(gl.DATA_PATH, "settings", "pages.json")

    def load_page(self, path: str, deck_controller: "DeckController") -> Page:
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

    def get_page(self, path: str, deck_controller: "DeckController") -> Page:
        in_flight_key = (deck_controller, path)

        while True:
            with self._pages_lock:
                page = self.pages.get(deck_controller, {}).get(path, {})
                if page:
                    page["page_number"] = self.page_number
                    page_object = page["page"]
                    self.page_number += 1
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

        self.clear_old_cached_pages()
        return page_object

    def discard_controller(self, deck_controller: "DeckController") -> None:
        """Drops every cached page entry for a torn-down controller (plan
        docs/memory-footprint-impl-plan.md P1.3 step 8; design doc bug 1):
        the dead controller's active_page was otherwise permanently
        unevictable and kept distorting clear_old_cached_pages()'s budget
        for every other live controller."""
        with self._pages_lock:
            self.pages.pop(deck_controller, None)

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
        # gutting a live page (issue #4) is USE-AFTER-EVICT -- a visible page
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
            # and pages mid-tick (ready_to_clear False) are never evicted.
            evictable = []
            for controller, controller_pages in self.pages.items():
                if controller.active_page is None:
                    continue
                for path, page_data in controller_pages.items():
                    page_obj = page_data["page"]
                    if page_obj is controller.active_page:
                        continue
                    if not page_obj.ready_to_clear:
                        continue
                    evictable.append((page_data["page_number"], controller_pages, path, page_obj))

            evictable.sort(key=lambda entry: entry[0])
            to_evict = evictable[:excess]

        # A concurrent discard_controller() may have already popped one of
        # these controllers' whole entry out from under us; controller_pages
        # is still the same (now possibly orphaned) dict object, so the pop
        # below is a harmless no-op in that case rather than a KeyError.
        for _, controller_pages, path, page_obj in to_evict:
            # Re-validate under the lock immediately before teardown (issue
            # #4): between the snapshot and this point the page may have
            # become live -- activated by a load_page (WindowGrabber cycling
            # generates exactly this cache pressure), stashed as a
            # controller's screensaver-pending page, or re-marked mid-tick.
            # Pop INSIDE the lock and BEFORE the teardown, so a concurrent
            # get_page() mints a fresh Page instead of receiving this gutted
            # one.
            #
            # Residual NOT covered here (deferred to #81, pin-counts): a page
            # already fetched via get_page() but not yet handed to load_page()
            # is neither active_page nor screensaver-pending and is born
            # ready_to_clear=True (Page.__init__), so it stays evictable in
            # this pre-activation window. The harm there is narrow: its
            # action_objects are still empty (built by load_page ->
            # initialize_actions, which runs only after active_page is set),
            # so clear_action_objects() is a near-no-op -- the observable
            # effect is just the duplicate-Page mint. Only ownership
            # pin-counts (#81) close this structurally.
            with self._pages_lock:
                page_data = controller_pages.get(path)
                if page_data is None or page_data.get("page") is not page_obj:
                    continue  # already discarded/replaced
                if not page_obj.ready_to_clear:
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
        was dead; issue #4 window 1)."""
        for controller in gl.deck_manager.deck_controller:
            if controller.active_page is page_obj:
                return True
            if getattr(controller, "_screensaver_pending_page", None) is page_obj:
                return True
        return False

    def get_default_page(self, deck_serial_number: str):
        page_settings = self.settings_manager.load_settings_from_file(self.PAGE_SETTINGS_PATH)
        page_path = page_settings.get("default-pages", {}).get(deck_serial_number, None)

        if page_path and os.path.isfile(page_path):
            return page_path

        return None

    def set_default_page(self, deck_serial_number: str, path: str):
        page_settings = self.settings_manager.load_settings_from_file(self.PAGE_SETTINGS_PATH)
        page_settings.setdefault("default-pages", {})
        page_settings["default-pages"][deck_serial_number] = path
        self.settings_manager.save_settings_to_file(self.PAGE_SETTINGS_PATH, page_settings)

    def get_all_default_page_serial_numbers(self) -> list[str]:
        serial_numbers = []

        page_settings = self.settings_manager.load_settings_from_file(self.PAGE_SETTINGS_PATH)
        for serial_number, page_path in page_settings.get("default-pages", {}).items():
            if not page_path:
                continue
            serial_numbers.append(serial_number)

        return serial_numbers

    def get_serial_numbers_from_page(self, path: str) -> list[str]:
        serial_numbers = []

        page_settings = self.settings_manager.load_settings_from_file(self.PAGE_SETTINGS_PATH)
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
        shutil.copy2(old_path, new_path)

        page_settings = gl.settings_manager.load_settings_from_file(self.PAGE_SETTINGS_PATH)
        default_pages = page_settings.get("default-pages", {})

        # Update Path in Objects
        for controller in gl.deck_manager.deck_controller:
            if controller.active_page is None:
                continue

            page = self.get_page(old_path, controller)

            if not page:
                continue

            page.json_path = new_path

        # Update path in Settings file
        for serial_number, path in default_pages.items():
            if path != old_path:
                continue
            default_pages[serial_number] = new_path

        # Save updated default pages
        page_settings["default-pages"] = default_pages
        gl.settings_manager.save_settings_to_file(self.PAGE_SETTINGS_PATH, page_settings)

        os.remove(old_path)
        #self.update_auto_change_info()

    def remove_page(self, page_path: str):
        settings_path = os.path.join(gl.DATA_PATH, "settings", "pages.json")
        settings = gl.settings_manager.load_settings_from_file(settings_path)
        default_pages = settings.get("default-pages", {})

        # Iterate over all deck controllers to handle any that are using the page to be removed
        for controller in gl.deck_manager.deck_controller:
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

            # Remove the page from the created pages cache for this controller
            with self._pages_lock:
                controller_pages = self.pages.get(controller, {})
                entry = controller_pages.pop(page_path, None)
            if entry is not None:
                # Unlike eviction, this teardown needs NO _page_is_live guard
                # (issue #4): it can never gut a live page. The cache is keyed
                # per-controller with a distinct Page object per (controller,
                # path), so popping THIS controller's entry can't touch
                # another controller's live page. For this controller, we only
                # reach here when its active_page was on page_path (guard at
                # the top of the loop) and load_page() above already switched
                # active_page onto new_page at a DIFFERENT path (the default/
                # fallback both exclude page_path) -- so entry["page"] is not
                # active_page. And a controller showing its screensaver is
                # skipped by that same top guard (its active_page is the
                # screensaver page, path mismatch), so entry["page"] is never
                # its _screensaver_pending_page either.
                #
                # Outside the lock: clear_action_objects() may run plugin
                # hooks (D1), which must not stall a concurrent close()/
                # get_page() waiting on _pages_lock from another thread.
                entry["page"].clear_action_objects()
                # Remove the controller entry entirely if it no longer has cached pages
                if not controller_pages:
                    with self._pages_lock:
                        self.pages.pop(controller, None)

        # Delete the JSON file representing the page
        if os.path.exists(page_path):
            os.remove(page_path)

        # Remove any references to this page in the default-pages setting
        new_default_pages = {}
        for serial, path in default_pages.items():
            if path != page_path:
                new_default_pages[serial] = path

        settings["default-pages"] = new_default_pages
        gl.settings_manager.save_settings_to_file(settings_path, settings)

        #self.update_auto_change_info()

    def add_page(self, page_name: str, page_dict: dict = None) -> str:
        page_dict = page_dict or {}

        # The app creates the pages dir at startup, but callers before/
        # outside that init (tests, future code paths) must not crash here.
        os.makedirs(self.PAGE_PATH, exist_ok=True)

        path = os.path.join(self.PAGE_PATH, f"{page_name}.json")
        if os.path.exists(path):
            raise FileExistsError(f"A page with the name '{page_name}' already exists.")

        atomic_write_json(path, page_dict)

        #self.update_auto_change_info()
        return path

    def register_page(self, path: str):
        if not os.path.isfile(path):
            log.error(f"Page {path} does not exist")
            return

        log.trace(f"Registering page {path}")
        self.custom_pages.append(path)

        gl.signal_manager.trigger_signal(Signals.PageAdd, path)

        # self.update_auto_change_info()

    def unregister_page(self, path: str):
        if not self.custom_pages.__contains__(path):
            return

        self.custom_pages.remove(path)
        gl.signal_manager.trigger_signal(Signals.PageDelete, path)

    def get_pages_with_path(self, path: str):
        pages_set = set()

        # Reads of self.pages must hold _pages_lock: discard_controller()
        # pops whole controller entries from another thread, so the
        # unlocked membership-check/lookup pair here raced it into a
        # KeyError. Lookups only -- no plugin hooks run under the lock.
        with self._pages_lock:
            for controller in gl.deck_manager.deck_controller:
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
        for controller in gl.deck_manager.deck_controller:
            controller.load_page(controller.active_page, allow_reload=True)

    def update_dict_of_pages_with_path(self, path: str) -> None:
        # Re-reading from disk can't lose unsaved in-memory edits: every
        # in-tree Page.dict mutator persists via save() in the same call.
        pages = self.get_pages_with_path(path)
        for page in pages:
            page.update_dict()

    def get_page_data(self, path: str, use_backup: bool = True) -> dict:
        """
        Loads the whole page settings and returns the dictionary.
        :param path: Path to the settings file.
        :param use_backup: Whether to use a backup file or not.
        :return: The dict containing the page settings.
        """
        if path is None:
            return {}

        backup_path = os.path.join(self.PAGE_PATH, "backups", os.path.basename(path))

        # Missing primary: substitute the backup (only when use_backup).
        if not os.path.exists(path) and os.path.exists(backup_path) and use_backup:
            path = backup_path

        data, corrupt = self.settings_manager.load_settings_reporting_corruption(path)

        # Corrupt primary: heal from the backup REGARDLESS of use_backup and
        # regardless of whether the loader could move the primary aside. This
        # is the crux of #32: every page-settings mutator reads via
        # get_page_data(path, False) and then writes the result straight back
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
        self.settings_manager.save_settings_to_file(path, data)
        self.update_dict_of_pages_with_path(path)
        if any([reload_brightness, reload_screensaver, reload_background, reload_inputs]):
            self.reload_pages_with_path(path,
                                        brightness=reload_brightness,
                                        screensaver=reload_screensaver,
                                        background=reload_background,
                                        inputs=reload_inputs)

    def remove_asset_from_all_pages(self, path: str):
        # Validate input path; reject empty or None
        if not path:
            raise ValueError("Invalid path")

        # Compute absolute path once for comparison
        abs_target_path = os.path.abspath(path)

        # Iterate over all page files (paths)
        for page_path in self.get_pages():
            page_had_asset = False  # Flag to track if this page had the asset

            # Open and load JSON page data. Via the settings loader so one
            # corrupt page (loads {}) skips instead of raising and aborting
            # the sweep for every remaining page.
            #
            # NOTE: the loader quarantines a corrupt file as a side effect of
            # ANY read -- so this read-oriented sweep may move a corrupt page
            # aside (to <path>.corrupt) even though it changes nothing about
            # that page's assets. That is non-destructive: the sidecar keeps
            # the corrupt bytes, the last good copy survives in pages/backups/
            # (the next get_page_data heals from it), and page_had_asset stays
            # False here so nothing is written back over the poison page.
            page_dict = self.settings_manager.load_settings_from_file(page_path)

            # Safely get keys dictionary from page data
            keys = page_dict.get("keys", {})

            # Iterate over each key and its data
            for key, key_data in keys.items():
                # Get all states for this key
                states = key_data.get("states", {})

                # Iterate through each state and its data
                for state, state_data in states.items():
                    # Get media dictionary from state data
                    media = state_data.get("media", {})
                    dict_path = media.get("path")

                    # If no media path defined, skip
                    if dict_path is None:
                        continue

                    # Compare absolute paths; if match, remove asset reference
                    if os.path.abspath(dict_path) == abs_target_path:
                        page_had_asset = True
                        state_data["media"]["path"] = None  # Remove the asset path

            # If any asset was removed, update page file and reload pages
            if page_had_asset:
                # Write updated page data back to file (atomically)
                atomic_write_json(page_path, page_dict)

                # Update internal cache or tracking dict with this page path
                self.update_dict_of_pages_with_path(page_path)

                # Reload any loaded Page objects corresponding to this file
                pages = self.get_pages_with_path(page_path)
                for page in pages:
                    # Reload the page if it is currently active on its controller
                    if page.deck_controller.active_page == page:
                        page.deck_controller.load_page(page, allow_reload=True)

    def find_matching_page_path(self, name: str) -> str:
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

    def set_page_settings(self, path: str, settings: dict):
        """
        Sets the whole settings section of the page json
        :param path: Path to the file
        :param settings: Settings dictionary to write into settings section of the file.
        :return: None
        """
        if path is None:
            return

        data = self.get_page_data(path, False)
        data["settings"] = settings
        self.settings_manager.save_settings_to_file(path, data)

        # Refresh EVERY cached Page object for this path, not just the ones
        # active on a controller (same mechanism set_page_data uses). A
        # cached-but-not-active Page kept its pre-edit dict here, and the
        # next Page.save() from any trigger (plugin set_settings, key/state
        # edits, ...) rewrote the file from that stale dict -- silently
        # erasing the just-saved settings section (auto-change, screensaver,
        # brightness, background overrides). See #113/#104.
        self.update_dict_of_pages_with_path(path)

    def get_auto_change_settings(self, path: str) -> dict:
        """
        Returns the auto change settings section of the page settings
        :param path: Path to the file
        :return: dict
        """
        page_settings = self.get_page_settings(path)
        return page_settings.get("auto-change", {})

    def set_auto_change_settings(self, path: str, enable: bool = False, wm_class: str = "", regex_title: str = "", stay_on_page: bool = False, decks: list[str] = None):
        settings = self.get_page_settings(path)

        decks = decks or []

        settings["auto-change"] = {
            "enable": enable,
            "wm-class": wm_class,
            "title": regex_title,
            "stay-on-page": stay_on_page,
            "decks": decks
        }

        self.set_page_settings(path, settings)

    def overwrite_auto_change_settings(self, path: str, enable: bool = None, wm_class: str = None, regex_title: str = None, stay_on_page: bool = None, decks: list[str] = None):
        settings = self.get_page_settings(path)
        auto_change_settings = settings.get("auto-change", {})

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

        settings["auto-change"] = auto_change_settings
        self.set_page_settings(path, settings)

    def get_screensaver_settings(self, path: str):
        page_settings = self.get_page_settings(path)
        return page_settings.get("screensaver", {})

    def set_screensaver_settings(self, path: str, overwrite: bool = False, enable: bool = False, time_delay: int = 5, loop: bool = False, fps: int = 30, brightness: float = 75, media_path: str = ""):
        settings = self.get_page_settings(path)

        settings["screensaver"] = {
            "overwrite": overwrite,
            "enable": enable,
            "time-delay": time_delay,
            "loop": loop,
            "fps": fps,
            "brightness": brightness,
            "media-path": media_path
        }

        self.set_page_settings(path, settings)

    def overwrite_screensaver_settings(self, path: str, overwrite: bool = None, enable: bool = None, time_delay: int = None, loop: bool = None, fps: int = None, brightness: float = None, media_path: str = None):
        settings = self.get_page_settings(path)
        screensaver_settings = settings.get("screensaver", {})

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

        settings["screensaver"] = screensaver_settings
        self.set_page_settings(path, settings)

    def get_brightness_settings(self, path: str):
        page_settings = self.get_page_settings(path)
        return page_settings.get("brightness", {})

    def set_brightness_settings(self, path: str, overwrite: bool = False, brightness: float = 75):
        settings = self.get_page_settings(path)

        settings["brightness"] = {
            "overwrite": overwrite,
            "value": brightness
        }

        self.set_page_settings(path, settings)

    def overwrite_brightness_settings(self, path: str, overwrite: bool = None, brightness: float = None):
        settings = self.get_page_settings(path)
        brightness_settings = settings.get("brightness", {})

        if overwrite is not None:
            brightness_settings["overwrite"] = overwrite
        if brightness is not None:
            brightness_settings["value"] = brightness

        settings["brightness"] = brightness_settings
        self.set_page_settings(path, settings)

    def get_background_settings(self, path: str):
        page_settings = self.get_page_settings(path)
        return page_settings.get("background", {})

    def set_background_settings(self, path: str, overwrite: bool = False, show: bool = False, fps: int = 30, loop: bool = False, media_path: str = "", extend_to_touchscreen: bool = False):
        settings = self.get_page_settings(path)

        settings["background"] = {
            "overwrite": overwrite,
            "show": show,
            "fps": fps,
            "loop": loop,
            "media-path": media_path,
            "extend-to-touchscreen": extend_to_touchscreen
        }

        self.set_page_settings(path, settings)

    def overwrite_background_settings(self, path: str, overwrite: bool = None, show: bool = None, fps: int = None, loop: bool = None, media_path: str = None, extend_to_touchscreen: bool = None):
        settings = self.get_page_settings(path)
        background_settings = settings.get("background", {})

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

        settings["background"] = background_settings
        self.set_page_settings(path, settings)
