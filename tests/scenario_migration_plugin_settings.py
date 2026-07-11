"""
Regression scenario for gl#30: Migrator_1_5_0_beta_5.migrate_plugin_settings
had an inverted existence check -- in the normal case (new settings path does
not exist yet) nothing was written before `os.remove(old_settings_path)`, so
the migration permanently deleted every plugin's settings.json; and when the
new path DID already exist, its current content was clobbered with the stale
pre-beta.5 copy.

Covers:
  (a) fresh migration (new path absent): the old plugin settings survive at
      the new location, byte-for-byte content intact, and only then is the
      old file removed -- never delete-without-write;
  (b) re-run against an already-migrated install (new path present, e.g.
      after a lost migrations.json): the current settings at the new path
      are NOT clobbered with the stale old copy; the stale old file is
      still cleaned up.
"""
import json
import os
import shutil

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import globals as gl

from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

PLUGIN_NAME = "com_example_TestPlugin"


def _old_settings_path() -> str:
    return os.path.join(gl.PLUGIN_DIR, PLUGIN_NAME, "settings.json")


def _new_settings_path() -> str:
    return os.path.join(gl.DATA_PATH, "settings", "plugins", PLUGIN_NAME, "settings.json")


def _reset() -> None:
    shutil.rmtree(os.path.join(gl.PLUGIN_DIR, PLUGIN_NAME), ignore_errors=True)
    shutil.rmtree(os.path.dirname(_new_settings_path()), ignore_errors=True)


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


def _read_json(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def check_settings_survive_fresh_migration() -> None:
    """(a) The normal upgrade case: new path absent. The old bug wrote
    nothing and then deleted the old file -- settings gone forever."""
    _reset()
    old_settings = {"api-token": "keep-me", "nested": {"list": [1, 2, 3]}}
    _write_json(_old_settings_path(), old_settings)
    assert not os.path.exists(_new_settings_path())

    Migrator_1_5_0_beta_5().migrate_plugin_settings()

    assert os.path.exists(_new_settings_path()), (
        "plugin settings were NOT written to the new location -- with the old "
        "file removed below, they would be permanently lost (gl#30)"
    )
    assert _read_json(_new_settings_path()) == old_settings, (
        "migrated settings content differs from the original"
    )
    assert not os.path.exists(_old_settings_path()), (
        "old settings file should be removed once a copy exists at the new path"
    )
    print("PASS: fresh migration moves plugin settings to the new path intact")


def check_existing_settings_not_clobbered() -> None:
    """(b) New path already populated (already-migrated install re-running
    the migrator): the current settings must win over the stale old copy."""
    _reset()
    _write_json(_old_settings_path(), {"api-token": "stale-pre-beta5"})
    current_settings = {"api-token": "current", "added-after-migration": True}
    _write_json(_new_settings_path(), current_settings)

    Migrator_1_5_0_beta_5().migrate_plugin_settings()

    assert _read_json(_new_settings_path()) == current_settings, (
        "current settings at the new path were clobbered with the stale "
        "pre-beta.5 copy (gl#30)"
    )
    assert not os.path.exists(_old_settings_path()), (
        "stale old settings file should still be cleaned up"
    )
    print("PASS: existing settings at the new path are not clobbered")


def main() -> None:
    check_settings_survive_fresh_migration()
    check_existing_settings_not_clobbered()
    print("PASS: scenario_migration_plugin_settings")


if __name__ == "__main__":
    main()
