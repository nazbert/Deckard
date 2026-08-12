"""
Plugin settings on the settings store: ONE heal, the envelope kept, and an
unreadable file that no longer takes the settings UI down with it.

Plugin settings used to carry a second, parallel copy of the loader's
corrupt-file handling -- one in PluginBase, one reaching back into it from the
asset manager -- while the app's other settings files were healed by the store.
Two implementations of one policy is two policies waiting to disagree, and they
already had: the asset manager's SAVE path guarded a decode failure but not a
READ failure, so a settings file that was present but unreadable (permissions,
a dead mount) raised out of five live plugin-settings UI paths.

Pins, all against the real code paths (no GTK window, no hardware):

1. ONE LOADER -- every one of the four entry points (get_settings,
   set_settings, load_assets, save_assets) quarantines a corrupt file through
   the STORE's quarantine, exactly once per corrupt read. Intercepting the
   store's own quarantine intercepts all four, which is only true if there is
   one implementation left; the parallel one is gone from both modules.
2. UNREADABLE IS NOT FATAL, AND IS NOT CORRUPTION -- a present-but-unreadable
   settings file makes every one of the four return empty and log, never
   raise, and is never quarantined (moving a healthy file aside over a
   transient EACCES is how a plugin loses settings nobody can get back).
   save_assets is the one that used to raise.
3. THE ENVELOPE IS THE APP'S, THE KEYS ARE THE PLUGIN'S -- reads unwrap
   `settings`, a pre-envelope file is migrated on first read (including an
   EMPTY one, which is a real file), an absent or corrupt one is NOT
   materialized by a read, and a write keeps whatever else the file holds.
4. THE ASSET OVERRIDES SHARE THAT FILE -- settings and assets survive each
   other's writes in both orders.
5. SERIALIZED, AND ONE LOCK ORDER -- the per-plugin lock still makes each
   accessor's own read-modify-write of the file indivisible (never two at
   once), and the plugin path takes that lock plus the store's leaf cache lock
   and nothing else: never the store's per-file edit lock, so the one nesting
   that exists has one direction. The surface stays uncached, too -- a
   plugin's settings never enter the shared content cache.

The asset manager's two methods deliberately do NOT take that lock, exactly as
before: they run their overrides through observer callbacks, and a callback
that reached back into get_settings under a plain (non-reentrant) lock would
deadlock the plugin rather than protect it.
"""
import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import contextlib
import json
import os
import threading
import time
from unittest import mock

import globals as gl  # noqa: E402

PLUGIN_ID = "com_test_settings_store"


def settings_path(plugin_id: str = PLUGIN_ID) -> str:
    path = os.path.join(gl.DATA_PATH, "settings", "plugins", plugin_id, "settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def make_plugin(plugin_id: str = PLUGIN_ID):
    """A PluginBase with nothing but a settings file -- the settings accessors
    touch nothing else, and building one for real needs a plugin tree."""
    from src.backend.PluginManager.PluginBase import PluginBase
    from src.backend.PluginManager.PluginSettings.PluginAssetManager import AssetManager

    plugin = object.__new__(PluginBase)
    plugin.settings_path = settings_path(plugin_id)
    # Both are what a real plugin carries, and a fake thinner than that turns
    # a diagnostic about this file into an AttributeError from inside it.
    plugin.plugin_name = plugin_id
    plugin.PATH = os.path.dirname(plugin.settings_path)
    return plugin, AssetManager(plugin)


def write_raw(path: str, text: str) -> str:
    with open(path, "w") as f:
        f.write(text)
    return text


def read_raw(path: str) -> str:
    with open(path) as f:
        return f.read()


def corrupt(path: str, marker: str) -> str:
    """Truncated mid-token, but identifiable: which bytes were preserved is
    what proves the corrupt file was not simply overwritten."""
    return write_raw(path, f'{{"file-version": "2.0", "marker": "{marker}", "settings": {{"a')


def sidecars(path: str) -> list[str]:
    directory = os.path.dirname(path)
    base = os.path.basename(path) + ".corrupt"
    return sorted(
        e for e in os.listdir(directory)
        if e == base or (e.startswith(base + ".") and e[len(base) + 1:].isdigit())
    )


@contextlib.contextmanager
def unreadable(path: str):
    """`path` present but unreadable -- the EACCES shape, restored after."""
    if os.geteuid() == 0:
        # root ignores the permission bits, so raise the same errno at the
        # syscall the loader makes instead.
        real_open = open

        def failing_open(file, *args, **kwargs):
            if str(file) == path:
                raise PermissionError(13, "simulated EACCES")
            return real_open(file, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=failing_open):
            yield
        return

    mode = os.stat(path).st_mode & 0o777
    os.chmod(path, 0o000)
    try:
        with open(path):
            raise AssertionError(f"{path} is still readable at mode 000 -- test setup is wrong")
    except PermissionError:
        pass
    try:
        yield
    finally:
        if os.path.exists(path):
            # atomic_write_json keeps an existing file's mode, so a write that
            # landed while it was unreadable left it that way.
            os.chmod(path, mode)


def check_one_loader() -> None:
    """The heal is the store's, for every entry point, and the second
    implementation is gone rather than merely unused."""
    from src.backend import settings_store
    from src.backend.PluginManager import PluginBase as plugin_base_module
    from src.backend.PluginManager.PluginSettings.PluginAssetManager import AssetManager

    assert not hasattr(plugin_base_module, "_quarantine_corrupt_json"), (
        "PluginBase still carries its own quarantine implementation"
    )
    assert not hasattr(AssetManager, "_quarantine"), (
        "the asset manager still carries its own quarantine implementation"
    )

    plugin, am = make_plugin("com_test_one_loader")
    path = plugin.settings_path
    calls: list[str] = []
    real_quarantine = settings_store.quarantine_corrupt_file

    def counting_quarantine(file_path: str):
        calls.append(file_path)
        return real_quarantine(file_path)

    # Patched at the STORE. On a tree with two implementations this intercepts
    # nothing the plugin does.
    with mock.patch.object(settings_store, "quarantine_corrupt_file", counting_quarantine):
        for label, drive in (
            ("get_settings", plugin.get_settings),
            ("set_settings", lambda: plugin.set_settings({"marker": "written"})),
            ("load_assets", am.load_assets),
            ("save_assets", am.save_assets),
        ):
            calls.clear()
            corrupt(path, label)
            drive()
            assert calls == [path], (
                f"{label} did not heal through the store's loader (calls={calls})"
            )

    # Four quarantines, and the retention bound the loader applies to its own
    # sidecars: three kept, oldest pruned.
    assert len(sidecars(path)) == 3, (
        f"one sidecar per corrupt read, retention-bounded, got {sidecars(path)}"
    )
    print("PASS(1): every plugin-settings entry point heals through the one loader")


def check_unreadable_is_not_fatal() -> None:
    """The gap: save_assets' read guarded a decode failure and not a read one,
    so an EACCES settings file raised out of the plugin settings UI."""
    from src.backend.PluginManager.PluginSettings.Asset import Color

    plugin, am = make_plugin("com_test_unreadable")
    path = plugin.settings_path
    plugin.set_settings({"marker": "healthy"})
    before = read_raw(path)
    assert sidecars(path) == [], "precondition: no sidecars yet"

    with unreadable(path):
        assert plugin.get_settings() == {}, "an unreadable file must read as empty settings"
        assert am.load_assets() == {}, "an unreadable file must read as no assets"
        assert sidecars(path) == [], (
            f"an unreadable file is not a corrupt one and must not be quarantined: "
            f"{sidecars(path)}"
        )

        # THE red proof: five UI paths reach this one.
        am.colors.add_override("accent", Color(color=(1, 2, 3, 4)), skip_asset_check=True)
        am.save_assets()

    assert sidecars(path) == [], "the unreadable file was quarantined after all"
    content = json.loads(read_raw(path))
    assert content["assets"]["colors"] == {"accent": [1, 2, 3, 4]}, (
        f"save_assets did not write the override it was given: {content}"
    )

    # A write over an unreadable file replaces it, which is what the settings
    # writer has always done -- what must not happen is the raise.
    with unreadable(path):
        plugin.set_settings({"marker": "rewritten"})
    assert plugin.get_settings() == {"marker": "rewritten"}
    assert before != read_raw(path)
    print("PASS(2): an unreadable settings file is survived by every entry point")


def check_envelope_round_trip() -> None:
    plugin, _am = make_plugin("com_test_envelope")
    path = plugin.settings_path

    # Absent: empty settings, and NOTHING written -- a read must not create the
    # file it did not find.
    assert plugin.get_settings() == {}
    assert not os.path.exists(path), "a read of an absent settings file created one"

    # Pre-envelope: the whole file was the settings. Migrated on first read,
    # and the settings themselves come back untouched.
    write_raw(path, json.dumps({"host": "10.0.0.2", "token": "abc"}))
    assert plugin.get_settings() == {"host": "10.0.0.2", "token": "abc"}
    assert json.loads(read_raw(path)) == {
        "file-version": "2.0",
        "settings": {"host": "10.0.0.2", "token": "abc"},
    }, f"the migrated file is not the current format: {read_raw(path)}"
    assert plugin.get_settings() == {"host": "10.0.0.2", "token": "abc"}, (
        "the second read of a migrated file lost the settings"
    )

    # An EMPTY object is a real pre-envelope file, not an absent one.
    write_raw(path, "{}")
    assert plugin.get_settings() == {}
    assert json.loads(read_raw(path)) == {"file-version": "2.0", "settings": {}}, (
        f"an empty pre-envelope file was not migrated: {read_raw(path)}"
    )

    # A write keeps what else the file holds...
    write_raw(path, json.dumps({"file-version": "2.0", "assets": {"colors": {}}, "settings": {}}))
    plugin.set_settings({"volume": 42})
    assert json.loads(read_raw(path)) == {
        "file-version": "2.0",
        "assets": {"colors": {}},
        "settings": {"volume": 42},
    }, f"set_settings dropped a sibling key: {read_raw(path)}"

    # ...and a pre-envelope file's own keys ARE the settings being replaced, so
    # the wrapping write is a replacement, as it has always been.
    write_raw(path, json.dumps({"volume": 1, "assets": {"colors": {}}}))
    plugin.set_settings({"volume": 2})
    assert json.loads(read_raw(path)) == {"file-version": "2.0", "settings": {"volume": 2}}

    # Corrupt: quarantined, empty settings, and no file conjured in its place.
    corrupt(path, "envelope")
    assert plugin.get_settings() == {}
    assert not os.path.exists(path), (
        "a corrupt file was quarantined and then re-created by the same read"
    )
    assert len(sidecars(path)) == 1

    # Valid JSON that is not an object: empty settings, left exactly where it
    # is -- it decoded, so there is nothing to preserve from a save.
    body = write_raw(path, '["not", "an", "object"]')
    assert plugin.get_settings() == {}
    assert read_raw(path) == body and len(sidecars(path)) == 1
    print("PASS(3): the file-version envelope round-trips, migrations included")


def check_assets_share_the_file() -> None:
    from src.backend.PluginManager.PluginSettings.Asset import Color

    plugin, am = make_plugin("com_test_shared_file")
    path = plugin.settings_path

    plugin.set_settings({"host": "10.0.0.2"})
    am.colors.add_override("accent", Color(color=(9, 8, 7, 6)), skip_asset_check=True)
    am.save_assets()
    assert plugin.get_settings() == {"host": "10.0.0.2"}, (
        f"save_assets clobbered the plugin's settings: {read_raw(path)}"
    )

    plugin.set_settings({"host": "10.0.0.3"})
    fresh_plugin, fresh_am = make_plugin("com_test_shared_file")
    fresh_am.load_assets()
    assert fresh_am.colors.get_asset("accent").get_values() == (9, 8, 7, 6), (
        f"set_settings clobbered the asset overrides: {read_raw(path)}"
    )
    assert fresh_plugin.get_settings() == {"host": "10.0.0.3"}
    print("PASS(4): plugin settings and asset overrides survive each other")


def check_serialized_and_lock_order() -> None:
    """The per-plugin lock makes each accessor's own read-modify-write of the
    FILE indivisible -- actions of one plugin run on_ready in parallel, and a
    save that read the document before another save wrote it would drop
    whatever that one added beside the settings.

    It does not span two calls: a caller's own get-mutate-set cycle is its own
    business, exactly as before."""
    from src.backend import settings_store

    plugin, _am = make_plugin("com_test_serialized")
    path = plugin.settings_path
    plugin.set_settings({})

    n = 12
    barrier = threading.Barrier(n)
    errors: list[Exception] = []
    counter_lock = threading.Lock()
    in_flight = 0
    peak = 0

    def observed(real):
        def wrapper(self, *args, **kwargs):
            nonlocal in_flight, peak
            with counter_lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                # Wide enough that unserialized cycles overlap: without the
                # lock every thread is inside this at once.
                time.sleep(0.01)
                return real(self, *args, **kwargs)
            finally:
                with counter_lock:
                    in_flight -= 1
        return wrapper

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            if i % 3 == 0:
                plugin.get_settings()
            else:
                plugin.set_settings({"writer": i})
        except Exception as e:
            # Reported on the main thread: an assert in here would only kill
            # this worker.
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,), name=f"w{i}") for i in range(n)]
    with mock.patch.object(settings_store.PluginSettings, "read",
                           observed(settings_store.PluginSettings.read)), \
         mock.patch.object(settings_store.PluginSettings, "write",
                           observed(settings_store.PluginSettings.write)):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert not errors, f"a concurrent settings accessor raised: {errors}"
    assert not any(t.is_alive() for t in threads), "a settings accessor wedged"
    assert peak == 1, (
        f"{peak} settings cycles ran on one plugin at once -- the per-plugin lock "
        f"no longer serializes them, and a save can drop what a concurrent save added"
    )

    final = json.loads(read_raw(path))
    assert final["file-version"] == "2.0" and final["settings"] in [
        {"writer": i} for i in range(n) if i % 3
    ], f"the file is not exactly one writer's settings: {final}"

    store = settings_store.get()
    resolved = os.path.realpath(path)
    assert resolved not in store._edit_locks, (
        "the plugin path took the store's per-file edit lock -- the documented "
        "order is the plugin's lock outside and the store's leaf cache lock "
        "inside, and a second lock in between is a second order to get wrong"
    )
    assert resolved not in store._cache, (
        "the plugin surface is uncached; a cached entry here would answer a "
        "plugin's backend process with content it did not write"
    )
    print("PASS(5): read-modify-write is serialized, and takes one lock order")


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_plugin_settings_store")

    check_one_loader()
    check_unreadable_is_not_fatal()
    check_envelope_round_trip()
    check_assets_share_the_file()
    check_serialized_and_lock_order()

    print("PASS: scenario_plugin_settings_store")


if __name__ == "__main__":
    main()
