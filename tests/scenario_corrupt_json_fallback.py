"""
Scenario: corrupt-but-present JSON must not become silent data loss
(issue #32, read side -- the write side landed with the atomic-write seam).

Four sites, all against the real code:

  1. get_page_data on a corrupt page WITH a backup: pre-fix the loader
     silently returned {} (and the next Page.save() persisted the empty
     dict); post-fix the corrupt file is quarantined to <path>.corrupt and
     the page heals from pages/backups/.
  2. get_page_data on a corrupt page WITHOUT a backup: still {} (nothing to
     heal from), but the corrupt original is preserved aside instead of
     sitting in place waiting to be overwritten by the next save.
  3. Migrator.get_settings on a torn migrations.json: pre-fix the raw
     json.load raised out of run_migrators and aborted startup; post-fix it
     quarantines and reports all migrations pending (safe to re-run since
     #30/#31).
  4. remove_asset_from_all_pages with one poison page: pre-fix the raw
     json.load aborted the sweep for every remaining page; post-fix the
     poison page skips and the healthy page is still cleaned.

Review round 1 (reviewer-fixed HIGH + two MEDIUMs -- the heal is now
loader-owned):

  5. set_page_settings on a corrupt page must NOT gut the live page. The
     settings mutators read via get_page_data(path, use_backup=False) and
     write the result straight back; the old heal only fired on the
     use_backup=True path, so a corrupt page loaded {} and the writer
     persisted {"settings": ...}, erasing keys/background/dials. Post-fix
     the loader reports corruption and get_page_data heals from the backup
     regardless of use_backup, so the healed content AND the new setting
     both survive.
  6. Heal must not depend on the quarantine rename succeeding. If os.replace
     fails (read-only fs / permissions), the corrupt primary stays in place;
     the old heal keyed off "primary no longer exists" and so did nothing,
     and the next save overwrote the corrupt file with {}. Post-fix the heal
     keys off the load-result corrupt flag, so it fires even when the
     quarantine could not move the file aside.
  7. Quarantine must not clobber a prior <path>.corrupt. A second corruption
     used to os.replace over the first forensic copy; post-fix the helper
     picks the first free .corrupt / .corrupt.1 / .corrupt.2 ... slot.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import json
import os
from unittest import mock

import globals as gl
from fixtures import seed_page, start_watchdog


def corrupt(path: str) -> None:
    with open(path, "w") as f:
        f.write('{"keys": {"0x0"')  # truncated mid-token


def check_page_backup_heal() -> int:
    path = seed_page("CorruptWithBackup")
    marker = {"keys": {}, "background": {"marker": "from-backup"}}

    backup_dir = os.path.join(gl.page_manager.PAGE_PATH, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    with open(os.path.join(backup_dir, os.path.basename(path)), "w") as f:
        json.dump(marker, f)

    corrupt(path)
    data = gl.page_manager.get_page_data(path)

    if data.get("background", {}).get("marker") != "from-backup":
        print(f"FAIL(1): corrupt page did not heal from backup, got: {data}")
        return 1
    if not os.path.exists(path + ".corrupt"):
        print("FAIL(1): corrupt original was not preserved at .corrupt")
        return 1
    print("PASS: corrupt page heals from backup; original quarantined")
    return 0


def check_page_no_backup_quarantine() -> int:
    path = seed_page("CorruptNoBackup")
    corrupt(path)

    data = gl.page_manager.get_page_data(path)
    if data != {}:
        print(f"FAIL(2): expected empty dict, got: {data}")
        return 1
    if os.path.exists(path) or not os.path.exists(path + ".corrupt"):
        print("FAIL(2): corrupt page left in place -- the next save would "
              "overwrite the only remaining copy")
        return 1
    print("PASS: backup-less corrupt page quarantined, not left for the next save")
    return 0


def check_migrations_torn() -> int:
    from src.backend.Migration.Migrator import Migrator

    os.makedirs(os.path.dirname(Migrator.SETTINGS_DIR), exist_ok=True)
    with open(Migrator.SETTINGS_DIR, "w") as f:
        f.write('{"1.5.0": tr')

    m = Migrator("1.5.0")
    try:
        settings = m.get_settings()
    except Exception as e:
        print(f"FAIL(3): torn migrations.json still raises at startup: "
              f"{type(e).__name__}: {e}")
        return 1
    if settings != {}:
        print(f"FAIL(3): expected pending-everything, got: {settings}")
        return 1
    if not os.path.exists(Migrator.SETTINGS_DIR + ".corrupt"):
        print("FAIL(3): torn migrations.json not preserved aside")
        return 1
    # State must be writable again after quarantine.
    m.set_migrated(True)
    if not m.get_settings().get("1.5.0"):
        print("FAIL(3): migration state not recordable after quarantine")
        return 1
    print("PASS: torn migrations.json quarantined; startup path survives")
    return 0


def check_sweep_survives_poison() -> int:
    asset = os.path.join(gl.DATA_PATH, "asset.png")
    with open(asset, "wb") as f:
        f.write(b"png")

    poison = seed_page("PoisonPage")
    healthy = seed_page("HealthyPage")
    with open(healthy, "w") as f:
        json.dump({"keys": {"0x0": {"states": {"0": {"media": {"path": asset}}}}}}, f)
    corrupt(poison)

    try:
        gl.page_manager.remove_asset_from_all_pages(asset)
    except Exception as e:
        print(f"FAIL(4): one poison page aborted the sweep: "
              f"{type(e).__name__}: {e}")
        return 1

    with open(healthy) as f:
        cleaned = json.load(f)
    if cleaned["keys"]["0x0"]["states"]["0"]["media"]["path"] is not None:
        print("FAIL(4): healthy page was not cleaned")
        return 1
    print("PASS: asset sweep survives a poison page and cleans the rest")
    return 0


def _seed_page_with_backup(name: str, content: dict) -> str:
    """Write a real page + a validated known-good backup, then corrupt the
    primary. Returns the page path."""
    path = seed_page(name)
    backup_dir = os.path.join(gl.page_manager.PAGE_PATH, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(content, f)
    with open(os.path.join(backup_dir, os.path.basename(path)), "w") as f:
        json.dump(content, f)
    corrupt(path)
    return path


def check_set_page_settings_no_gut() -> int:
    # HIGH: the settings writers read with use_backup=False and save the
    # result back. A corrupt page must heal from the backup so the write
    # preserves keys/background instead of gutting them to {"settings": ...}.
    content = {"keys": {"0x0": {"states": {"0": {}}}}, "background": {"path": "wall.png"}}
    path = _seed_page_with_backup("SettingsWriterHeal", content)

    gl.page_manager.set_page_settings(path, {"brightness": 42})

    with open(path) as f:
        after = json.load(f)
    if "keys" not in after or "background" not in after:
        print(f"FAIL(5): set_page_settings gutted the live page, got: {after}")
        return 1
    if after.get("settings", {}).get("brightness") != 42:
        print(f"FAIL(5): the new setting did not survive, got: {after}")
        return 1
    if after["background"].get("path") != "wall.png":
        print(f"FAIL(5): healed content wrong, got: {after}")
        return 1
    print("PASS: set_page_settings on a corrupt page heals; keys/background + new setting survive")
    return 0


def check_get_page_settings_heals() -> int:
    # HIGH sibling: the pure reader must also heal (it feeds every mutator).
    content = {"keys": {}, "settings": {"brightness": 77}}
    path = _seed_page_with_backup("SettingsReaderHeal", content)

    got = gl.page_manager.get_page_settings(path)
    if got.get("brightness") != 77:
        print(f"FAIL(5b): get_page_settings did not heal from backup, got: {got}")
        return 1
    print("PASS: get_page_settings on a corrupt page reads the backup's settings")
    return 0


def check_heal_when_quarantine_fails() -> int:
    # MEDIUM: heal must not depend on the quarantine rename succeeding.
    content = {"keys": {}, "background": {"marker": "from-backup"}}
    path = _seed_page_with_backup("QuarantineFails", content)

    real_replace = os.replace

    def failing_replace(src, dst, *a, **kw):
        if str(dst).startswith(path) and ".corrupt" in os.path.basename(str(dst)):
            raise OSError("simulated read-only fs")
        return real_replace(src, dst, *a, **kw)

    with mock.patch("os.replace", side_effect=failing_replace):
        data = gl.page_manager.get_page_data(path)

    if data.get("background", {}).get("marker") != "from-backup":
        print(f"FAIL(6): heal did not fire when quarantine rename failed, got: {data}")
        return 1
    if not os.path.exists(path):
        print("FAIL(6): corrupt primary vanished though the rename was made to fail")
        return 1
    print("PASS: corrupt page heals from backup even when quarantine rename fails")
    return 0


def check_quarantine_no_clobber() -> int:
    # MEDIUM: a second corruption must not destroy the first .corrupt copy.
    from src.backend.SettingsManager import SettingsManager

    path = seed_page("NoClobber")
    with open(path, "w") as f:
        f.write("FIRST-CORRUPT")
    SettingsManager.load_settings_from_file(path)  # -> path + ".corrupt"

    if not os.path.exists(path + ".corrupt"):
        print("FAIL(7): first quarantine did not produce .corrupt")
        return 1
    with open(path + ".corrupt") as f:
        if f.read() != "FIRST-CORRUPT":
            print("FAIL(7): first .corrupt has wrong content")
            return 1

    # regenerate the primary and corrupt it a second time
    with open(path, "w") as f:
        f.write("SECOND-CORRUPT")
    SettingsManager.load_settings_from_file(path)  # must NOT clobber .corrupt

    with open(path + ".corrupt") as f:
        if f.read() != "FIRST-CORRUPT":
            print("FAIL(7): second quarantine clobbered the first forensic copy")
            return 1
    if not os.path.exists(path + ".corrupt.1"):
        print("FAIL(7): second corrupt copy was not preserved at .corrupt.1")
        return 1
    with open(path + ".corrupt.1") as f:
        if f.read() != "SECOND-CORRUPT":
            print("FAIL(7): .corrupt.1 has wrong content")
            return 1
    print("PASS: second corruption preserved at .corrupt.1, first forensic copy intact")
    return 0


def main() -> int:
    start_watchdog(30, "corrupt_json_fallback")
    fixtures._install_integration_globals()
    rc = 0
    for check in (
        check_page_backup_heal,
        check_page_no_backup_quarantine,
        check_migrations_torn,
        check_sweep_survives_poison,
        check_set_page_settings_no_gut,
        check_get_page_settings_heals,
        check_heal_when_quarantine_fails,
        check_quarantine_no_clobber,
    ):
        try:
            rc |= check()
        except Exception as e:  # a check itself raising is a failure too
            print(f"FAIL({check.__name__}): raised {type(e).__name__}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
