"""
Regression test for atomic JSON writes (issue #119, upstream #618).

Page.save() got the tmp-file + fsync + os.replace + dir-fsync treatment in
4faa8ea3, but SettingsManager.save_settings_to_file, PluginBase.set_settings
and PageManagerBackend.add_page still wrote with plain open("w") +
json.dump -- a crash/SIGKILL/OOM mid-write truncated the destination file in
place and the config was gone on the next launch. All of them now route
through src/backend/atomic_json.py::atomic_write_json.

Two fault models are exercised:

  1. Serialization fault mid-dump (an unserializable object raises TypeError
     after json.dump already emitted a prefix): with the old code the
     destination is already truncated at that point; with the atomic helper
     only the temp file is affected and it's cleaned up.
  2. Process death after the temp file is written but before os.replace
     (fsync patched to os._exit(9) in a subprocess): the destination must
     still contain the previous, complete JSON.
"""
import glob
import json
import os
import subprocess
import sys

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
    page = controller.active_page
    before = read_json(page.json_path)

    # Top-level key: untouched by get_without_action_objects' traversal, but
    # json.dump chokes on it mid-serialization.
    page.dict["poison"] = Unserializable()
    try:
        page.save()
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


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_atomic_settings")
    controller = fixtures.make_headless_controller(serial="atomic-1")
    try:
        check_settings_manager()
        check_plugin_base()
        check_add_page()
        check_page_save(controller)
        check_kill_before_replace()
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_atomic_settings")


if __name__ == "__main__":
    main()
