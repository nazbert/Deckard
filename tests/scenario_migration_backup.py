"""
Regression scenario for Migrator.create_backup.

create_backup names each archive after the migrator's own version, so a
chained upgrade keeps one archive per migrator. It archives settings/plugins/
as well as pages/, so a failed plugin-settings migration stays recoverable.
"""
import json
import os
import shutil
import zipfile

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import globals as gl

from src.backend.Migration.MigrationManager import MigrationManager
from src.backend.Migration.Migrators.Migrator_1_5_0 import Migrator_1_5_0
from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

BACKUPS_DIR = os.path.join(gl.DATA_PATH, "backups")
PLUGIN_SETTINGS_REL = os.path.join("plugins", "com_backup_Test", "settings.json")


def _reset() -> None:
    for sub in ("pages", "backups", os.path.join("settings", "plugins")):
        shutil.rmtree(os.path.join(gl.DATA_PATH, sub), ignore_errors=True)
    migrations_json = os.path.join(gl.DATA_PATH, "settings", "migrations.json")
    if os.path.exists(migrations_json):
        os.remove(migrations_json)


def check_chained_backups_per_migrator() -> None:
    _reset()

    os.makedirs(os.path.join(gl.DATA_PATH, "pages"), exist_ok=True)
    with open(os.path.join(gl.DATA_PATH, "pages", "P.json"), "w") as f:
        json.dump({"keys": {}}, f, indent=4)

    ps_dir = os.path.join(gl.DATA_PATH, "settings", "plugins", "com_backup_Test")
    os.makedirs(ps_dir, exist_ok=True)
    with open(os.path.join(ps_dir, "settings.json"), "w") as f:
        json.dump({"secret": "back-me-up"}, f, indent=4)

    original_app_version = gl.app_version
    gl.app_version = "1.5.0"  # both migrators arm
    try:
        manager = MigrationManager()
        manager.add_migrator(Migrator_1_5_0())
        manager.add_migrator(Migrator_1_5_0_beta_5())
        manager.run_migrators()
    finally:
        gl.app_version = original_app_version

    backups = sorted(os.listdir(BACKUPS_DIR))
    # One distinct archive per migrator version.
    assert "before_1.5.0-beta.5_migration.zip" in backups, (
        f"beta.5 backup missing/clobbered: {backups}"
    )
    assert "before_1.5.0_migration.zip" in backups, f"1.5.0 backup missing: {backups}"
    assert len(backups) == 2, (
        f"expected 2 distinctly-named backups (one per migrator), got {backups} -- "
        "app-version-keyed names would collapse to one"
    )

    # Plugin settings must be recoverable from a backup archive.
    found = False
    for name in backups:
        with zipfile.ZipFile(os.path.join(BACKUPS_DIR, name)) as z:
            if any(PLUGIN_SETTINGS_REL.replace(os.sep, "/") in n for n in z.namelist()):
                found = True
    assert found, (
        "plugin settings.json is in no backup archive -- beta_5 deletes it "
        "with no recovery path if the migration fails"
    )
    print("PASS: chained backups are per-migrator and include plugin settings")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_migration_backup")
    check_chained_backups_per_migrator()
    print("PASS: scenario_migration_backup")


if __name__ == "__main__":
    main()
