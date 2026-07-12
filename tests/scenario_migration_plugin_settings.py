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
      still cleaned up;
  (c) crash-safety (M2, MR !11 review): the new file is written atomically
      (temp + fsync + os.replace), so process death between writing the new
      file and removing the old one cannot leave a truncated settings.json.
      Simulated by killing a child at fsync (os._exit); the old file must
      survive intact for the re-run. Red-tests against a plain
      open('w')+dump write;
  (d) partial-crash re-run idempotency: set_migrated fires once at the end of
      migrate(), so a crash after plugin A is migrated but before B re-runs
      the WHOLE migrate_plugin_settings -- it must finish B without clobbering
      the already-migrated A.
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


def check_partial_crash_rerun_is_idempotent() -> None:
    """(d) set_migrated(True) fires once at the end of migrate(), so a crash
    between plugins re-runs the whole migrate_plugin_settings. Model the state
    after 'A migrated, B not yet': A only at the new path (old gone), B still
    at the old path. Re-running must migrate B and leave A untouched."""
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


def check_atomic_write_survives_death_before_replace() -> None:
    """(c) Crash between writing the new file and removing the old: with the
    atomic temp+fsync+os.replace write, a death at fsync leaves the target
    either absent or complete -- never truncated -- and the OLD file must still
    be present for the re-run (os.remove is reached only after os.replace).
    Runs in a child that dies at fsync via os._exit(9)."""
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
        # The death was AT fsync, before os.replace, so the target must NOT yet
        # exist (the write never committed) -- it is definitely not truncated.
        assert not os.path.exists(new), (
            "new settings path exists though the write died before os.replace -- "
            "the write was not atomic"
        )
        # An orphaned .migrate-*.tmp under the target dir is the EXPECTED residue
        # of a pre-replace death (the temp file itself, never renamed). It is a
        # distinct name, so it never masquerades as the live settings.json. The
        # migrator runs once and lacks a temp-reaper (unlike MR !9's helper);
        # this is a known, tolerated trade-off documented in the source comment.
        import glob
        orphans = glob.glob(os.path.join(os.path.dirname(new), ".migrate-*.tmp"))
        for orphan in orphans:
            assert os.path.basename(orphan) != "settings.json"
    finally:
        # data_path IS the child's isolated temp data dir (fixtures mkdtemp);
        # the child died via os._exit so its atexit cleanup never ran.
        shutil.rmtree(data_path, ignore_errors=True)
    print("PASS: atomic write keeps old settings intact through mid-write death")


def main() -> None:
    check_settings_survive_fresh_migration()
    check_existing_settings_not_clobbered()
    check_partial_crash_rerun_is_idempotent()
    check_atomic_write_survives_death_before_replace()
    print("PASS: scenario_migration_plugin_settings")


if __name__ == "__main__":
    main()
