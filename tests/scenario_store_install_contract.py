"""
Regression test for gl#26 -- install/update results ignored across the store
backend, exercised WITHOUT network:

- install_plugin's failure returns (404/400 ints, NoConnectionError) were
  discarded by the UI and by update_all_plugins, which deregistered the old
  version BEFORE the fallible download and returned len(plugins_to_update)
  -- failures counted as successes in the "assets updated" toast, and a
  failed update left the (still on-disk) plugin unregistered until restart.
- update_everything checked only the plugins/icons legs for
  NoConnectionError; a wallpapers-leg failure raised TypeError on the sum.

The contract is now: install_plugin success is exactly True;
download_repo/install_icon/install_wallpaper success is exactly 200;
update_all_* count only real successes; a failed plugin update triggers
reload_installed_plugins() so the old version keeps working.
"""
import asyncio

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError
from src.windows.Store.StoreData import PluginData, IconData


class RecordingPluginManager:
    def __init__(self):
        self.calls = []

    def load_plugins(self): self.calls.append("load_plugins")
    def init_plugins(self): self.calls.append("init_plugins")
    def generate_action_index(self): self.calls.append("generate_action_index")
    def get_plugins(self): return {}


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    return sb


def test_install_plugin_failure_propagates_and_skips_reload() -> None:
    fixtures.install_stub_globals()
    plugin_manager = RecordingPluginManager()
    gl.plugin_manager = plugin_manager

    sb = _make_backend()

    async def download_nce(**kwargs):
        return NoConnectionError()

    async def download_404(**kwargs):
        return 404

    data = PluginData(github="https://github.com/test/test", plugin_id="com_test_Plugin")

    sb.download_repo = download_nce
    result = asyncio.run(sb.install_plugin(data))
    assert isinstance(result, NoConnectionError), (
        f"failed download must propagate, got {result!r}"
    )

    sb.download_repo = download_404
    result = asyncio.run(sb.install_plugin(data))
    assert result == 404, f"hard download failure must return 404, got {result!r}"

    assert plugin_manager.calls == [], (
        f"a failed install must never reload/reinit plugins over a missing "
        f"tree, got {plugin_manager.calls}"
    )


def test_update_all_plugins_counts_only_successes_and_recovers() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    plugin_ok = PluginData(github="https://github.com/a/a", plugin_id="com_a_Ok")
    plugin_bad = PluginData(github="https://github.com/b/b", plugin_id="com_b_Bad")

    async def fake_get_plugins_to_update():
        return [plugin_ok, plugin_bad]

    uninstalled = []

    def fake_uninstall(plugin_id, remove_from_pages=False, remove_files=True):
        uninstalled.append((plugin_id, remove_files))

    async def fake_install(plugin_data, auto_update=False):
        return True if plugin_data is plugin_ok else NoConnectionError()

    recovery_calls = []

    sb.get_plugins_to_update = fake_get_plugins_to_update
    sb.uninstall_plugin = fake_uninstall
    sb.install_plugin = fake_install
    sb.reload_installed_plugins = lambda: recovery_calls.append(True)

    n = asyncio.run(sb.update_all_plugins())
    assert n == 1, f"only the ONE successful update may be counted, got {n!r}"
    assert uninstalled == [("com_a_Ok", False), ("com_b_Bad", False)], (
        "updates must deregister with remove_files=False"
    )
    assert recovery_calls == [True], (
        "a failed update must re-register the still-on-disk plugins "
        f"(exactly once), got {len(recovery_calls)} recovery calls"
    )

    # All successes: no recovery.
    async def install_all_ok(plugin_data, auto_update=False):
        return True

    recovery_calls.clear()
    sb.install_plugin = install_all_ok
    n = asyncio.run(sb.update_all_plugins())
    assert n == 2
    assert recovery_calls == [], "no recovery reload when every update succeeded"


def test_update_everything_checks_all_three_legs() -> None:
    sb = _make_backend()

    async def plugins_ok(): return 2
    async def icons_ok(): return 1
    async def wallpapers_fail(): return NoConnectionError()

    sb.update_all_plugins = plugins_ok
    sb.update_all_icons = icons_ok
    sb.update_all_wallpapers = wallpapers_fail

    result = asyncio.run(sb.update_everything())
    assert isinstance(result, NoConnectionError), (
        f"a wallpapers-leg failure must surface as NoConnectionError "
        f"(it used to TypeError on the sum), got {result!r}"
    )

    async def wallpapers_ok(): return 3
    sb.update_all_wallpapers = wallpapers_ok
    result = asyncio.run(sb.update_everything())
    assert result == 6


def test_update_all_icons_counts_only_successes() -> None:
    sb = _make_backend()

    icon_ok = IconData(github="https://github.com/a/icons", icon_id="com_a_Icons")
    icon_bad = IconData(github="https://github.com/b/icons", icon_id="com_b_Icons")

    async def fake_get_icons_to_update():
        return [icon_ok, icon_bad]

    async def fake_install_icon(icon_data):
        return 200 if icon_data is icon_ok else NoConnectionError()

    sb.get_icons_to_update = fake_get_icons_to_update
    sb.install_icon = fake_install_icon

    n = asyncio.run(sb.update_all_icons())
    assert n == 1, f"only the ONE successful icon update may be counted, got {n!r}"


def test_install_icon_propagates_download_result() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    async def download_ok(**kwargs):
        return 200

    async def download_fail(**kwargs):
        return NoConnectionError()

    data = IconData(github="https://github.com/a/icons", icon_id="com_a_Icons")

    sb.download_repo = download_ok
    assert asyncio.run(sb.install_icon(data)) == 200

    sb.download_repo = download_fail
    result = asyncio.run(sb.install_icon(data))
    assert isinstance(result, NoConnectionError), (
        f"install_icon must propagate the failed download, got {result!r}"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_install_contract")
    test_install_plugin_failure_propagates_and_skips_reload()
    test_update_all_plugins_counts_only_successes_and_recovers()
    test_update_everything_checks_all_three_legs()
    test_update_all_icons_counts_only_successes()
    test_install_icon_propagates_download_result()
    print("scenario_store_install_contract: PASS")


if __name__ == "__main__":
    main()
