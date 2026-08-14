"""
The icon, wallpaper and SD+ bar-wallpaper update path must not pre-delete.

download_repo stages, validates and version-stamps the new tree before it swaps
that tree over the installed one, and the three pack installers make no
uninstall call of their own.
"""

# A failing download therefore leaves the installed pack byte-identical on
# disk. download_repo is stubbed to return Err.
import os

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Err, ErrReason
from src.windows.Store.StoreData import IconData, WallpaperData, SDPlusBarWallpaperData


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    return sb


def _seed_pack(rel_dir: str, pack_id: str) -> str:
    """Write a plausible installed pack (a file + a VERSION) under
    DATA_PATH/<rel_dir>/<pack_id> and return its path."""
    path = os.path.join(gl.DATA_PATH, rel_dir, pack_id)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "icon.png"), "w") as f:
        f.write("the user's installed art")
    with open(os.path.join(path, "VERSION"), "w") as f:
        f.write("old-but-working-sha")
    return path


def _assert_pack_survived(pack_path: str, kind: str) -> None:
    assert os.path.isdir(pack_path), (
        f"B-06: the installed {kind} pack at {pack_path} was deleted by the "
        f"update path before the (failing) download and never restored -- "
        f"the pack is permanently gone on a mid-download failure"
    )
    art = os.path.join(pack_path, "icon.png")
    assert os.path.isfile(art), f"B-06: {kind} pack contents lost"
    with open(art) as f:
        assert f.read() == "the user's installed art", f"{kind} pack corrupted"


def check_icon_pack_survives_failed_update() -> None:
    sb = _make_backend()
    data = IconData(github="https://github.com/test/Icons", icon_id="com_test_Icons",
                    commit_sha="b" * 40)
    pack = _seed_pack("icons", data.icon_id)

    def failing_download(**kwargs):
        return Err(ErrReason.NO_CONNECTION, "offline")

    sb.download_repo = failing_download

    result = sb.install_icon(data)
    assert isinstance(result, Err), (
        f"the failed download must surface, got {result!r}"
    )
    _assert_pack_survived(pack, "icon")
    print("PASS: icon pack survives a failed update")


def check_wallpaper_pack_survives_failed_update() -> None:
    sb = _make_backend()
    data = WallpaperData(github="https://github.com/test/Wall", wallpaper_id="com_test_Wall",
                         commit_sha="c" * 40)
    pack = _seed_pack("wallpapers", data.wallpaper_id)

    def failing_download(**kwargs):
        return Err(ErrReason.NO_CONNECTION, "offline")

    sb.download_repo = failing_download

    sb.install_wallpaper(data)
    _assert_pack_survived(pack, "wallpaper")
    print("PASS: wallpaper pack survives a failed update")


def check_sd_plus_pack_survives_failure() -> None:
    sb = _make_backend()
    data = SDPlusBarWallpaperData(github="https://github.com/test/SDPlus", id="com_test_SDPlus",
                                  commit_sha="d" * 40)
    pack = _seed_pack("sd_plus_bar_wallpapers", data.id)

    def failing_download(**kwargs):
        return Err(ErrReason.NO_CONNECTION, "offline")

    sb.download_repo = failing_download

    sb.install_sd_plus_bar_wallpaper(data)
    _assert_pack_survived(pack, "SD+ bar wallpaper")
    print("PASS: SD+ bar wallpaper pack survives a failed update")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_b06_pack_survival")
    check_icon_pack_survives_failed_update()
    check_wallpaper_pack_survives_failed_update()
    check_sd_plus_pack_survives_failure()
    print("PASS: scenario_store_b06_pack_survival")


if __name__ == "__main__":
    main()
