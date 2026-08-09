"""
Regression test for issue #152: corrupt-JSON quarantine on the PLUGIN file
set (settings.json / manifest.json / about.json), plus the ``.corrupt``
sidecar retention policy.

Issue #32 gave pages and app/page settings quarantine-on-corrupt-read; the
plugin files were explicitly deferred. Until #152 they fell back to ``{}``
silently and left the bad file in place, so a corrupt plugin settings file
was indistinguishable from "this plugin has no settings" -- and the next
set_settings() overwrote the only surviving copy of the user's config.

Pins, all against the real load path (no GTK, no hardware):

1. QUARANTINE + FALLBACK + CLEAN NEXT LOAD -- a corrupt settings.json is
   moved to <path>.corrupt (byte-for-byte), get_settings() still returns {},
   and the plugin can save and read back normally afterwards.
2. WRITE PATH -- set_settings() on a corrupt file preserves it BEFORE the
   atomic write replaces it: the sidecar holds the corrupt bytes and the
   primary holds the new settings.
3. MANIFEST DEGRADES EXACTLY LIKE A MISSING MANIFEST -- the plugin lands in
   PluginManager.load_errors with the identical "did not register (invalid or
   incomplete manifest?)" reason a manifest-less plugin gets. Never
   registered, never disabled, and the scan continues (a healthy neighbor
   still loads). The corrupt manifest is NOT quarantined: it is a plugin
   SOURCE file, which the app never writes, so there is no save to protect it
   from -- and moving it would mutate the developer's git working tree.
4. ABOUT -- a corrupt about.json degrades to the missing-file result ({})
   instead of raising into the plugin-about window, and a valid-but-non-object
   one no longer raises AttributeError downstream. Source file: left in place.
5. A PRIOR .corrupt IS NEVER CLOBBERED -- a second corruption takes the next
   free slot (.corrupt.1); the first forensic copy survives intact.
6. RETENTION, ONCE PER WIRED QUARANTINE CALL SITE (the plugin loader, the
   SettingsManager loader reached through PageManagerBackend's page read,
   and the Migrator's pre-SettingsManager loader) -- five successive
   corruptions leave exactly three sidecars, the OLDEST pruned (contents pin
   which three survived). Names are deliberately not asserted as an age
   order: quarantine_corrupt_file reuses a pruned slot.
7. OSError IS NOT CORRUPTION -- an unreadable (EACCES) settings file keeps
   the old behavior: {} returned, file left exactly where it is, no sidecar.
   Quarantining there would move a perfectly healthy file out of the way.
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
    """Truncated mid-token, but identifiable -- the sidecar's content is what
    proves WHICH corruption was preserved."""
    payload = f'{{"file-version": "2.0", "marker": "{marker}", "settings": {{"a'
    with open(path, "w") as f:
        f.write(payload)
    return payload.encode()


def sidecars(path: str) -> list[str]:
    """Every quarantine sidecar of `path`, by name."""
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

    # Clean afterwards: the plugin is usable again, no stale corruption.
    plugin.set_settings({"marker": "after-corruption"})
    assert plugin.get_settings() == {"marker": "after-corruption"}, (
        f"plugin could not save/read after quarantine: {plugin.get_settings()}"
    )
    print("PASS(1): corrupt plugin settings quarantined, fallback returned, next load clean")


def check_set_settings_preserves_before_overwrite(plugin) -> None:
    path = plugin.settings_path
    bad = corrupt(path, "second")

    # THE data-loss moment: this write replaces the file wholesale.
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
    """Five successive corruptions of `path` must leave exactly three
    sidecars, the oldest pruned. `corrupting_read` is the loader call that
    quarantines -- one per wired call site."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for n in range(1, 6):
        corrupt(path, f"gen{n}")
        got = corrupting_read()
        assert got == {}, f"{label}: corrupt read must fall back to empty, got {got}"
        # Distinct mtimes: the sidecar inherits the corrupt primary's mtime,
        # which is what the oldest-first prune orders by.
        time.sleep(0.01)

    names = sidecars(path)
    assert len(names) == 3, f"{label}: 5 corruptions must leave exactly 3 sidecars, got {names}"
    survived = {read(os.path.join(os.path.dirname(path), n)) for n in names}
    markers = sorted(s.split('"marker": "')[1].split('"')[0] for s in survived)
    assert markers == ["gen3", "gen4", "gen5"], (
        f"{label}: retention must keep the NEWEST three and prune oldest-first, kept {markers}"
    )
    print(f"PASS(4): .corrupt sidecars are retention-bounded, oldest pruned -- {label}")


def check_oserror_is_not_corruption(plugin) -> None:
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

    # The manifest lives in the plugin's SOURCE tree, which the app never
    # writes -- so it must be left exactly where it is. Renaming it aside
    # would mutate the developer's git working tree (dev plugins are symlinks
    # into ~/dev): a manifest corrupt mid-rebase would show up as a DELETED
    # file. Nothing overwrites it either, so there is nothing to protect.
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

    got = plugin.get_about()  # used to raise straight into the about window

    assert got == {}, f"a corrupt about.json must degrade to the missing-file result, got {got}"
    # Source file again: log, do not touch.
    assert os.path.isfile(about_path) and read(about_path) == '{"copyright": "me"'
    assert sidecars(about_path) == [], (
        f"a plugin SOURCE file must never be quarantined: {os.listdir(plugin.PATH)}"
    )

    # Valid JSON that is not an object used to reach PluginAbout and raise
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

    # Real SettingsManager + PageManagerBackend: the page loader delegates its
    # corrupt-read handling to SettingsManager, so this is how the second
    # wired call site gets exercised end to end.
    fixtures._install_integration_globals()

    write_plugin("com_test_q_settings", "QSettingsPlugin", manifest_json("com_test_q_settings"))
    write_plugin("com_test_q_retention", "QRetentionPlugin", manifest_json("com_test_q_retention"))
    write_plugin("com_test_q_oserror", "QOsErrorPlugin", manifest_json("com_test_q_oserror"))
    # Corrupt manifest vs. no manifest at all -- the two must be indistinguishable.
    corrupt_dir = write_plugin("com_test_q_manifest_corrupt", "QCorruptManifestPlugin",
                               '{"name": "x", "id": ')
    missing_dir = write_plugin("com_test_q_manifest_missing", "QMissingManifestPlugin", None)

    pm = PluginManager()
    gl.plugin_manager = pm
    pm.load_plugins()

    check_manifest_degrades_like_missing(pm, PluginBase, corrupt_dir, missing_dir)

    settings_plugin = PluginBase.plugins["com_test_q_settings"]["object"]
    check_settings_quarantine(settings_plugin)
    check_set_settings_preserves_before_overwrite(settings_plugin)
    check_prior_sidecar_not_clobbered(settings_plugin)
    check_about_degrades_to_empty(settings_plugin)

    check_oserror_is_not_corruption(PluginBase.plugins["com_test_q_oserror"]["object"])

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

    print("PASS: scenario_plugin_settings_quarantine")


if __name__ == "__main__":
    main()
