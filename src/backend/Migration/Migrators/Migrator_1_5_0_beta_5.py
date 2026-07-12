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
import json
import os
import tempfile

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

            with open(page_path, "w") as f:
                json.dump(page, f, indent=4)

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
            # INVARIANT (gl#30): write the migrated copy to the new path FIRST,
            # and only remove the old file once that copy is durably in place.
            # NEVER the inverted exists-check main had -- gating the write on
            # the new path already existing meant the normal case (new path
            # absent) wrote nothing and then os.remove'd the old, deleting the
            # settings forever. If the new path already exists it holds the
            # CURRENT settings; leave it untouched rather than clobbering it
            # with the stale pre-beta.5 copy.
            if not os.path.exists(new_settings_path):
                os.makedirs(os.path.dirname(new_settings_path), exist_ok=True)
                # Crash-safe write: a plain open('w')+dump truncated in place on
                # a mid-write crash, and with the old file removed just below
                # the settings would be gone. Write to a same-dir temp, fsync,
                # os.replace (atomic on POSIX) -- so a crash leaves either the
                # old file intact (temp discarded) or the complete new file.
                # NOTE: duplicates src/backend/atomic_json.py::atomic_write_json
                # from MR !9; kept self-contained so !11 runs standalone before
                # !9 merges -- de-dupe to that helper once !9 lands.
                self._atomic_write_json(new_settings_path, settings)

            # Remove old settings -- a complete copy now exists at the new path.
            os.remove(old_settings_path)

    @staticmethod
    def _atomic_write_json(path: str, data) -> None:
        dir_name = os.path.dirname(path)
        fd, tmp_path = tempfile.mkstemp(prefix=".migrate-", suffix=".tmp", dir=dir_name)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
