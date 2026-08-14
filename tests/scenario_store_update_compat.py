"""
Auto-update must not replace an installed plugin with an incompatible build.
"""

# When no compatible version exists, prepare_plugin pins the newest
# incompatible commit and marks the entry is_compatible False, so the store can
# still list it. get_plugins_to_update skips and reports such an entry, and
# get_install_state_for reads it as installed rather than update-available.

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Ok, Err
from src.windows.Store.StoreData import PluginData


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    return sb


def _catalog() -> list[PluginData]:
    return [
        PluginData(github="https://github.com/a/uptodate", plugin_id="com_a_UpToDate",
                   local_sha="aaa", commit_sha="aaa", is_compatible=True),
        PluginData(github="https://github.com/b/outdated", plugin_id="com_b_Outdated",
                   local_sha="old", commit_sha="new", is_compatible=True),
        PluginData(github="https://github.com/c/incompat", plugin_id="com_c_Incompat",
                   local_sha="old", commit_sha="next-major", is_compatible=False),
        PluginData(github="https://github.com/d/notinstalled", plugin_id="com_d_NotInstalled",
                   local_sha=None, commit_sha="xyz", is_compatible=False),
    ]


def test_get_plugins_to_update_skips_incompatible() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    def fake_get_all_plugins(include_images: bool = True):
        return Ok(_catalog())

    sb.get_all_plugins = fake_get_all_plugins

    to_update = sb.get_plugins_to_update()
    assert not isinstance(to_update, Err)
    ids = [p.plugin_id for p in to_update.value]
    assert ids == ["com_b_Outdated"], (
        f"only the compatibly-outdated plugin may be offered for update, got {ids}"
    )


def test_update_all_plugins_never_installs_incompatible() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    def fake_get_all_plugins(include_images: bool = True):
        return Ok(_catalog())

    uninstalled: list[str] = []
    installed: list[str] = []

    def fake_uninstall(plugin_id, remove_from_pages=False, remove_files=True):
        uninstalled.append(plugin_id)

    def fake_install(plugin_data, auto_update=False):
        installed.append(plugin_data.plugin_id)
        return Ok(None)

    sb.get_all_plugins = fake_get_all_plugins
    sb.uninstall_plugin = fake_uninstall
    sb.install_plugin = fake_install

    result = sb.update_all_plugins()
    assert isinstance(result, Ok) and result.value == 1, (
        f"exactly the one compatible update may be counted, got {result!r}"
    )
    assert installed == ["com_b_Outdated"], (
        f"the incompatible plugin must never be installed, got installs {installed}"
    )
    assert uninstalled == [], (
        "update_all_plugins must not deregister anything itself -- "
        f"install_plugin deregisters only after a good download, got {uninstalled}"
    )


def test_install_state_for_incompatible_update_reads_installed() -> None:
    """The store UI derives the install button from the same verdict. An
    installed plugin whose only newer pinned version is incompatible reads
    as installed, never as update-available."""
    from src.windows.Store.Plugins.PluginPage import PluginPreview

    state_for = PluginPreview.get_install_state_for

    not_installed, outdated, incompat, _ = (
        _catalog()[3], _catalog()[1], _catalog()[2], None,
    )
    up_to_date = _catalog()[0]

    assert state_for(not_installed) == 0
    assert state_for(up_to_date) == 1
    assert state_for(outdated) == 2
    assert state_for(incompat) == 1, (
        "an installed plugin pinned to an INCOMPATIBLE newer version must "
        "show as installed, not offer the incompatible update"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_update_compat")
    test_get_plugins_to_update_skips_incompatible()
    test_update_all_plugins_never_installs_incompatible()
    test_install_state_for_incompatible_update_reads_installed()
    print("scenario_store_update_compat: PASS")


if __name__ == "__main__":
    main()
