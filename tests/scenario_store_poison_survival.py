"""
Regression test for gl#20 -- poison-entry survival stopped at plugins,
exercised WITHOUT network:

prepare_plugin lists a plugin with `image = None` when only its thumbnail
fetch fails (pinned by scenario_store_resilience), but prepare_icon,
prepare_wallpaper and prepare_sd_plus_bar_wallpaper still returned the
NoConnectionError -- process_store_data's `isinstance(result, data_class)`
filter then dropped the whole pack from the tab. Under a partial 429 storm
the Icons/Wallpapers/SD+ catalogs silently thinned out, and an all-fail page
showed "Nothing here" instead of the connection-error page.

The contract is now uniform across all four preparers: a failed
thumbnail/asset fetch lists the entry without an image; only a failed
MANIFEST (no id/name to list) still drops it.
"""
import asyncio
from types import SimpleNamespace

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError
from src.windows.Store.StoreData import IconData, SDPlusBarWallpaperData, WallpaperData


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    sb.official_authors = []
    return sb


def _stub_asset_fetches(sb: StoreBackend, manifest: dict) -> None:
    """Stubs every remote fetch a prepare_* coroutine performs, with the
    thumbnail fetch failing like a 429/offline does."""
    async def fake_manifest(url, commit):
        return dict(manifest)

    async def fake_image(url, path, branch="main"):
        return NoConnectionError()

    async def fake_attribution(url, commit):
        return {}

    sb.get_manifest = fake_manifest
    sb.get_web_image = fake_image
    sb.get_attribution = fake_attribution
    gl.lm = SimpleNamespace(get_custom_translation=lambda d: None)


# One entry per catalog: a store-JSON item pinned to a compatible version.
_ENTRY = {"url": "https://github.com/Example/TestPack", "commits": {"1.5.0": "abc123"}}
_MANIFEST = {"id": "com_example_TestPack", "name": "Test Pack",
             "version": "1.0", "thumbnail": "store/thumb.png"}


def test_prepare_icon_survives_failed_thumbnail() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()
    _stub_asset_fetches(sb, _MANIFEST)

    result = asyncio.run(sb.prepare_icon(dict(_ENTRY)))
    assert isinstance(result, IconData), (
        f"a failed thumbnail fetch must not drop the icon pack, got {result!r}"
    )
    assert result.image is None
    assert result.icon_id == "com_example_TestPack"


def test_prepare_wallpaper_survives_failed_thumbnail() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()
    _stub_asset_fetches(sb, _MANIFEST)

    result = asyncio.run(sb.prepare_wallpaper(dict(_ENTRY)))
    assert isinstance(result, WallpaperData), (
        f"a failed thumbnail fetch must not drop the wallpaper, got {result!r}"
    )
    assert result.image is None


def test_prepare_sd_plus_bar_wallpaper_survives_failed_thumbnail() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()
    _stub_asset_fetches(sb, _MANIFEST)

    result = asyncio.run(sb.prepare_sd_plus_bar_wallpaper(dict(_ENTRY)))
    assert isinstance(result, SDPlusBarWallpaperData), (
        f"a failed thumbnail fetch must not drop the SD+ bar wallpaper, got {result!r}"
    )
    assert result.image is None


def test_catalog_keeps_entry_with_failed_thumbnail() -> None:
    """End-to-end through process_store_data: the poison entry must survive
    NEXT TO the healthy one instead of being filtered out of the tab."""
    fixtures.install_stub_globals()
    sb = _make_backend()

    good = {"url": "https://github.com/Example/GoodPack", "commits": {"1.5.0": "good"}}
    poison = {"url": "https://github.com/Example/PoisonPack", "commits": {"1.5.0": "poison"}}

    async def fake_get_stores():
        return [("https://github.com/Example/store", "main")]

    async def fake_fetch_and_parse(url, filename, branch, n_errors=0):
        return [good, poison], n_errors

    async def fake_manifest(url, commit):
        pack = "GoodPack" if "GoodPack" in url else "PoisonPack"
        return {"id": f"com_example_{pack}", "name": pack,
                "version": "1.0", "thumbnail": "store/thumb.png"}

    class FakeImage:
        pass

    async def fake_image(url, path, branch="main"):
        if "PoisonPack" in url:
            return NoConnectionError()
        return FakeImage()

    async def fake_attribution(url, commit):
        return {}

    sb.get_stores = fake_get_stores
    sb.fetch_and_parse_store_json = fake_fetch_and_parse
    sb.get_manifest = fake_manifest
    sb.get_web_image = fake_image
    sb.get_attribution = fake_attribution
    gl.lm = SimpleNamespace(get_custom_translation=lambda d: None)

    results = asyncio.run(
        sb.process_store_data(StoreBackend.ICON_FILE, sb.prepare_icon, None, IconData)
    )
    assert not isinstance(results, NoConnectionError)
    ids = sorted(icon.icon_id for icon in results)
    assert ids == ["com_example_GoodPack", "com_example_PoisonPack"], (
        f"the entry with the failed thumbnail must stay in the catalog, got {ids}"
    )
    by_id = {icon.icon_id: icon for icon in results}
    assert by_id["com_example_PoisonPack"].image is None
    assert by_id["com_example_GoodPack"].image is not None


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_poison_survival")
    test_prepare_icon_survives_failed_thumbnail()
    test_prepare_wallpaper_survives_failed_thumbnail()
    test_prepare_sd_plus_bar_wallpaper_survives_failed_thumbnail()
    test_catalog_keeps_entry_with_failed_thumbnail()
    print("scenario_store_poison_survival: PASS")


if __name__ == "__main__":
    main()
