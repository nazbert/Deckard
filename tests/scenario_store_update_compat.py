"""
Regression test for gl#23 -- auto-update could replace an installed plugin
with an incompatible version, exercised WITHOUT network:

When no compatible version exists, prepare_plugin pins the newest
INCOMPATIBLE commit and marks the entry is_compatible=False (so the store
can still list it in the incompatible section). get_plugins_to_update
compared only local_sha != commit_sha with no compatibility filter, so
startup auto-update (update_everything -> update_all_plugins) uninstalled a
working plugin and installed a build pinned for a different app major. The
store UI had the same hole: such plugins showed install state 2 ("update
available") and the update button ran the same unguarded install.

The contract is now: get_plugins_to_update skips (and reports) entries with
is_compatible False, and PluginPreview.get_install_state_for reads an
installed-but-incompatibly-outdated plugin as state 1 (installed), never 2.
"""
import asyncio

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError
from src.windows.Store.StoreData import PluginData


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
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

    async def fake_get_all_plugins_async(include_images: bool = True):
        return _catalog()

    sb.get_all_plugins_async = fake_get_all_plugins_async

    to_update = asyncio.run(sb.get_plugins_to_update())
    assert not isinstance(to_update, NoConnectionError)
    ids = [p.plugin_id for p in to_update]
    assert ids == ["com_b_Outdated"], (
        f"only the compatibly-outdated plugin may be offered for update, got {ids}"
    )


def test_update_all_plugins_never_installs_incompatible() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    async def fake_get_all_plugins_async(include_images: bool = True):
        return _catalog()

    uninstalled: list[str] = []
    installed: list[str] = []

    def fake_uninstall(plugin_id, remove_from_pages=False, remove_files=True):
        uninstalled.append(plugin_id)

    async def fake_install(plugin_data, auto_update=False):
        installed.append(plugin_data.plugin_id)
        return True

    sb.get_all_plugins_async = fake_get_all_plugins_async
    sb.uninstall_plugin = fake_uninstall
    sb.install_plugin = fake_install
    sb.reload_installed_plugins = lambda: None

    n = asyncio.run(sb.update_all_plugins())
    assert n == 1, f"exactly the one compatible update may be counted, got {n!r}"
    assert installed == ["com_b_Outdated"], (
        f"the incompatible plugin must never be installed, got installs {installed}"
    )
    assert uninstalled == ["com_b_Outdated"], (
        f"the incompatible plugin must never be deregistered either, got {uninstalled}"
    )


def test_install_state_for_incompatible_update_reads_installed() -> None:
    """The store UI derives the install button from the same verdict: an
    installed plugin whose only newer pinned version is incompatible must
    read 'installed' (1), never 'update available' (2)."""
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
