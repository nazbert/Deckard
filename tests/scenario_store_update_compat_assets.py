"""
The auto-update compatibility gate must cover the asset catalogs too.

prepare_icon, prepare_wallpaper and prepare_sd_plus_bar_wallpaper mark an
entry is_compatible False through the same fallback prepare_plugin uses. Each
matching *_to_update skips and reports such an entry, so only a compatible
outdated pack is offered and counted. No network is involved.
"""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Ok, Err
from src.windows.Store.StoreData import IconData, SDPlusBarWallpaperData, WallpaperData


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    return sb


def _icon_catalog() -> list[IconData]:
    return [
        IconData(github="https://github.com/a/uptodate", icon_id="com_a_UpToDate",
                 local_sha="aaa", commit_sha="aaa", is_compatible=True),
        IconData(github="https://github.com/b/outdated", icon_id="com_b_Outdated",
                 local_sha="old", commit_sha="new", is_compatible=True),
        IconData(github="https://github.com/c/incompat", icon_id="com_c_Incompat",
                 local_sha="old", commit_sha="next-major", is_compatible=False),
        IconData(github="https://github.com/d/notinstalled", icon_id="com_d_NotInstalled",
                 local_sha=None, commit_sha="xyz", is_compatible=False),
    ]


def _wallpaper_catalog() -> list[WallpaperData]:
    return [
        WallpaperData(github="https://github.com/b/outdated", wallpaper_id="com_b_Outdated",
                      local_sha="old", commit_sha="new", is_compatible=True),
        WallpaperData(github="https://github.com/c/incompat", wallpaper_id="com_c_Incompat",
                      local_sha="old", commit_sha="next-major", is_compatible=False),
    ]


def _sd_plus_catalog() -> list[SDPlusBarWallpaperData]:
    return [
        SDPlusBarWallpaperData(github="https://github.com/b/outdated", id="com_b_Outdated",
                               local_sha="old", commit_sha="new", is_compatible=True),
        SDPlusBarWallpaperData(github="https://github.com/c/incompat", id="com_c_Incompat",
                               local_sha="old", commit_sha="next-major", is_compatible=False),
    ]


def test_get_icons_to_update_skips_incompatible() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    def fake_get_all_icons(include_images: bool = True):
        return Ok(_icon_catalog())

    sb.get_all_icons = fake_get_all_icons

    to_update = sb.get_icons_to_update()
    assert not isinstance(to_update, Err)
    ids = [i.icon_id for i in to_update.value]
    assert ids == ["com_b_Outdated"], (
        f"only the compatibly-outdated icon pack may be offered for update, got {ids}"
    )


def test_update_all_icons_never_installs_incompatible() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    def fake_get_all_icons(include_images: bool = True):
        return Ok(_icon_catalog())

    installed: list[str] = []

    def fake_install_icon(icon_data):
        installed.append(icon_data.icon_id)
        return Ok(None)

    sb.get_all_icons = fake_get_all_icons
    sb.install_icon = fake_install_icon

    result = sb.update_all_icons()
    assert isinstance(result, Ok) and result.value == 1, (
        f"exactly the one compatible icon update may be counted, got {result!r}"
    )
    assert installed == ["com_b_Outdated"], (
        f"the incompatible icon pack must never be installed, got installs {installed}"
    )


def test_get_wallpapers_to_update_skips_incompatible() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    def fake_get_all_wallpapers(include_images: bool = True):
        return Ok(_wallpaper_catalog())

    sb.get_all_wallpapers = fake_get_all_wallpapers

    to_update = sb.get_wallpapers_to_update()
    assert not isinstance(to_update, Err)
    ids = [w.wallpaper_id for w in to_update.value]
    assert ids == ["com_b_Outdated"], (
        f"only the compatibly-outdated wallpaper may be offered for update, got {ids}"
    )


def test_get_sd_plus_bar_wallpapers_to_update_skips_incompatible() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    def fake_get_all_sd_plus(include_images: bool = True):
        return Ok(_sd_plus_catalog())

    sb.get_all_sd_plus_bar_wallpapers = fake_get_all_sd_plus

    to_update = sb.get_sd_plus_bar_wallpapers_to_update()
    assert not isinstance(to_update, Err)
    ids = [w.id for w in to_update.value]
    assert ids == ["com_b_Outdated"], (
        f"only the compatibly-outdated SD+ bar wallpaper may be offered for update, got {ids}"
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_update_compat_assets")
    test_get_icons_to_update_skips_incompatible()
    test_update_all_icons_never_installs_incompatible()
    test_get_wallpapers_to_update_skips_incompatible()
    test_get_sd_plus_bar_wallpapers_to_update_skips_incompatible()
    print("scenario_store_update_compat_assets: PASS")


if __name__ == "__main__":
    main()
