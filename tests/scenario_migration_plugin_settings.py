"""
Regression scenario for Migrator_1_5_0_beta_5.migrate_plugin_settings.

The migrator writes each plugin's settings.json to the new path before it
removes the old file, writes atomically, and never clobbers settings that
already sit at the new path. A re-run after a crash finishes the remainder.
"""
import json
import os
import shutil
import subprocess
import sys

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
    """The normal upgrade case, with the new path absent. A delete without a
    write loses the settings."""
    _reset()
    old_settings = {"api-token": "keep-me", "nested": {"list": [1, 2, 3]}}
    _write_json(_old_settings_path(), old_settings)
    assert not os.path.exists(_new_settings_path())

    Migrator_1_5_0_beta_5().migrate_plugin_settings()

    assert os.path.exists(_new_settings_path()), (
        "plugin settings were NOT written to the new location -- with the old "
        "file removed below, they would be permanently lost"
    )
    assert _read_json(_new_settings_path()) == old_settings, (
        "migrated settings content differs from the original"
    )
    assert not os.path.exists(_old_settings_path()), (
        "old settings file should be removed once a copy exists at the new path"
    )
    print("PASS: fresh migration moves plugin settings to the new path intact")


def check_existing_settings_not_clobbered() -> None:
    """The new path already holds settings. The current settings must win
    over the stale old copy."""
    _reset()
    _write_json(_old_settings_path(), {"api-token": "stale-pre-beta5"})
    current_settings = {"api-token": "current", "added-after-migration": True}
    _write_json(_new_settings_path(), current_settings)

    Migrator_1_5_0_beta_5().migrate_plugin_settings()

    assert _read_json(_new_settings_path()) == current_settings, (
        "current settings at the new path were clobbered with the stale "
        "pre-beta.5 copy"
    )
    assert not os.path.exists(_old_settings_path()), (
        "stale old settings file should still be cleaned up"
    )
    print("PASS: existing settings at the new path are not clobbered")


def check_partial_crash_rerun_idempotent() -> None:
    """set_migrated fires once at the end of migrate(), so a crash between
    plugins re-runs the whole pass. Plugin A sits at the new path and plugin B
    at the old one. The re-run migrates B and leaves A intact."""
    for name in ("com_example_A", "com_example_B"):
        shutil.rmtree(os.path.join(gl.PLUGIN_DIR, name), ignore_errors=True)
        shutil.rmtree(os.path.join(gl.DATA_PATH, "settings", "plugins", name), ignore_errors=True)

    a_new = os.path.join(gl.DATA_PATH, "settings", "plugins", "com_example_A", "settings.json")
    b_old = os.path.join(gl.PLUGIN_DIR, "com_example_B", "settings.json")
    _write_json(a_new, {"a": "already-migrated"})
    _write_json(b_old, {"b": "pending"})

    Migrator_1_5_0_beta_5().migrate_plugin_settings()

    assert _read_json(a_new) == {"a": "already-migrated"}, (
        "re-run clobbered the already-migrated plugin A"
    )
    b_new = os.path.join(gl.DATA_PATH, "settings", "plugins", "com_example_B", "settings.json")
    assert _read_json(b_new) == {"b": "pending"}, "re-run did not migrate the pending plugin B"
    assert not os.path.exists(b_old), "old B file not cleaned up on re-run"
    print("PASS: partial-crash re-run finishes the remainder without loss")


def check_atomic_write_survives_death() -> None:
    """A death at fsync leaves the target absent or complete, and keeps the
    old file for the re-run; os.remove runs only after os.replace. A child
    process dies at fsync through os._exit(9)."""
    child_code = (
        "import fixtures, os, json\n"
        "import globals as gl\n"
        "from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5\n"
        "name = 'com_example_Killed'\n"
        "old = os.path.join(gl.PLUGIN_DIR, name, 'settings.json')\n"
        "os.makedirs(os.path.dirname(old), exist_ok=True)\n"
        "json.dump({'token': 'keep-me'}, open(old, 'w'))\n"
        "print(gl.DATA_PATH, flush=True)\n"
        "real_fsync = os.fsync\n"
        "def dying_fsync(fd):\n"
        "    real_fsync(fd)\n"
        "    os._exit(9)\n"
        "os.fsync = dying_fsync\n"
        "Migrator_1_5_0_beta_5().migrate_plugin_settings()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 9, (
        f"child should have died at fsync, rc={proc.returncode}: {proc.stderr}"
    )
    data_path = proc.stdout.strip().splitlines()[-1]
    old = os.path.join(data_path, "plugins", "com_example_Killed", "settings.json")
    new = os.path.join(data_path, "settings", "plugins", "com_example_Killed", "settings.json")
    try:
        assert os.path.exists(old), (
            "old settings file was removed before the atomic write completed -- "
            "with a plain open('w') write, mid-write death loses the settings (M2)"
        )
        assert _read_json(old) == {"token": "keep-me"}, "old settings truncated/corrupted"
        # The death happened at fsync, before os.replace, so the target does
        # not exist at all.
        assert not os.path.exists(new), (
            "new settings path exists though the write died before os.replace -- "
            "the write was not atomic"
        )
        # A death before os.replace leaves the temp file behind. Its
        # ".save-<basename>." prefix proves the write went through
        # atomic_write_json. The next atomic_write_json for the same target
        # reaps stale siblings. realpath is needed because atomic_write_json
        # resolves the destination before it picks the temp directory.
        import glob
        target_dir = os.path.realpath(os.path.dirname(new))
        orphans = glob.glob(os.path.join(target_dir, ".save-settings.json.*.tmp"))
        assert orphans, (
            f"no .save-settings.json.*.tmp residue in {target_dir} -- the "
            "migrated write did not go through the shared atomic writer"
        )
    finally:
        # data_path is the child's temp data dir. The child died through
        # os._exit, so its atexit cleanup never ran.
        shutil.rmtree(data_path, ignore_errors=True)
    print("PASS: atomic write keeps old settings intact through mid-write death")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_migration_plugin_settings")
    check_settings_survive_fresh_migration()
    check_existing_settings_not_clobbered()
    check_partial_crash_rerun_idempotent()
    check_atomic_write_survives_death()
    print("PASS: scenario_migration_plugin_settings")


if __name__ == "__main__":
    main()
