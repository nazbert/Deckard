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
        """
        SettingsManager is not yet loaded when this is called
        """
        if not os.path.exists(self.SETTINGS_DIR):
            return {}
        try:
            with open(self.SETTINGS_DIR, "r") as f:
                return json.load(f)
        except ValueError as e:
            # ValueError, not JSONDecodeError: a file of garbage bytes raises
            # UnicodeDecodeError (a ValueError, not a JSON error) while
            # decoding, which used to escape this handler and abort startup --
            # exactly what the comment below claims was fixed. json's own
            # JSONDecodeError is a ValueError subclass, so one clause covers
            # both (StoreBackend.py:945 is the in-repo precedent).
            #
            # A torn migrations.json used to abort startup here (raised
            # straight out of run_migrators). Quarantine and treat as "no
            # migrations recorded": re-running the migrators is safe --
            # beta_5 never deletes-without-write and leaves existing targets
            # alone, 1_5_0's walker is idempotent, and create_backup() runs
            # before any destructive work. A prior .corrupt is never
            # clobbered (shared helper picks the first free sidecar name).
            moved, dest = quarantine_corrupt_file(self.SETTINGS_DIR)
            if moved:
                log.error(
                    f"Could not read {self.SETTINGS_DIR} ({e}) -- preserved at "
                    f"{dest}, treating all migrations as pending"
                )
                # Bounded retention for this one file's sidecars. Safe
                # this early: atomic_json is stdlib-only by design precisely so
                # the migrators (which run before SettingsManager exists) can
                # use it, and this module already imports from it.
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
            # Unreadable is not corrupt. Quarantining here would rename a
            # perfectly healthy migrations.json away over a transient EACCES/
            # EIO and re-run every migrator against a state file that now
            # claims nothing has run. Report pending (which is what an
            # unreadable state file means) and leave the file alone.
            log.error(
                f"Could not read {self.SETTINGS_DIR} ({e}) -- leaving it in place "
                f"(unreadable, not corrupt), treating all migrations as pending"
            )
            return {}
        
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
