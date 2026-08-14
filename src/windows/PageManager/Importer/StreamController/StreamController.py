import os
import json

from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.backend.PageManagement import page_flush
from src.backend.atomic_json import atomic_write_json

from loguru import logger as log

import globals as gl

from gi.repository import GLib

class StreamControllerImporter:
    def __init__(self, json_export_path: str):
        self.json_export_path = json_export_path

    
    def save_json(self, json_path: str, data: dict, _retries: int = 3):
        atomic_write_json(json_path, data)

        loaded = None
        try:
            with open(json_path) as f:
                loaded = json.load(f)
        except Exception as e:
            pass

        if loaded != data:
            if _retries > 0:
                log.error(f"Failed to save {json_path}, trying again ({_retries} retries left)")
                self.save_json(json_path, data, _retries=_retries - 1)
            else:
                log.error(f"Failed to save {json_path} after all retries, giving up")
            
    def perform_import(self):
        with open(self.json_export_path) as f:
            self.export = json.load(f)

        for page_name in self.export:
            page = self.export[page_name]
            page_path = os.path.join(gl.DATA_PATH, "pages", f"{page_name}.json")
            if ".json.json" in page_path:
                page_path = page_path.replace(".json.json", ".json")

            # An import replaces a whole page, so a write that still waits
            # for that path holds a version that the user discarded, and it
            # lands after this one and undoes the import. Drop it instead of
            # flushing it, and scope the drop to the page path, because
            # save_json also writes deck settings, which the flush seam does
            # not cover.
            page_flush.get().discard_path(page_path)

            self.save_json(page_path, page)

            gl.page_manager.refresh_document(page_path)
            gl.page_manager.reload_pages_with_path(page_path)

            log.success(f"Imported page {page_name}")

        log.success("Imported all pages from StreamController")

        # These pages go to disk whole, past every page-settings setter, so
        # an import that carries enabled window auto-change rules is the one
        # way such rules appear unnoticed. Without this call the watcher stays
        # off for the rest of the session and the imported rules do nothing.
        if gl.page_manager is not None:
            gl.page_manager.refresh_window_watch_state()

        if recursive_hasattr(gl, "app.main_win.sidebar.page_selector"):
            GLib.idle_add(gl.app.main_win.sidebar.page_selector.update)
        if recursive_hasattr(gl, "page_manager_window.page_selector"):
            GLib.idle_add(gl.page_manager_window.page_selector.load_pages)
        log.success("Updated ui")