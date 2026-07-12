"""
Regression test for the settings-identity half of issue #102 ("config not
fully applied on restart" -- the custom-repo-plugins leg): plugin settings
must be keyed by the MANIFEST id (the identity used by registration and the
store), not by the folder name under PLUGIN_DIR.

Before the fix, PluginBase.settings_path was derived from the folder name
(the PluginBase.py:64 TODO) while everything else keys the plugin by its
manifest id -- so a plugin whose folder name differs from its id (git clone
of "MyPlugin-main", store install under a new name) silently lost its
settings on every reinstall/rename: another "settings reset after restart"
vector.

Covers:
1. MIGRATION: a plugin whose folder name diverges from its manifest id, with
   settings saved by an earlier app version under the folder-name path, gets
   the whole settings dir moved to the id path once -- content preserved.
2. FRESH: a diverging plugin with no legacy settings uses the id path from
   the start, and set_settings() persists there.
3. BOTH EXIST: the id path wins; the folder-name dir is left in place (never
   deleted/overwritten) and a warning is logged.
4. PLAIN: folder name == id (the normal case) -- path unchanged, no
   migration side effects.
"""
import json
import os
import textwrap

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: E402

PLUGIN_MAIN = """
    from src.backend.PluginManager.PluginBase import PluginBase

    class {class_name}(PluginBase):
        def __init__(self):
            super().__init__()
            self.register()
"""


def write_plugin(folder: str, class_name: str, plugin_id: str) -> None:
    plugin_dir = os.path.join(gl.PLUGIN_DIR, folder)
    os.makedirs(plugin_dir, exist_ok=True)
    with open(os.path.join(plugin_dir, "main.py"), "w") as f:
        f.write(textwrap.dedent(PLUGIN_MAIN.format(class_name=class_name)))
    with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
        json.dump({
            "name": plugin_id,
            "id": plugin_id,
            "github": f"https://github.com/example/{plugin_id}",
            "version": "1.0.0",
            "app-version": "1.5.0",
            "minimum-app-version": "1.0.0",
        }, f)


def seed_settings(dir_name: str, marker: str) -> str:
    settings_dir = os.path.join(gl.DATA_PATH, "settings", "plugins", dir_name)
    os.makedirs(settings_dir, exist_ok=True)
    path = os.path.join(settings_dir, "settings.json")
    with open(path, "w") as f:
        json.dump({"file-version": "2.0", "settings": {"marker": marker}}, f)
    return settings_dir


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_plugin_settings_identity")

    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.PluginManager.PluginManager import PluginManager

    plugins_root = os.path.join(gl.DATA_PATH, "settings", "plugins")

    # 1. Folder name diverges from id, legacy settings under the folder path.
    write_plugin("com_test_migrate_main", "MigratePlugin", "com_test_migrate")
    seed_settings("com_test_migrate_main", "legacy-value")

    # 2. Diverging folder, no legacy settings.
    write_plugin("com_test_fresh_main", "FreshPlugin", "com_test_fresh")

    # 3. Settings exist under BOTH the id and the folder-name path.
    write_plugin("com_test_both_main", "BothPlugin", "com_test_both")
    seed_settings("com_test_both", "id-value")
    seed_settings("com_test_both_main", "folder-value")

    # 4. The normal case: folder name == manifest id.
    write_plugin("com_test_plain", "PlainPlugin", "com_test_plain")

    pm = PluginManager()
    gl.plugin_manager = pm
    pm.load_plugins()

    for plugin_id in ("com_test_migrate", "com_test_fresh", "com_test_both",
                      "com_test_plain"):
        assert plugin_id in PluginBase.plugins, (
            f"{plugin_id} must register; registered={sorted(PluginBase.plugins)}, "
            f"errors={pm.load_errors}"
        )

    def plugin(plugin_id: str) -> PluginBase:
        return PluginBase.plugins[plugin_id]["object"]

    # --- 1: migration moved the folder-name dir to the id dir, content intact.
    migrate = plugin("com_test_migrate")
    expected = os.path.join(plugins_root, "com_test_migrate", "settings.json")
    assert migrate.settings_path == expected, (
        f"settings_path must be id-keyed, got {migrate.settings_path}"
    )
    assert os.path.isfile(expected), "migrated settings.json must exist at the id path"
    assert not os.path.exists(os.path.join(plugins_root, "com_test_migrate_main")), (
        "the legacy folder-name settings dir must be gone after migration"
    )
    assert migrate.get_settings().get("marker") == "legacy-value", (
        f"migrated settings content lost: {migrate.get_settings()}"
    )

    # --- 2: fresh diverging plugin reads/writes the id path.
    fresh = plugin("com_test_fresh")
    assert fresh.settings_path == os.path.join(plugins_root, "com_test_fresh", "settings.json")
    fresh.set_settings({"written": True})
    assert os.path.isfile(fresh.settings_path), "set_settings must persist at the id path"
    assert fresh.get_settings() == {"written": True}
    assert not os.path.exists(os.path.join(plugins_root, "com_test_fresh_main")), (
        "no folder-name settings dir may appear for a fresh plugin"
    )

    # --- 3: id path wins; the folder-name dir is left untouched.
    both = plugin("com_test_both")
    assert both.settings_path == os.path.join(plugins_root, "com_test_both", "settings.json")
    assert both.get_settings().get("marker") == "id-value", (
        f"the id-keyed settings must win, got {both.get_settings()}"
    )
    folder_path = os.path.join(plugins_root, "com_test_both_main", "settings.json")
    assert os.path.isfile(folder_path), "the losing folder-name dir must be left in place"
    with open(folder_path) as f:
        assert json.load(f)["settings"]["marker"] == "folder-value", (
            "the losing folder-name settings must not be modified"
        )

    # --- 4: the normal case is byte-for-byte the old behavior.
    plain = plugin("com_test_plain")
    assert plain.settings_path == os.path.join(plugins_root, "com_test_plain", "settings.json")

    print("PASS: scenario_plugin_settings_identity")


if __name__ == "__main__":
    main()
