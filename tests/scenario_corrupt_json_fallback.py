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
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import json
import os

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


def main() -> int:
    start_watchdog(30, "corrupt_json_fallback")
    fixtures._install_integration_globals()
    rc = 0
    for check in (
        check_page_backup_heal,
        check_page_no_backup_quarantine,
        check_migrations_torn,
        check_sweep_survives_poison,
    ):
        try:
            rc |= check()
        except Exception as e:  # a check itself raising is a failure too
            print(f"FAIL({check.__name__}): raised {type(e).__name__}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
