"""
Regression test for "action list empty -- cannot add actions" (#118): the
plugin discovery/load path must survive broken plugins, and every failure
must be RECORDED (PluginManager.load_errors) instead of silently dropping
the plugin. Pins, without hardware or GTK widgets:

1. One poison plugin (import crash, constructor crash, invalid manifest)
   must not abort loading of the healthy plugins.
2. Every failure mode lands in PluginManager.load_errors with the plugin's
   folder as the key -- the source for the startup toast and the Add-Action
   dialog's empty state.
3. The PluginBase.register() version gate must not CRASH on version
   metadata: a plugin whose manifest has a major-version mismatch but no
   minimum-app-version used to raise TypeError (None > Version) out of the
   plugin's __init__, making the plugin vanish entirely. It must land in
   PluginBase.disabled_plugins with a reason instead.
4. A corrupt per-plugin settings.json (e.g. truncated by a crash) must not
   kill the plugin: AssetManager.load_assets()/get_settings() run inside
   PluginBase.__init__/register().
5. get_plugins(include_disabled=True) must not mutate PluginBase.plugins in
   place -- get_plugin_by_id() defaults to include_disabled=True and runs on
   every page-load action resolution, so the old aliasing bug leaked every
   disabled plugin into the enabled registry (and the action index) on
   first hit.
6. Failures are surfaced: load_plugins(show_notification=True) before the
   app exists defers the "N plugins failed to load" toast via
   gl.app_loading_finished_tasks.
"""
import json
import os
import textwrap

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: E402


def write_plugin(folder: str, main_py: str, manifest: dict | None = None) -> None:
    plugin_dir = os.path.join(gl.PLUGIN_DIR, folder)
    os.makedirs(plugin_dir, exist_ok=True)
    with open(os.path.join(plugin_dir, "main.py"), "w") as f:
        f.write(textwrap.dedent(main_py))
    if manifest is not None:
        with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f)


GOOD_MAIN = """
    from src.backend.PluginManager.PluginBase import PluginBase

    class {class_name}(PluginBase):
        def __init__(self):
            super().__init__()
            self.register()
"""


def manifest(plugin_id: str, **overrides) -> dict:
    base = {
        "name": plugin_id,
        "id": plugin_id,
        "github": f"https://github.com/example/{plugin_id}",
        "version": "1.0.0",
        "app-version": "1.5.0",
        "minimum-app-version": "1.0.0",
    }
    base.update(overrides)
    # Explicit None means "field absent from the manifest".
    return {k: v for k, v in base.items() if v is not None}


def seed_plugins() -> None:
    # Healthy plugin: must register no matter what its neighbors do.
    write_plugin("com_test_good", GOOD_MAIN.format(class_name="GoodPlugin"),
                 manifest("com_test_good"))

    # Poison at import time.
    write_plugin("com_test_poison_import",
                 'raise RuntimeError("poison: module-level crash")\n')

    # Poison in the constructor (before register()).
    write_plugin("com_test_poison_init", """
        from src.backend.PluginManager.PluginBase import PluginBase

        class PoisonInitPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                raise RuntimeError("poison: constructor crash")
    """)

    # Constructs fine but register() bails (no github repo): used to vanish
    # without any record.
    write_plugin("com_test_no_register", GOOD_MAIN.format(class_name="NoRegisterPlugin"),
                 manifest("com_test_no_register", github=None))

    # Major-version mismatch WITHOUT minimum-app-version: used to raise
    # TypeError (None > Version) out of register() -> plugin vanished.
    write_plugin("com_test_old_major", GOOD_MAIN.format(class_name="OldMajorPlugin"),
                 manifest("com_test_old_major", **{"app-version": "0.9.0",
                                                   "minimum-app-version": None}))

    # Unparseable version metadata: must disable, not crash.
    write_plugin("com_test_bad_version", GOOD_MAIN.format(class_name="BadVersionPlugin"),
                 manifest("com_test_bad_version", **{"app-version": "not-a-version"}))

    # Healthy plugin with a corrupt settings.json (truncated write).
    write_plugin("com_test_corrupt_settings",
                 GOOD_MAIN.format(class_name="CorruptSettingsPlugin"),
                 manifest("com_test_corrupt_settings"))
    settings_dir = os.path.join(gl.DATA_PATH, "settings", "plugins", "com_test_corrupt_settings")
    os.makedirs(settings_dir, exist_ok=True)
    with open(os.path.join(settings_dir, "settings.json"), "w") as f:
        f.write('{"file-version": "2.0", "settings": {"first-se')  # truncated

    # A stray file in the plugin dir is not a plugin and must not be
    # counted as a failure.
    with open(os.path.join(gl.PLUGIN_DIR, "stray-file.txt"), "w") as f:
        f.write("not a plugin")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_plugin_load_failures")

    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.PluginManager.PluginManager import PluginManager

    seed_plugins()

    pm = PluginManager()
    gl.plugin_manager = pm
    assert gl.app is None, "harness precondition: no App -- toast must defer"
    pm.load_plugins(show_notification=True)

    # --- 1+4: healthy plugins registered despite the poison neighbors. ---
    assert "com_test_good" in PluginBase.plugins, (
        f"healthy plugin must register despite poison neighbors; "
        f"registered={sorted(PluginBase.plugins)}"
    )
    assert "com_test_corrupt_settings" in PluginBase.plugins, (
        "a corrupt settings.json must not kill the plugin "
        f"(registered={sorted(PluginBase.plugins)})"
    )

    # --- 2: every failure mode is recorded, keyed by folder. ---
    for folder in ("com_test_poison_import", "com_test_poison_init", "com_test_no_register"):
        assert folder in pm.load_errors, (
            f"{folder} must be recorded in load_errors, got {pm.load_errors}"
        )
        assert folder not in PluginBase.plugins, f"{folder} must not be registered"
    assert "stray-file.txt" not in pm.load_errors, (
        "a stray file in PLUGIN_DIR is not a plugin failure"
    )

    # --- 3: version-gate outcomes land in disabled_plugins, not nowhere. ---
    assert "com_test_old_major" in PluginBase.disabled_plugins, (
        "major-mismatch plugin without minimum-app-version must be DISABLED "
        "(used to vanish via TypeError: None > Version); "
        f"disabled={sorted(PluginBase.disabled_plugins)}, errors={pm.load_errors}"
    )
    assert PluginBase.disabled_plugins["com_test_old_major"]["reason"] == "plugin-out-of-date"
    assert "com_test_old_major" not in pm.load_errors, (
        "version-gated plugins are 'disabled', not 'failed' -- they have "
        "their own notification"
    )

    assert "com_test_bad_version" in PluginBase.disabled_plugins, (
        "unparseable version metadata must disable the plugin, not crash it away; "
        f"disabled={sorted(PluginBase.disabled_plugins)}, errors={pm.load_errors}"
    )
    assert PluginBase.disabled_plugins["com_test_bad_version"]["reason"] == "invalid-version"

    # --- 5: include_disabled must not leak into the enabled registry. ---
    disabled_probe = pm.get_plugin_by_id("com_test_old_major", include_disabled=True)
    assert disabled_probe is not None, "disabled plugin must be findable when asked for"
    assert "com_test_old_major" not in PluginBase.plugins, (
        "get_plugins(include_disabled=True) mutated PluginBase.plugins in "
        "place -- disabled plugins leaked into the enabled registry"
    )
    assert "com_test_old_major" not in pm.get_plugins(), (
        "get_plugins() (enabled-only) must not contain disabled plugins"
    )
    pm.generate_action_index()  # must not pick up disabled plugins either
    assert all(not k.startswith("com_test_old_major::") for k in pm.action_index), (
        "action index must not contain disabled plugins' actions"
    )

    # --- 6: failure toast deferred for the not-yet-running app. ---
    assert len(gl.app_loading_finished_tasks) >= 1, (
        "load_plugins(show_notification=True) with failures must queue a "
        "deferred notification task"
    )

    # --- health counts feed the Add-Action empty state. ---
    n_failed, n_disabled = pm.get_load_health()
    assert n_failed == 3, f"expected 3 failed plugins, got {n_failed} ({pm.load_errors})"
    assert n_disabled == 2, f"expected 2 disabled plugins, got {n_disabled}"

    # --- snapshot safety: get_load_health() must never observe a half-built
    # load_errors while a store-install reload (background thread) rebuilds it.
    # A store install runs load_plugins() off the GTK main thread; the main
    # thread reads get_load_health() for the Add-Action empty state. The lock
    # makes the rebuild atomic against that read. Hammer both concurrently and
    # assert the reader never raises and never sees a nonsensical count. ---
    import threading

    stop = threading.Event()
    reader_error: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                n_failed, n_disabled = pm.get_load_health()
                # load_errors only ever holds the seeded broken folders; the
                # count must stay within [0, seeded] -- never a torn/garbage
                # value from a mid-rebuild dict.
                assert 0 <= n_failed <= 8, f"torn load_errors read: {n_failed}"
                assert n_disabled >= 0
        except BaseException as e:  # noqa: BLE001 -- surface to the main thread
            reader_error.append(e)

    reader_thread = threading.Thread(target=reader, name="load_health_reader")
    reader_thread.start()
    try:
        for _ in range(50):
            pm.load_plugins()  # rebinds/rebuilds load_errors under the lock
    finally:
        stop.set()
        reader_thread.join(timeout=10)
    assert not reader_thread.is_alive(), "load_health reader thread hung"
    assert not reader_error, f"get_load_health raced the reload rebuild: {reader_error[0]!r}"

    health_before = pm.get_load_health()
    assert health_before == pm.get_load_health(), "get_load_health must be stable at rest"

    # get_load_health() must serialize its read against the load_errors
    # rebuild via _load_errors_lock -- without it (the pre-fix code) a
    # store-install reload on a background thread could rebuild the dict
    # under a main-thread reader. Hold the lock and prove the reader blocks
    # until it is released (a read that ran lock-free would return early).
    assert hasattr(pm, "_load_errors_lock"), (
        "load_errors reads/writes must be guarded by a lock (cross-thread "
        "store-install reload vs main-thread get_load_health)"
    )
    blocked = threading.Event()
    returned = threading.Event()

    def blocked_reader() -> None:
        blocked.set()
        pm.get_load_health()  # must not complete until the lock is free
        returned.set()

    with pm._load_errors_lock:
        t = threading.Thread(target=blocked_reader, name="blocked_health_reader")
        t.start()
        assert blocked.wait(timeout=5), "reader thread never started"
        # While we hold the lock, the reader must NOT have returned.
        assert not returned.wait(timeout=0.5), (
            "get_load_health() returned while the load_errors lock was held "
            "-- the read is not serialized against the rebuild"
        )
    assert returned.wait(timeout=5), "get_load_health() never completed after lock release"
    t.join(timeout=5)

    # --- pruning: a removed (uninstalled) plugin's error is dropped. ---
    import shutil
    shutil.rmtree(os.path.join(gl.PLUGIN_DIR, "com_test_poison_import"))
    pm.load_plugins()
    assert "com_test_poison_import" not in pm.load_errors, (
        "errors for uninstalled plugins must be pruned on the next load"
    )
    assert "com_test_poison_init" in pm.load_errors, (
        "errors for still-broken plugins must survive a reload"
    )

    print("scenario_plugin_load_failures: PASS")


if __name__ == "__main__":
    main()
