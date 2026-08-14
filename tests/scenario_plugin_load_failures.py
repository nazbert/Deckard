"""
The plugin load path survives broken plugins and records every failure.

One poison plugin must not abort the healthy ones, and each failure lands in
PluginManager.load_errors keyed by folder. The register() version gate
disables a plugin with a reason instead of raising, and include_disabled must
not leak disabled plugins into the enabled registry.
"""
import json
import os
import sys
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
    # A healthy plugin must register whatever its neighbors do.
    write_plugin("com_test_good", GOOD_MAIN.format(class_name="GoodPlugin"),
                 manifest("com_test_good"))

    # Poison at import time.
    write_plugin("com_test_poison_import",
                 'raise RuntimeError("poison: module-level crash")\n')

    # Poison in the constructor, before register().
    write_plugin("com_test_poison_init", """
        from src.backend.PluginManager.PluginBase import PluginBase

        class PoisonInitPlugin(PluginBase):
            def __init__(self):
                super().__init__()
                raise RuntimeError("poison: constructor crash")
    """)

    # Constructs, but register() bails because the manifest has no github
    # repo.
    write_plugin("com_test_no_register", GOOD_MAIN.format(class_name="NoRegisterPlugin"),
                 manifest("com_test_no_register", github=None))

    # A major-version mismatch with no minimum-app-version. A comparison
    # against None here raises TypeError out of register().
    write_plugin("com_test_old_major", GOOD_MAIN.format(class_name="OldMajorPlugin"),
                 manifest("com_test_old_major", **{"app-version": "0.9.0",
                                                   "minimum-app-version": None}))

    # Unparseable version metadata must disable the plugin.
    write_plugin("com_test_bad_version", GOOD_MAIN.format(class_name="BadVersionPlugin"),
                 manifest("com_test_bad_version", **{"app-version": "not-a-version"}))

    # A healthy plugin with a truncated settings.json.
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

    # A dotted directory name, as a timestamped backup makes, is unimportable
    # as plugins.<name>.main. The loader must skip it without an import
    # attempt and without inflating the failed count. The seed is a full copy
    # of a working plugin, like a real backup dir.
    write_plugin("com_test_good.bak.20260101-000000",
                 GOOD_MAIN.format(class_name="BackupPlugin"),
                 manifest("com_test_good_backup"))


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_plugin_load_failures")

    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.PluginManager.PluginManager import PluginManager
    from src.backend.notify import Notify

    seed_plugins()

    # main.create_global_objects installs this before the plugin load, and
    # the load-failure report goes through it.
    gl.notify = Notify()

    pm = PluginManager()
    gl.plugin_manager = pm
    assert gl.app is None, "harness precondition: no App -- toast must defer"
    pm.load_plugins(show_notification=True)

    # Healthy plugins register despite the poison neighbors.
    assert "com_test_good" in PluginBase.plugins, (
        f"healthy plugin must register despite poison neighbors; "
        f"registered={sorted(PluginBase.plugins)}"
    )
    assert "com_test_corrupt_settings" in PluginBase.plugins, (
        "a corrupt settings.json must not kill the plugin "
        f"(registered={sorted(PluginBase.plugins)})"
    )

    # Every failure mode is recorded, keyed by folder.
    for folder in ("com_test_poison_import", "com_test_poison_init", "com_test_no_register"):
        assert folder in pm.load_errors, (
            f"{folder} must be recorded in load_errors, got {pm.load_errors}"
        )
        assert folder not in PluginBase.plugins, f"{folder} must not be registered"
    assert "stray-file.txt" not in pm.load_errors, (
        "a stray file in PLUGIN_DIR is not a plugin failure"
    )
    # Dotted backup dirs are skipped, not failed.
    assert "com_test_good.bak.20260101-000000" not in pm.load_errors, (
        "a dotted (unimportable) directory must not land in load_errors: "
        f"{pm.load_errors}"
    )
    assert not any("com_test_good.bak" in mod for mod in sys.modules), (
        "no import may even be attempted for a dotted plugin directory"
    )
    assert "com_test_good_backup" not in PluginBase.plugins, (
        "a backup dir must not register as a plugin"
    )

    # Version-gate outcomes land in disabled_plugins.
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

    # include_disabled must not leak into the enabled registry.
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

    # The failure toast defers while the app does not exist yet.
    assert len(gl.app_loading_finished_tasks) >= 1, (
        "load_plugins(show_notification=True) with failures must queue a "
        "deferred notification task"
    )

    # The health counts feed the Add-Action empty state.
    n_failed, n_disabled = pm.get_load_health()
    assert n_failed == 3, f"expected 3 failed plugins, got {n_failed} ({pm.load_errors})"
    assert n_disabled == 2, f"expected 2 disabled plugins, got {n_disabled}"

    # get_load_health must never observe a half-built load_errors while a
    # store-install reload rebuilds it on a background thread. The main thread
    # reads get_load_health for the Add-Action empty state, and the lock makes
    # the rebuild atomic against that read.
    import threading

    stop = threading.Event()
    reader_error: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                n_failed, n_disabled = pm.get_load_health()
                # load_errors holds only the seeded broken folders, so the
                # count stays between 0 and the seeded total. A mid-rebuild
                # dict gives a torn value.
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

    # get_load_health must serialize its read against the load_errors rebuild
    # through _load_errors_lock. A store-install reload on a background thread
    # would otherwise rebuild the dict under a main-thread reader. Hold the
    # lock and prove the reader blocks until it is released.
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
        # The reader must not return while the lock is held.
        assert not returned.wait(timeout=0.5), (
            "get_load_health() returned while the load_errors lock was held "
            "-- the read is not serialized against the rebuild"
        )
    assert returned.wait(timeout=5), "get_load_health() never completed after lock release"
    t.join(timeout=5)

    # An uninstalled plugin's error is pruned on the next load.
    import shutil
    shutil.rmtree(os.path.join(gl.PLUGIN_DIR, "com_test_poison_import"))
    pm.load_plugins()
    assert "com_test_poison_import" not in pm.load_errors, (
        "errors for uninstalled plugins must be pruned on the next load"
    )
    assert "com_test_poison_init" in pm.load_errors, (
        "errors for still-broken plugins must survive a reload"
    )

    # A hot install that lands version-disabled must notify in that session.
    # install_plugin's reload runs the register() gate, and a log-only disable
    # would leave the next launch's startup toast as the first feedback.
    # gl.app is faked only now, because the deferral assertions above need it
    # None.
    import types
    from gi.repository import GLib
    from src.backend.Store.StoreBackend import StoreBackend

    notifications = []
    gl.app = types.SimpleNamespace(
        send_notification=lambda icon, title, body, **kw: notifications.append((title, body))
    )
    try:
        notified = StoreBackend.notify_if_installed_disabled("com_test_old_major")
        ctx = GLib.MainContext.default()
        while ctx.pending():
            ctx.iteration(False)
        assert notified, (
            "installing a version-disabled plugin must notify in the install "
            "session, not first on the next launch"
        )
        assert len(notifications) == 1, f"expected one notification, got {notifications}"
        assert "older version" in notifications[0][1], (
            f"the notification must explain the plugin-out-of-date reason: "
            f"{notifications[0]}"
        )

        # A healthy registered plugin must not notify.
        assert not StoreBackend.notify_if_installed_disabled("com_test_good")
        # An id in neither registry must not notify either.
        assert not StoreBackend.notify_if_installed_disabled("com_test_never_existed")
        while ctx.pending():
            ctx.iteration(False)
        assert len(notifications) == 1, (
            f"healthy/unknown plugins must not produce disable notifications: "
            f"{notifications}"
        )
    finally:
        gl.app = None

    # remove_plugin_from_list must handle a plugin that lives only in
    # disabled_plugins. get_plugin_by_id defaults to include_disabled=True, so
    # uninstall_plugin hands it version-gated plugins too. A KeyError there
    # aborts the deregister before the sys.modules purge, and the updated
    # plugin keeps serving its old code.
    disabled_plugin = pm.get_plugin_by_id("com_test_old_major", include_disabled=True)
    assert disabled_plugin is not None
    pm.remove_plugin_from_list(disabled_plugin)  # must not raise
    assert "com_test_old_major" not in PluginBase.disabled_plugins, (
        "deregistering a disabled plugin must remove its disabled_plugins entry"
    )
    assert "com_test_old_major" not in PluginBase.plugins

    enabled_plugin = pm.get_plugin_by_id("com_test_good")
    assert enabled_plugin is not None
    pm.remove_plugin_from_list(enabled_plugin)
    assert "com_test_good" not in PluginBase.plugins, (
        "deregistering an enabled plugin must still remove it from plugins"
    )

    print("scenario_plugin_load_failures: PASS")


if __name__ == "__main__":
    main()
