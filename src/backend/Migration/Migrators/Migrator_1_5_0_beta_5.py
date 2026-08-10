"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from src.backend.Migration.Migrator import Migrator
from src.backend.atomic_json import atomic_write_json
import json
import os

import globals as gl

class Migrator_1_5_0_beta_5(Migrator):
    def __init__(self):
        super().__init__("1.5.0-beta.5")
        
    def migrate(self):
        self.migrate_pages()
        self.migrate_plugin_settings()

        self.set_migrated(True)

    def migrate_pages(self):
        pages_dir = os.path.join(gl.DATA_PATH, "pages")
        if not os.path.exists(pages_dir):
            return
        
        for page_path in os.listdir(pages_dir):
            if not page_path.endswith(".json"):
                continue
            page_path = os.path.join(pages_dir, page_path)
            with open(page_path, "r") as f:
                page = json.load(f)

            for key in page.get("keys", {}):
                if "states" in page["keys"][key]:
                    continue

                key_dict = page["keys"][key].copy()
                page["keys"][key].clear()

                page["keys"][key]["states"] = {}
                page["keys"][key]["states"]["0"] = key_dict

                page["keys"][key]["states"]["0"].setdefault("image-control-action", 0)
                page["keys"][key]["states"]["0"].setdefault("label-control-actions", [0, 0, 0])

            atomic_write_json(page_path, page)

    def migrate_plugin_settings(self):
        if not os.path.exists(gl.PLUGIN_DIR):
            return
        for plugin_dir_name in os.listdir(gl.PLUGIN_DIR):
            old_settings_path = os.path.join(gl.PLUGIN_DIR, plugin_dir_name, "settings.json")
            if not os.path.exists(old_settings_path):
                continue
            try:
                with open(old_settings_path, "r") as f:
                    settings = json.load(f)
            except Exception as e:
                continue

            new_settings_path = os.path.join(gl.DATA_PATH, "settings", "plugins", plugin_dir_name, "settings.json")
            # INVARIANT: write the migrated copy to the new path FIRST,
            # and only remove the old file once that copy is durably in place.
            # NEVER the inverted exists-check main had -- gating the write on
            # the new path already existing meant the normal case (new path
            # absent) wrote nothing and then os.remove'd the old, deleting the
            # settings forever. If the new path already exists it holds the
            # CURRENT settings; leave it untouched rather than clobbering it
            # with the stale pre-beta.5 copy.
            if not os.path.exists(new_settings_path):
                # Crash-safe write: a plain open('w')+dump truncates in place on
                # a mid-write crash, and with the old file removed just below
                # the settings would be gone. atomic_write_json commits via a
                # same-dir temp + fsync + os.replace, so a crash leaves either
                # the old file intact (temp discarded, never renamed) or the
                # complete new file. It creates the parent directory itself.
                # A write failure propagates, which is what keeps os.remove
                # below unreachable until the copy is durably in place.
                atomic_write_json(new_settings_path, settings)

            # Remove old settings -- a complete copy now exists at the new path.
            os.remove(old_settings_path)
