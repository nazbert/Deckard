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

from src.backend.atomic_json import atomic_write_json

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
        """
        SettingsManager is not yet loaded when this is called
        """
        if not os.path.exists(self.SETTINGS_DIR):
            return {}
        with open(self.SETTINGS_DIR, "r") as f:
            return json.load(f)
        
    def set_settings(self, settings: dict) -> None:
        """
        SettingsManager is not yet loaded when this is called
        """
        atomic_write_json(self.SETTINGS_DIR, settings)

    def create_backup(self) -> None:
        # Back up everything a migrator may destructively rewrite/delete:
        # pages/ AND settings/plugins/ (Migrator_1_5_0_beta_5 moves-then-deletes
        # each plugin's settings.json -- pages/ alone left that with no recovery
        # path). Nothing to back up if neither exists yet (fresh install).
        pages_path = os.path.join(gl.DATA_PATH, "pages")
        plugin_settings_path = os.path.join(gl.DATA_PATH, "settings", "plugins")
        sources = [p for p in (pages_path, plugin_settings_path) if os.path.exists(p)]
        if not sources:
            return

        backup_path = os.path.join(gl.DATA_PATH, "backups")
        os.makedirs(backup_path, exist_ok=True)

        # Namespace the archive by the MIGRATOR's own version, not gl.app_version:
        # a chained upgrade runs several migrators in one session and they all
        # share gl.app_version, so keying on it made each migrator's backup
        # overwrite the previous one's. self.app_version is unique per migrator.
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
