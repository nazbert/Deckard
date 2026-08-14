"""
Corrupt-JSON quarantine on the plugin file set, with sidecar retention.

A corrupt settings.json moves to a .corrupt sidecar and the read falls back
to {}, so a later set_settings cannot overwrite the last copy. Retention
keeps three sidecars per file and prunes the oldest. A plugin source file, a
manifest or an about.json, is never moved. An OSError is not corruption.
"""
import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import json
import os
import textwrap
import time
from unittest import mock

import globals as gl  # noqa: E402

GOOD_MAIN = """
    from src.backend.PluginManager.PluginBase import PluginBase

    class {class_name}(PluginBase):
        def __init__(self):
            super().__init__()
            self.register()
"""


def manifest_json(plugin_id: str) -> str:
    return json.dumps({
        "name": plugin_id,
        "id": plugin_id,
        "github": f"https://github.com/example/{plugin_id}",
        "version": "1.0.0",
        "app-version": "1.5.0",
        "minimum-app-version": "1.0.0",
    })


def write_plugin(folder: str, class_name: str, manifest_text: str | None) -> str:
    plugin_dir = os.path.join(gl.PLUGIN_DIR, folder)
    os.makedirs(plugin_dir, exist_ok=True)
    with open(os.path.join(plugin_dir, "main.py"), "w") as f:
        f.write(textwrap.dedent(GOOD_MAIN.format(class_name=class_name)))
    if manifest_text is not None:
        with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
            f.write(manifest_text)
    return plugin_dir


def corrupt(path: str, marker: str) -> bytes:
    """Truncated mid-token but identifiable. The sidecar's content proves
    which corruption was preserved."""
    payload = f'{{"file-version": "2.0", "marker": "{marker}", "settings": {{"a'
    with open(path, "w") as f:
        f.write(payload)
    return payload.encode()


def sidecars(path: str) -> list[str]:
    """Every quarantine sidecar of path, by name."""
    directory = os.path.dirname(path)
    base = os.path.basename(path) + ".corrupt"
    return sorted(
        e for e in os.listdir(directory)
        if e == base or (e.startswith(base + ".") and e[len(base) + 1:].isdigit())
    )


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


def check_settings_quarantine(plugin) -> None:
    path = plugin.settings_path
    plugin.set_settings({"marker": "before-corruption"})
    assert os.path.isfile(path), "precondition: settings file written"

    bad = corrupt(path, "first")
    got = plugin.get_settings()

    assert got == {}, f"corrupt settings must still fall back to empty, got {got}"
    assert not os.path.exists(path), (
        "the corrupt settings file was left in place -- the next set_settings() "
        "would overwrite the only copy the user has left"
    )
    quarantined = sidecars(path)
    assert quarantined == [os.path.basename(path) + ".corrupt"], (
        f"expected exactly one .corrupt sidecar, got {quarantined}"
    )
    dest = os.path.join(os.path.dirname(path), quarantined[0])
    assert read(dest).encode() == bad, "the sidecar is not the corrupt file, byte for byte"

    # The plugin is usable again afterwards, with no stale corruption.
    plugin.set_settings({"marker": "after-corruption"})
    assert plugin.get_settings() == {"marker": "after-corruption"}, (
        f"plugin could not save/read after quarantine: {plugin.get_settings()}"
    )
    print("PASS(1): corrupt plugin settings quarantined, fallback returned, next load clean")


def check_set_settings_preserves_corrupt(plugin) -> None:
    path = plugin.settings_path
    bad = corrupt(path, "second")

    # The data-loss moment. This write replaces the file wholesale.
    plugin.set_settings({"marker": "written-over-corruption"})

    assert plugin.get_settings() == {"marker": "written-over-corruption"}
    quarantined = sidecars(path)
    assert len(quarantined) == 2, f"expected the second corruption preserved too, got {quarantined}"
    contents = {read(os.path.join(os.path.dirname(path), name)) for name in quarantined}
    assert bad.decode() in contents, (
        "set_settings overwrote the corrupt file without preserving it first"
    )
    print("PASS(2): set_settings preserves the corrupt file before replacing it")


def check_prior_sidecar_not_clobbered(plugin) -> None:
    path = plugin.settings_path
    names = sidecars(path)
    base = os.path.basename(path) + ".corrupt"
    assert names[0] == base and names[1] == base + ".1", (
        f"a second corruption must take the next free slot, got {names}"
    )
    first, second = (read(os.path.join(os.path.dirname(path), n)) for n in names)
    assert '"marker": "first"' in first, f"the FIRST forensic copy was clobbered: {first!r}"
    assert '"marker": "second"' in second, f"the second copy is wrong: {second!r}"
    print("PASS(3): a prior .corrupt is never clobbered")


def check_retention(path: str, corrupting_read, label: str) -> None:
    """Five successive corruptions of path must leave exactly three sidecars,
    with the oldest pruned. corrupting_read is the loader call that
    quarantines, one per wired call site."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for n in range(1, 6):
        corrupt(path, f"gen{n}")
        got = corrupting_read()
        assert got == {}, f"{label}: corrupt read must fall back to empty, got {got}"
        # Distinct mtimes. The sidecar inherits the corrupt primary's mtime,
        # and the oldest-first prune orders by that.
        time.sleep(0.01)

    names = sidecars(path)
    assert len(names) == 3, f"{label}: 5 corruptions must leave exactly 3 sidecars, got {names}"
    survived = {read(os.path.join(os.path.dirname(path), n)) for n in names}
    markers = sorted(s.split('"marker": "')[1].split('"')[0] for s in survived)
    assert markers == ["gen3", "gen4", "gen5"], (
        f"{label}: retention must keep the NEWEST three and prune oldest-first, kept {markers}"
    )
    print(f"PASS(4): .corrupt sidecars are retention-bounded, oldest pruned -- {label}")


def check_fresh_sidecar_survives_prune(plugin) -> None:
    """os.replace carries the primary's mtime onto the new sidecar, so a
    corrupt file that is old on disk produces a fresh forensic copy that looks
    older than every sidecar already there. The prune must keep it."""
    path = plugin.settings_path
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Three sidecars with recent mtimes fill the retention budget.
    for n in range(1, 4):
        corrupt(path, f"existing{n}")
        assert plugin.get_settings() == {}
        time.sleep(0.01)
    assert len(sidecars(path)) == 3, "precondition: retention budget full"

    # The fourth corruption is old on disk, as a timestamp-preserving restore
    # leaves it.
    corrupt(path, "old-but-fresh-copy")
    old = time.time() - 365 * 24 * 3600
    os.utime(path, (old, old))

    assert plugin.get_settings() == {}
    names = sidecars(path)
    assert len(names) == 3, f"retention still bounded, got {names}"
    survived = {read(os.path.join(os.path.dirname(path), n)) for n in names}
    markers = sorted(s.split('"marker": "')[1].split('"')[0] for s in survived)
    assert "old-but-fresh-copy" in markers, (
        "the sidecar the loader had just created was pruned by its own retention "
        f"call -- the forensic copy 'preserved at ...' no longer exists; kept {markers}"
    )
    assert markers == ["existing2", "existing3", "old-but-fresh-copy"], (
        f"the next-oldest sidecar should go instead of the fresh one, kept {markers}"
    )
    print("PASS(8): the sidecar just created is never pruned by its own retention call")


def check_garbage_bytes_are_corruption(plugin, migrator) -> None:
    """Undecodable bytes raise UnicodeDecodeError, which is a ValueError but
    not a JSONDecodeError. A handler that names the JSON error lets the
    crudest corruption of all straight through."""
    path = plugin.settings_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\xff\xfe\x00\x80garbage\xc3\x28")

    got = plugin.get_settings()  # a UnicodeDecodeError here would escape register()
    assert got == {}, f"garbage bytes must fall back to empty, got {got}"
    assert len(sidecars(path)) == 1, (
        f"garbage bytes are corruption and must be quarantined, got {sidecars(path)}"
    )

    # The same class of file, read by the startup-critical Migrator loader.
    with open(migrator.SETTINGS_DIR, "wb") as f:
        f.write(b"\xff\xfe\x00\x80garbage\xc3\x28")
    try:
        assert migrator.get_settings() == {}
    except UnicodeDecodeError as e:
        raise AssertionError(f"garbage migrations.json still aborts startup: {e}") from None
    assert len(sidecars(migrator.SETTINGS_DIR)) >= 1, "garbage migrations.json not preserved"
    print("PASS(9): undecodable bytes are treated as corruption, not raised")


def check_migrator_oserror_not_corruption(migrator) -> None:
    """The Migrator must not quarantine on OSError. Renaming away a healthy
    but momentarily unreadable migrations.json reports every migrator as
    pending and re-runs the lot."""
    path = migrator.SETTINGS_DIR
    migrator.set_migrated(True)
    before = read(path)
    n_before = len(sidecars(path))

    real_open = open

    def failing_open(file, *args, **kwargs):
        if str(file) == path:
            raise PermissionError(13, "simulated EACCES")
        return real_open(file, *args, **kwargs)

    with mock.patch("builtins.open", side_effect=failing_open):
        got = migrator.get_settings()

    assert got == {}, f"an unreadable migrations.json still reports pending, got {got}"
    assert len(sidecars(path)) == n_before, (
        "an unreadable migrations.json was quarantined -- a healthy state file renamed "
        "away over a transient EACCES re-runs every migrator"
    )
    assert os.path.isfile(path) and read(path) == before, "the healthy state file was disturbed"
    print("PASS(10): the Migrator no longer quarantines on OSError")


def check_asset_manager_quarantines(plugin) -> None:
    """The asset manager reads and rewrites the same settings.json.
    load_assets is the earliest reader of all, inside PluginBase.__init__, and
    save_assets replaces the file wholesale from five live UI call sites."""
    path = plugin.settings_path
    am = plugin.asset_manager
    os.makedirs(os.path.dirname(path), exist_ok=True)

    bad = corrupt(path, "asset-load")
    am.load_assets()  # must not raise, and must quarantine
    assert not os.path.exists(path), "load_assets left the corrupt file in place"
    names = sidecars(path)
    assert len(names) == 1, f"load_assets did not quarantine, got {names}"
    assert read(os.path.join(os.path.dirname(path), names[0])).encode() == bad

    # The save_assets write below replaces the file wholesale.
    bad2 = corrupt(path, "asset-save")
    am.save_assets()

    names = sidecars(path)
    assert len(names) == 2, f"save_assets destroyed the corrupt file, got {names}"
    contents = {read(os.path.join(os.path.dirname(path), n)) for n in names}
    assert bad2.decode() in contents, "save_assets overwrote the corrupt file unpreserved"
    with open(path) as f:
        assert "assets" in json.load(f), "save_assets did not write the new content"

    # Valid JSON that is not an object must not raise on assignment.
    with open(path, "w") as f:
        f.write('["not", "an", "object"]')
    am.save_assets()
    with open(path) as f:
        assert "assets" in json.load(f), "a non-object settings file broke save_assets"
    print("PASS(11): the asset manager quarantines the same file the same way")


def check_oserror_not_corruption(plugin) -> None:
    path = plugin.settings_path
    plugin.set_settings({"marker": "healthy"})
    before = read(path)
    assert sidecars(path) == [], "precondition: no sidecars yet for this plugin"

    real_open = open

    def failing_open(file, *args, **kwargs):
        if str(file) == path:
            raise PermissionError(13, "simulated EACCES")
        return real_open(file, *args, **kwargs)

    with mock.patch("builtins.open", side_effect=failing_open):
        got = plugin.get_settings()

    assert got == {}, f"an unreadable settings file still falls back to empty, got {got}"
    assert sidecars(path) == [], (
        "an unreadable file is NOT a corrupt one -- quarantining on EACCES would "
        f"destroy a healthy file's availability; got {sidecars(path)}"
    )
    assert os.path.isfile(path) and read(path) == before, "the healthy file was disturbed"
    assert plugin.get_settings() == {"marker": "healthy"}, (
        "settings must read back untouched once the file is readable again"
    )
    print("PASS(5): OSError keeps its old behavior -- no quarantine, file untouched")


def check_manifest_degrades_like_missing(pm, PluginBase, corrupt_dir, missing_dir) -> None:
    for folder in ("com_test_q_manifest_corrupt", "com_test_q_manifest_missing"):
        assert folder in pm.load_errors, (
            f"{folder} must be recorded in load_errors, got {pm.load_errors}"
        )
    assert (pm.load_errors["com_test_q_manifest_corrupt"]
            == pm.load_errors["com_test_q_manifest_missing"]), (
        "a quarantined manifest must degrade EXACTLY like a missing one, got "
        f"{pm.load_errors}"
    )
    for plugin_id in ("com_test_q_manifest_corrupt", "com_test_q_manifest_missing"):
        assert plugin_id not in PluginBase.plugins
        assert plugin_id not in PluginBase.disabled_plugins
    assert "com_test_q_settings" in PluginBase.plugins, (
        f"the plugin scan must continue past a corrupt manifest; registered="
        f"{sorted(PluginBase.plugins)}, errors={pm.load_errors}"
    )

    # The manifest lives in the plugin's source tree, which the app never
    # writes, so it must stay exactly where it is. A rename would mutate the
    # developer's git working tree, because dev plugins are symlinks into
    # ~/dev. Nothing overwrites it either, so nothing needs protection.
    manifest_path = os.path.join(corrupt_dir, "manifest.json")
    assert os.path.isfile(manifest_path), (
        "the corrupt manifest was moved out of the plugin's source tree"
    )
    assert read(manifest_path) == '{"name": "x", "id": ', "the source manifest was modified"
    assert sidecars(manifest_path) == [], (
        f"a plugin SOURCE file must never be quarantined: {os.listdir(corrupt_dir)}"
    )
    assert not os.path.exists(os.path.join(missing_dir, "manifest.json"))
    print("PASS(6): a corrupt manifest degrades exactly like a missing one, left in place")


def check_about_degrades_to_empty(plugin) -> None:
    about_path = os.path.join(plugin.PATH, "about.json")
    with open(about_path, "w") as f:
        f.write('{"copyright": "me"')  # truncated

    got = plugin.get_about()  # a raise here would reach the about window

    assert got == {}, f"a corrupt about.json must degrade to the missing-file result, got {got}"
    # A source file again. Log it and leave it alone.
    assert os.path.isfile(about_path) and read(about_path) == '{"copyright": "me"'
    assert sidecars(about_path) == [], (
        f"a plugin SOURCE file must never be quarantined: {os.listdir(plugin.PATH)}"
    )

    # Valid JSON that is not an object must not reach PluginAbout and raise
    # AttributeError on .get().
    with open(about_path, "w") as f:
        f.write('["not", "an", "object"]')
    assert plugin.get_about() == {}, "a non-object about.json must degrade to {}"
    print("PASS(7): corrupt/non-object about.json degrades to {} instead of raising, left in place")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_plugin_settings_quarantine")

    from src.backend.Migration.Migrator import Migrator
    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.PluginManager.PluginManager import PluginManager

    # Real SettingsManager and PageManagerBackend. The page loader delegates
    # its corrupt-read handling to SettingsManager, which drives the second
    # wired call site end to end.
    fixtures._install_integration_globals()

    write_plugin("com_test_q_settings", "QSettingsPlugin", manifest_json("com_test_q_settings"))
    write_plugin("com_test_q_retention", "QRetentionPlugin", manifest_json("com_test_q_retention"))
    write_plugin("com_test_q_oserror", "QOsErrorPlugin", manifest_json("com_test_q_oserror"))
    write_plugin("com_test_q_prune", "QPrunePlugin", manifest_json("com_test_q_prune"))
    write_plugin("com_test_q_garbage", "QGarbagePlugin", manifest_json("com_test_q_garbage"))
    write_plugin("com_test_q_assets", "QAssetsPlugin", manifest_json("com_test_q_assets"))
    # A corrupt manifest and no manifest at all must be indistinguishable.
    corrupt_dir = write_plugin("com_test_q_manifest_corrupt", "QCorruptManifestPlugin",
                               '{"name": "x", "id": ')
    missing_dir = write_plugin("com_test_q_manifest_missing", "QMissingManifestPlugin", None)

    pm = PluginManager()
    gl.plugin_manager = pm
    pm.load_plugins()

    check_manifest_degrades_like_missing(pm, PluginBase, corrupt_dir, missing_dir)

    settings_plugin = PluginBase.plugins["com_test_q_settings"]["object"]
    check_settings_quarantine(settings_plugin)
    check_set_settings_preserves_corrupt(settings_plugin)
    check_prior_sidecar_not_clobbered(settings_plugin)
    check_about_degrades_to_empty(settings_plugin)

    check_oserror_not_corruption(PluginBase.plugins["com_test_q_oserror"]["object"])
    check_fresh_sidecar_survives_prune(PluginBase.plugins["com_test_q_prune"]["object"])

    # Retention, once per wired quarantine call site.
    retention_plugin = PluginBase.plugins["com_test_q_retention"]["object"]
    check_retention(retention_plugin.settings_path, retention_plugin.get_settings,
                    "plugin settings (PluginBase loader)")

    page_path = fixtures.seed_page("QRetentionPage")
    check_retention(page_path, lambda: gl.page_manager.get_page_data(page_path),
                    "pages/app settings (SettingsManager loader, via PageManagerBackend)")

    migrator = Migrator("1.5.0")
    check_retention(Migrator.SETTINGS_DIR, migrator.get_settings,
                    "migrations.json (Migrator loader)")

    check_garbage_bytes_are_corruption(
        PluginBase.plugins["com_test_q_garbage"]["object"], migrator)
    check_migrator_oserror_not_corruption(migrator)
    check_asset_manager_quarantines(PluginBase.plugins["com_test_q_assets"]["object"])

    print("PASS: scenario_plugin_settings_quarantine")


if __name__ == "__main__":
    main()
