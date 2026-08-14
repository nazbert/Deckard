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
import json
import shutil
import tempfile
import globals as gl
import os
from packaging import version
from loguru import logger as log

from src.backend.atomic_json import atomic_write_json, prune_corrupt_sidecars, quarantine_corrupt_file

class Migrator:
    SETTINGS_DIR = os.path.join(gl.DATA_PATH, "settings", "migrations.json")
    def __init__(self, app_version: str):
        self.app_version = app_version
        self.parsed_app_version = version.parse(app_version)

    def get_need_migration(self) -> bool:
        app_version = version.parse(gl.app_version)
        migrator_version = self.parsed_app_version
        if app_version < migrator_version:
            return False

        settings = self.get_settings()
        return not settings.get(self.app_version, False)
    
    def set_migrated(self, migrated: bool) -> None:
        settings = self.get_settings()
        settings[self.app_version] = migrated
        self.set_settings(settings)

    def get_settings(self) -> dict:
        """SettingsManager does not exist yet when a caller reaches this."""
        if not os.path.exists(self.SETTINGS_DIR):
            return {}
        try:
            with open(self.SETTINGS_DIR, "r") as f:
                return json.load(f)
        except ValueError as e:
            # Catch ValueError. A file of garbage bytes raises
            # UnicodeDecodeError while the reader decodes it, and json raises
            # JSONDecodeError. Both derive from ValueError.
            #
            # Quarantine the file and report every migration as pending. A
            # re-run is safe. beta_5 writes before it deletes and leaves an
            # existing target alone, the 1_5_0 walker is idempotent, and
            # create_backup() runs before any destructive work.
            moved, dest = quarantine_corrupt_file(self.SETTINGS_DIR)
            if moved:
                log.error(
                    f"Could not read {self.SETTINGS_DIR} ({e}) -- preserved at "
                    f"{dest}, treating all migrations as pending"
                )
                # Bound the sidecar count for this file. atomic_json imports
                # stdlib only, so the migrators can call it before
                # SettingsManager exists.
                for pruned in prune_corrupt_sidecars(self.SETTINGS_DIR, protect=dest):
                    log.info(f"Pruned old quarantined copy {pruned}")
            else:
                log.error(
                    f"Could not read {self.SETTINGS_DIR} ({e}) -- it was NOT moved aside "
                    f"here (rename failed, or another reader quarantined it first), "
                    f"treating all migrations as pending"
                )
            return {}
        except OSError as e:
            # Unreadable is not corrupt. A rename here moves a healthy
            # migrations.json aside over a transient EACCES or EIO, and every
            # migrator then re-runs against an empty state file. Report every
            # migration as pending and leave the file alone.
            log.error(
                f"Could not read {self.SETTINGS_DIR} ({e}) -- leaving it in place "
                f"(unreadable, not corrupt), treating all migrations as pending"
            )
            return {}
        
    def set_settings(self, settings: dict) -> None:
        """SettingsManager does not exist yet when a caller reaches this."""
        atomic_write_json(self.SETTINGS_DIR, settings)

    def create_backup(self) -> None:
        # Back up every tree a migrator can rewrite or delete, which are
        # pages/ and settings/plugins/. Migrator_1_5_0_beta_5 moves and then deletes each
        # plugin's settings.json, so a pages-only backup gives no recovery
        # path. A fresh install has neither tree and needs no backup.
        pages_path = os.path.join(gl.DATA_PATH, "pages")
        plugin_settings_path = os.path.join(gl.DATA_PATH, "settings", "plugins")
        sources = [p for p in (pages_path, plugin_settings_path) if os.path.exists(p)]
        if not sources:
            return

        backup_path = os.path.join(gl.DATA_PATH, "backups")
        os.makedirs(backup_path, exist_ok=True)

        # Name the archive after this migrator's own version, not
        # gl.app_version. A chained upgrade runs several migrators in one
        # session and they all share gl.app_version, so each backup would
        # overwrite the previous one. self.app_version is unique per migrator.
        safe_version = self.app_version.replace(os.sep, "_")
        with tempfile.TemporaryDirectory() as staging:
            for src in sources:
                # pages/ -> <staging>/pages, settings/plugins/ -> <staging>/plugins
                shutil.copytree(src, os.path.join(staging, os.path.basename(src)))

            log.info(f"Creating backup to {backup_path}")
            path = shutil.make_archive(
                base_name=os.path.join(backup_path, f"before_{safe_version}_migration"),
                format="zip",
                root_dir=staging,
            )
        log.success(f"Saved backup to {path}")
