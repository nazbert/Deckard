"""Regression test for atomic JSON writes.

Every settings and page writer routes through atomic_write_json, so a crash
mid-write leaves the destination complete and only a temp file behind.
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
    """json.dump raises TypeError on this, mid-stream.

    The serializable prefix of the payload is already written by then.
    """


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

    # A top-level key that get_without_action_objects does not traverse, but
    # json.dump chokes on mid-serialization.
    page.dict["poison"] = Unserializable()
    try:
        # save() only marks the page, so the serialization and the TypeError
        # belong to the flush. Every synchronous flush site does this: page
        # switch, deck close, quit, or any read of the file. The assertion
        # pins the file, not the timing.
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
    """save_font_defaults must merge into the general section.

    A replace wipes hold-time, rolling-labels and app-launches whenever a font
    default changes.
    """
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
    """New files must honor the process umask; existing modes must survive.

    Plugin settings hold API tokens, so a hardcoded 0644 leaks them under
    umask 077.
    """
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
    """A write through a symlinked config must update the real file.

    The link must stay a link, because os.replace over the link path leaves a
    regular file and detaches a stow or chezmoi managed settings tree.
    """
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
    """A later write to the same target must reap orphaned temp files.

    A SIGKILL between write and rename leaves one. A racing writer's fresh
    temp must stay.
    """
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
    """Model a power loss after the temp file is written, before the rename.

    The destination must keep its previous complete content. os._exit skips
    atexit, so the child temp data dir survives for the parent to inspect.
    """
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
        # The child DATA_DIR is two levels above .../settings/kill.json
        shutil.rmtree(os.path.dirname(os.path.dirname(target)), ignore_errors=True)
    print("PASS: destination survives process death between write and rename")


def check_migrator_page_write() -> None:
    """The migrator page rewrite must go through atomic_write_json.

    The rewrite nests each key under states.0. A death mid-dump leaves a
    truncated page the loader must quarantine, on the first launch after an
    upgrade. The child owns its data dir and dies at fsync, so nothing commits.
    """
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
    # A plain open('w') and json.dump never fsyncs, so it never reaches the
    # trap. rc 0 here means the migrator stopped writing atomically.
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
        # Same residue rule as every other atomic write. Expect a never-renamed
        # temp under the writer prefix, never a partial file under the real name.
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
