"""
Regression test for install and update results across the store backend.

The four install_* entry points answer a StoreResult, Ok(None) on success and
an Err naming the failure otherwise. Each update_all_* narrows on Ok, never
on truthiness, and returns Ok(count) or propagates the Err. update_everything
returns Ok(sum) or the first leg's Err. No network is involved.
"""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Ok, Err, ErrReason
from src.windows.Store.StoreData import PluginData, IconData, SDPlusBarWallpaperData


class RecordingPluginManager:
    def __init__(self):
        self.calls = []

    def load_plugins(self): self.calls.append("load_plugins")
    def init_plugins(self): self.calls.append("init_plugins")
    def generate_action_index(self): self.calls.append("generate_action_index")
    def get_plugins(self): return {}


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    return sb


def test_install_plugin_failure_skips_reload() -> None:
    fixtures.install_stub_globals()
    plugin_manager = RecordingPluginManager()
    gl.plugin_manager = plugin_manager

    sb = _make_backend()

    def download_conn(**kwargs):
        return Err(ErrReason.NO_CONNECTION, "offline")

    def download_hard(**kwargs):
        return Err(ErrReason.INSTALL_FAILED, "hard failure")

    data = PluginData(github="https://github.com/test/test", plugin_id="com_test_Plugin")

    sb.download_repo = download_conn
    result = sb.install_plugin(data)
    assert isinstance(result, Err) and result.reason is ErrReason.NO_CONNECTION, (
        f"a failed download must propagate its Err, got {result!r}"
    )

    sb.download_repo = download_hard
    result = sb.install_plugin(data)
    assert isinstance(result, Err) and result.reason is ErrReason.INSTALL_FAILED, (
        f"a hard download failure must propagate its Err, got {result!r}"
    )

    assert plugin_manager.calls == [], (
        f"a failed install must never reload/reinit plugins over a missing "
        f"tree, got {plugin_manager.calls}"
    )


def test_update_all_plugins_counts_successes() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    plugin_ok = PluginData(github="https://github.com/a/a", plugin_id="com_a_Ok")
    plugin_bad = PluginData(github="https://github.com/b/b", plugin_id="com_b_Bad")

    def fake_get_plugins_to_update():
        return Ok([plugin_ok, plugin_bad])

    uninstalled = []

    def fake_uninstall(plugin_id, remove_from_pages=False, remove_files=True):
        uninstalled.append((plugin_id, remove_files))

    def fake_install(plugin_data, auto_update=False):
        return Ok(None) if plugin_data is plugin_ok else Err(ErrReason.NO_CONNECTION, "offline")

    sb.get_plugins_to_update = fake_get_plugins_to_update
    sb.uninstall_plugin = fake_uninstall
    sb.install_plugin = fake_install

    result = sb.update_all_plugins()
    assert isinstance(result, Ok) and result.value == 1, (
        f"only the ONE successful update may be counted, got {result!r}"
    )
    assert uninstalled == [], (
        "update_all_plugins must never deregister a plugin itself -- "
        "install_plugin deregisters only after a good download, "
        f"got {uninstalled}"
    )
    assert not hasattr(sb, "reload_installed_plugins"), (
        "the deregister-first recovery reload is gone with the "
        "transactional install -- nothing should resurrect it"
    )


def test_update_everything_checks_all_four_legs() -> None:
    sb = _make_backend()

    # The _update_all_assets app-action toasts success only on an Ok. This
    # mirrors that discriminant, so the check pins what the toast reads.
    def app_reads_failure(result) -> bool:
        return not isinstance(result, Ok)

    def plugins_ok(): return Ok(2)
    def icons_ok(): return Ok(1)
    def wallpapers_fail(): return Err(ErrReason.NO_CONNECTION, "offline")
    def sd_plus_ok(): return Ok(4)

    sb.update_all_plugins = plugins_ok
    sb.update_all_icons = icons_ok
    sb.update_all_wallpapers = wallpapers_fail
    sb.update_all_sd_plus_bar_wallpapers = sd_plus_ok

    result = sb.update_everything()
    assert isinstance(result, Err), (
        f"a wallpapers-leg failure must surface as an Err "
        f"(it used to TypeError on the sum), got {result!r}"
    )
    assert app_reads_failure(result), (
        "the app-action must read a one-leg failure as the failure-toast path"
    )

    def wallpapers_ok(): return Ok(3)
    sb.update_all_wallpapers = wallpapers_ok
    result = sb.update_everything()
    assert isinstance(result, Ok) and result.value == 10, (
        f"the sum must include the SD+ bar wallpapers leg (2+1+3+4), got {result!r}"
    )
    assert not app_reads_failure(result), (
        "all legs Ok must read as the success-toast path"
    )

    # An SD+ failure must surface too, through its own leg.
    def sd_plus_fail(): return Err(ErrReason.NO_CONNECTION, "offline")
    sb.update_all_sd_plus_bar_wallpapers = sd_plus_fail
    result = sb.update_everything()
    assert isinstance(result, Err), (
        f"an SD+-leg failure must surface as an Err, got {result!r}"
    )


def test_update_all_sd_plus_successes() -> None:
    sb = _make_backend()

    wp_ok = SDPlusBarWallpaperData(github="https://github.com/a/sdplus", id="com_a_SDPlus",
                                   local_sha="old", commit_sha="new")
    wp_bad = SDPlusBarWallpaperData(github="https://github.com/b/sdplus", id="com_b_SDPlus",
                                    local_sha="old", commit_sha="new")
    wp_current = SDPlusBarWallpaperData(github="https://github.com/c/sdplus", id="com_c_SDPlus",
                                        local_sha="same", commit_sha="same")
    wp_not_installed = SDPlusBarWallpaperData(github="https://github.com/d/sdplus", id="com_d_SDPlus",
                                              local_sha=None, commit_sha="new")

    def fake_get_all(*args, **kwargs):
        return Ok([wp_ok, wp_bad, wp_current, wp_not_installed])

    installed = []

    def fake_install(wallpaper_data):
        installed.append(wallpaper_data.id)
        return Ok(None) if wallpaper_data is wp_ok else Err(ErrReason.NO_CONNECTION, "offline")

    sb.get_all_sd_plus_bar_wallpapers = fake_get_all
    sb.install_sd_plus_bar_wallpaper = fake_install

    result = sb.update_all_sd_plus_bar_wallpapers()
    assert isinstance(result, Ok) and result.value == 1, (
        f"only the ONE successful SD+ update may be counted, got {result!r}"
    )
    assert installed == ["com_a_SDPlus", "com_b_SDPlus"], (
        f"exactly the outdated installed packs may be reinstalled, got {installed}"
    )

    # Catalog failure propagates.
    def fake_get_all_fail(*args, **kwargs):
        return Err(ErrReason.NO_CONNECTION, "offline")

    sb.get_all_sd_plus_bar_wallpapers = fake_get_all_fail
    result = sb.update_all_sd_plus_bar_wallpapers()
    assert isinstance(result, Err)


def test_update_all_icons_counts_only_successes() -> None:
    sb = _make_backend()

    icon_ok = IconData(github="https://github.com/a/icons", icon_id="com_a_Icons")
    icon_bad = IconData(github="https://github.com/b/icons", icon_id="com_b_Icons")

    def fake_get_icons_to_update():
        return Ok([icon_ok, icon_bad])

    def fake_install_icon(icon_data):
        return Ok(None) if icon_data is icon_ok else Err(ErrReason.NO_CONNECTION, "offline")

    sb.get_icons_to_update = fake_get_icons_to_update
    sb.install_icon = fake_install_icon

    result = sb.update_all_icons()
    assert isinstance(result, Ok) and result.value == 1, (
        f"only the ONE successful icon update may be counted, got {result!r}"
    )


def test_install_icon_propagates_download_result() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    def download_ok(**kwargs):
        return Ok(None)

    def download_fail(**kwargs):
        return Err(ErrReason.NO_CONNECTION, "offline")

    data = IconData(github="https://github.com/a/icons", icon_id="com_a_Icons")

    sb.download_repo = download_ok
    assert isinstance(sb.install_icon(data), Ok)

    sb.download_repo = download_fail
    result = sb.install_icon(data)
    assert isinstance(result, Err), (
        f"install_icon must propagate the failed download, got {result!r}"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_install_contract")
    test_install_plugin_failure_skips_reload()
    test_update_all_plugins_counts_successes()
    test_update_everything_checks_all_four_legs()
    test_update_all_sd_plus_successes()
    test_update_all_icons_counts_only_successes()
    test_install_icon_propagates_download_result()
    print("scenario_store_install_contract: PASS")


if __name__ == "__main__":
    main()
