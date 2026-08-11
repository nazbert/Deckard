"""
Regression test for atomic JSON writes.

Page.save() got the tmp-file + fsync + os.replace + dir-fsync treatment in
4faa8ea3, but SettingsManager.save_settings_to_file, PluginBase.set_settings,
PageManagerBackend.add_page and the 1.5.0-beta.5 migrator's page rewrite still
wrote with plain open("w") + json.dump -- a crash/SIGKILL/OOM mid-write
truncated the destination file in place and the config was gone on the next
launch. All of them now route through
src/backend/atomic_json.py::atomic_write_json.

Two fault models are exercised:

  1. Serialization fault mid-dump (an unserializable object raises TypeError
     after json.dump already emitted a prefix): with the old code the
     destination is already truncated at that point; with the atomic helper
     only the temp file is affected and it's cleaned up.
  2. Process death after the temp file is written but before os.replace
     (fsync patched to os._exit(9) in a subprocess): the destination must
     still contain the previous, complete JSON.

Additionally: new files must honor the process umask
(secret-bearing plugin settings must not come out world-readable under
umask 077) while pre-existing modes are preserved; symlinked targets must
stay symlinks (os.replace on the link path would silently detach
stow/chezmoi-managed configs); and temp files orphaned by a hard kill must
be reaped by later writes to the same target instead of accumulating
forever.
"""
import glob
import json
import os
import subprocess
import sys
import time

import fixtures
import globals as gl


class Unserializable:
    """json.dump raises TypeError on this -- but only mid-stream, after the
    serializable prefix of the payload was already written."""


def tmp_litter(dir_path: str) -> list[str]:
    return glob.glob(os.path.join(dir_path, ".save-*.tmp"))


def read_json(path: str):
    with open(path) as f:
        return json.load(f)


def check_settings_manager() -> None:
    path = os.path.join(gl.DATA_PATH, "settings", "atomic_test.json")
    good = {"keep": True, "nested": {"a": 1}}

    gl.settings_manager.save_settings_to_file(path, good)
    assert read_json(path) == good, "plain save round-trip failed"

    try:
        gl.settings_manager.save_settings_to_file(path, {"keep": False, "bad": Unserializable()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError from unserializable payload")

    assert read_json(path) == good, (
        "settings file was corrupted by an interrupted save_settings_to_file"
    )
    assert not tmp_litter(os.path.dirname(path)), "temp file left behind after failed save"
    print("PASS: SettingsManager.save_settings_to_file survives a mid-write fault")


def check_plugin_base() -> None:
    from src.backend.PluginManager.PluginBase import PluginBase

    plugin = object.__new__(PluginBase)  # set_settings only touches settings_path
    plugin.settings_path = os.path.join(
        gl.DATA_PATH, "settings", "plugins", "com_test_atomic", "settings.json"
    )

    plugin.set_settings({"volume": 42})
    on_disk = read_json(plugin.settings_path)
    assert on_disk == {"file-version": "2.0", "settings": {"volume": 42}}, on_disk

    try:
        plugin.set_settings({"volume": 0, "bad": Unserializable()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError from unserializable payload")

    assert read_json(plugin.settings_path) == {
        "file-version": "2.0",
        "settings": {"volume": 42},
    }, "plugin settings file was corrupted by an interrupted set_settings"
    assert not tmp_litter(os.path.dirname(plugin.settings_path))
    print("PASS: PluginBase.set_settings survives a mid-write fault")


def check_add_page() -> None:
    path = gl.page_manager.add_page("AtomicNew", {"keys": {}})
    assert os.path.isfile(path) and read_json(path) == {"keys": {}}

    try:
        gl.page_manager.add_page("AtomicBad", {"bad": Unserializable()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError from unserializable payload")

    bad_path = os.path.join(gl.page_manager.PAGE_PATH, "AtomicBad.json")
    assert not os.path.exists(bad_path), (
        "add_page left a partial page file behind after an interrupted write"
    )
    assert not tmp_litter(gl.page_manager.PAGE_PATH)
    print("PASS: PageManagerBackend.add_page survives a mid-write fault")


def check_page_save(controller) -> None:
    from src.backend.PageManagement import page_flush

    page = controller.active_page
    before = read_json(page.json_path)

    # Top-level key: untouched by get_without_action_objects' traversal, but
    # json.dump chokes on it mid-serialization.
    page.dict["poison"] = Unserializable()
    try:
        # save() only marks the page now -- the serialization, and so the
        # TypeError, belongs to the flush. Asked for explicitly here, which
        # is what every synchronous flush site (page switch, deck close,
        # quit, any read of the file) does; the assertion below is unchanged,
        # because what is being pinned is the file, not the timing.
        page.save()
        page_flush.get().flush_path(page.json_path)
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError from unserializable payload")
    finally:
        page.dict.pop("poison", None)

    assert read_json(page.json_path) == before, (
        "page json was corrupted by an interrupted Page.save()"
    )
    assert not tmp_litter(os.path.dirname(page.json_path))
    print("PASS: Page.save() survives a mid-write fault")


def check_font_defaults_merge() -> None:
    """save_font_defaults must merge into the general section, not replace
    it -- it used to wipe hold-time/rolling-labels/app-launches/... whenever
    a font default was changed."""
    app_settings = gl.settings_manager.get_app_settings()
    app_settings.setdefault("general", {})
    app_settings["general"]["hold-time"] = 0.7
    app_settings["general"]["rolling-labels"] = False
    gl.settings_manager.save_app_settings(app_settings)

    gl.settings_manager.font_defaults = {"font-color": [10, 20, 30, 255]}
    gl.settings_manager.save_font_defaults()

    general = gl.settings_manager.get_app_settings().get("general", {})
    assert general.get("default-font", {}).get("font-color") == [10, 20, 30, 255]
    assert general.get("hold-time") == 0.7 and general.get("rolling-labels") is False, (
        f"save_font_defaults wiped sibling general.* settings: {general}"
    )
    print("PASS: save_font_defaults preserves the rest of the general section")


def check_umask_and_mode_preservation() -> None:
    """New files must honor the process umask like plain open('w') did --
    hardcoding 0644 leaked secret-bearing files (plugin settings hold API
    tokens) under umask 077. Pre-existing modes must be preserved."""
    from src.backend.atomic_json import atomic_write_json

    base = os.path.join(gl.DATA_PATH, "settings", "modes")
    fresh = os.path.join(base, "fresh.json")
    old_umask = os.umask(0o077)
    try:
        atomic_write_json(fresh, {"token": "secret"})
    finally:
        os.umask(old_umask)
    mode = os.stat(fresh).st_mode & 0o777
    assert mode == 0o600, (
        f"new file ignored umask 077: mode {oct(mode)}, want 0o600 -- "
        f"secret-bearing plugin settings would be world-readable"
    )

    keep = os.path.join(base, "keep.json")
    atomic_write_json(keep, {"v": 1})
    os.chmod(keep, 0o640)
    atomic_write_json(keep, {"v": 2})
    mode = os.stat(keep).st_mode & 0o777
    assert mode == 0o640, f"existing file mode not preserved: {oct(mode)}, want 0o640"
    assert read_json(keep) == {"v": 2}
    print("PASS: new files honor umask; existing modes are preserved")


def check_symlinked_target() -> None:
    """Writing through a symlinked config must update the REAL file and keep
    the link a link -- os.replace over the link path replaces it with a
    regular file, silently detaching stow/chezmoi-managed settings."""
    from src.backend.atomic_json import atomic_write_json

    real_dir = os.path.join(gl.DATA_PATH, "dotfiles-store")
    os.makedirs(real_dir, exist_ok=True)
    real = os.path.join(real_dir, "managed-settings.json")
    with open(real, "w") as f:
        json.dump({"generation": 1}, f)

    link_dir = os.path.join(gl.DATA_PATH, "settings", "linked")
    os.makedirs(link_dir, exist_ok=True)
    link = os.path.join(link_dir, "settings.json")
    os.symlink(real, link)

    atomic_write_json(link, {"generation": 2})

    assert os.path.islink(link), (
        "symlinked config was replaced by a regular file -- the managed real "
        "file keeps stale content and a later re-link reverts every edit"
    )
    assert read_json(real) == {"generation": 2}, (
        f"real file behind the symlink was not updated: {read_json(real)}"
    )
    assert read_json(link) == {"generation": 2}
    print("PASS: symlinked targets stay symlinks and the real file is updated")


def check_stale_tmp_reaped() -> None:
    """Temps orphaned by SIGKILL-between-write-and-rename must be reaped by
    a later write to the same target (they used to accumulate forever); a
    racing writer's FRESH temp must be left alone."""
    from src.backend.atomic_json import atomic_write_json

    d = os.path.join(gl.DATA_PATH, "settings", "reap")
    target = os.path.join(d, "target.json")
    atomic_write_json(target, {"v": 1})

    stale = os.path.join(d, ".save-target.json.orphan.tmp")
    with open(stale, "w") as f:
        f.write("{")
    two_hours_ago = time.time() - 2 * 60 * 60
    os.utime(stale, (two_hours_ago, two_hours_ago))

    fresh = os.path.join(d, ".save-target.json.racing.tmp")
    with open(fresh, "w") as f:
        f.write("{")

    other = os.path.join(d, ".save-other.json.orphan.tmp")  # different target
    with open(other, "w") as f:
        f.write("{")
    os.utime(other, (two_hours_ago, two_hours_ago))

    atomic_write_json(target, {"v": 2})

    assert not os.path.exists(stale), "stale orphaned temp for the same target was not reaped"
    assert os.path.exists(fresh), "a racing writer's fresh temp was wrongly deleted"
    assert os.path.exists(other), "another target's temp was wrongly deleted"
    assert read_json(target) == {"v": 2}
    os.remove(fresh)
    os.remove(other)
    print("PASS: stale same-target temps reaped; fresh/other-target temps untouched")


def check_kill_before_replace() -> None:
    """Simulates dying (power loss / SIGKILL) after the temp file is written
    but before it's renamed over the destination: the destination must keep
    the previous complete content. os._exit skips atexit, so the child's
    temp data dir survives for the parent to inspect (cleaned up below)."""
    child_code = (
        "import fixtures, os\n"
        "from src.backend.atomic_json import atomic_write_json\n"
        "target = os.path.join(fixtures.DATA_DIR, 'settings', 'kill.json')\n"
        "atomic_write_json(target, {'generation': 1})\n"
        "print(target, flush=True)\n"
        "real_fsync = os.fsync\n"
        "def dying_fsync(fd):\n"
        "    real_fsync(fd)\n"
        "    os._exit(9)\n"
        "os.fsync = dying_fsync\n"
        "atomic_write_json(target, {'generation': 2})\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 9, f"child should have died at fsync, rc={proc.returncode}: {proc.stderr}"
    target = proc.stdout.strip().splitlines()[-1]
    try:
        assert read_json(target) == {"generation": 1}, (
            "destination no longer holds the previous complete JSON after a "
            "mid-write process death"
        )
    finally:
        import shutil
        # child's DATA_DIR is two levels above .../settings/kill.json
        shutil.rmtree(os.path.dirname(os.path.dirname(target)), ignore_errors=True)
    print("PASS: destination survives process death between write and rename")


def check_migrator_page_write() -> None:
    """The 1.5.0-beta.5 migrator rewrites every page in place to nest each
    key under states.0. That rewrite used a plain open('w') + json.dump, so a
    death mid-dump left a truncated page the loader then had to quarantine --
    on the very first launch after an upgrade, with only the pre-migration
    backup zip to fall back on. It now routes through atomic_write_json.

    Runs entirely in a child with its own data dir (a live controller owns the
    pages/ of this process): the child dies at fsync, so the write never
    commits and the ORIGINAL page must still be complete on disk."""
    child_code = (
        "import fixtures, os, json\n"
        "import globals as gl\n"
        "from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5\n"
        "pages = os.path.join(gl.DATA_PATH, 'pages')\n"
        "os.makedirs(pages, exist_ok=True)\n"
        "with open(os.path.join(pages, 'Flat.json'), 'w') as f:\n"
        "    json.dump({'keys': {'0x0': {'labels': {'top': {'text': 'keep-me'}}}}}, f, indent=4)\n"
        "print(gl.DATA_PATH, flush=True)\n"
        "real_fsync = os.fsync\n"
        "def dying_fsync(fd):\n"
        "    real_fsync(fd)\n"
        "    os._exit(9)\n"
        "os.fsync = dying_fsync\n"
        "Migrator_1_5_0_beta_5().migrate_pages()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    # A bare open('w') + json.dump never fsyncs, so it would not even reach the
    # trap: rc 0 here means the migrator stopped writing atomically.
    assert proc.returncode == 9, (
        f"child should have died at fsync, rc={proc.returncode}: {proc.stderr}"
    )
    data_path = proc.stdout.strip().splitlines()[-1]
    pages_dir = os.path.realpath(os.path.join(data_path, "pages"))
    page_path = os.path.join(pages_dir, "Flat.json")
    try:
        assert read_json(page_path) == {"keys": {"0x0": {"labels": {"top": {"text": "keep-me"}}}}}, (
            "page json is no longer the complete pre-migration content after a "
            "mid-rewrite process death"
        )
        # Same residue rule as every other atomic write: the never-renamed temp
        # under the writer's own prefix, never a partial file under the real name.
        assert glob.glob(os.path.join(pages_dir, ".save-Flat.json.*.tmp")), (
            f"no .save-Flat.json.*.tmp residue in {pages_dir} -- the page "
            "rewrite did not go through the shared atomic writer"
        )
    finally:
        import shutil
        shutil.rmtree(data_path, ignore_errors=True)
    print("PASS: migrator page rewrite survives process death between write and rename")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_atomic_settings")
    controller = fixtures.make_headless_controller(serial="atomic-1")
    try:
        check_settings_manager()
        check_plugin_base()
        check_add_page()
        check_page_save(controller)
        check_font_defaults_merge()
        check_umask_and_mode_preservation()
        check_symlinked_target()
        check_stale_tmp_reaped()
        check_kill_before_replace()
        check_migrator_page_write()
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_atomic_settings")


if __name__ == "__main__":
    main()
