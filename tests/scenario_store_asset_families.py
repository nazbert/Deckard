"""Equivalence matrix for the four store prepare families collapsed to one.

prepare_plugin / prepare_icon / prepare_wallpaper /
prepare_sd_plus_bar_wallpaper are now thin wrappers over a single
_prepare_asset, selected by an AssetTypeDescriptor. This scenario drives all
four public wrappers through ONE identical stubbed fetch layer and asserts
the constructed *Data field-for-field against a literal expected table -- the
proof standard for a pure-refactor collapse is mutation-grade equivalence,
not red->green. Both views are covered (the full store-window view with
include_image=True, and the update-check view with include_image=False),
plus the error legs (manifest NoConnectionError propagates; an unparseable
url and a url-less plugin entry become None; no compatible version becomes
the NoCompatibleVersion class, which process_store_data filters out).

Two structural properties are pinned here on purpose:

  * The fetch layer is stubbed as INSTANCE ATTRIBUTES (``sb.get_manifest``,
    ``sb.get_web_image``, ``sb.get_attribution`` ...). _prepare_asset reaches
    every one of them through ``self`` / ``getattr(self, ...)``, never a bound
    callable captured in the descriptor -- so these stubs are honoured. The
    image assertion IS that proof: the constructed image is exactly the object
    the stub returned, which cannot happen if the descriptor had frozen a
    reference to the original get_web_image.

  * The literal expected tables below hard-code each type's id/name/version
    field names. They are NOT read off the descriptor, so a descriptor whose
    field names drift disagrees with them and the matrix goes red.

All network-free.
"""
import dataclasses
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from PIL import Image

from src.backend.Store.StoreBackend import StoreBackend, NoCompatibleVersion, NoConnectionError
from src.backend.Store.StoreCache import StoreCache
from src.windows.Store.StoreData import (
    IconData,
    PluginData,
    SDPlusBarWallpaperData,
    WallpaperData,
)


APP_MAJOR = int(gl.app_version.split(".")[0])
COMPATIBLE_VERSION = f"{APP_MAJOR}.1.0"
INCOMPATIBLE_VERSION = f"{APP_MAJOR + 1}.0.0"

URL = "https://github.com/acme/Widget"
COMMIT_SHA = "c0ffee" + "0" * 34
INCOMPAT_SHA = "dead" + "b" * 36
BRANCH_SHA = "b12345" + "a" * 34

# The one manifest and attribution every type is fed. version resolution keys
# off the commit map, so the manifest's own "version" is deliberately a
# distinct value: a collapse that confused the commit sha with the manifest
# version would be caught here.
MANIFEST = {
    "id": "com_acme_Widget",
    "name": "Widget",
    "version": "9.9.9",
    "thumbnail": "store/Thumbnail.png",
    # Fallbacks used only when a translation is absent; the translation stub
    # below echoes "en", so these are shadowed and the translated values win.
    "description": "long fallback (unused)",
    "short-description": "short fallback (unused)",
    # Deliberately distinct strings so a swap of the long/short pairing shows.
    "descriptions": {"en": "translated LONG"},
    "short-descriptions": {"en": "translated SHORT"},
    "tags": ["util", "demo"],
    "minimum-app-version": "1.0.0",
    "app-version": "1.5.0",
}
ATTRIBUTION_GENERIC = {
    "copyright": "(c) acme",
    "original-url": "https://acme.example/widget",
    "licence": "MIT",
    "licence-descriptions": {"en": "MIT terms"},
}
ATTRIBUTION = {"generic": ATTRIBUTION_GENERIC}
IMAGE = Image.new("RGB", (4, 4), (0, 128, 255))


# (key, wrapper method name, data_cls, id_field, name_field, version_field, is_plugin).
# Literal -- the descriptor table is NOT consulted to build this.
TYPES = [
    ("plugin", "prepare_plugin", PluginData, "plugin_id", "plugin_name", "plugin_version", True),
    ("icon", "prepare_icon", IconData, "icon_id", "icon_name", "icon_version", False),
    ("wallpaper", "prepare_wallpaper", WallpaperData, "wallpaper_id", "wallpaper_name", "wallpaper_version", False),
    ("sd_plus", "prepare_sd_plus_bar_wallpaper", SDPlusBarWallpaperData,
     "id", "name", "version", False),
]


def _stub_globals() -> None:
    fixtures.install_stub_globals()
    # Echo the "en" translation so the long/short descriptions carry distinct
    # values through _translate_descriptions -- pinning that pairing, which a
    # None-returning stub would leave invisible.
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: (translations or {}).get("en"))


def _make_backend() -> StoreBackend:
    """A backend with __init__ skipped (it spawns an authors-fetch thread) and
    exactly the attributes the prepare path and process_store_data touch."""
    sb = StoreBackend.__new__(StoreBackend)
    sb.store_cache = StoreCache()
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb._prepare_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="store-prepare")
    sb.official_authors = []
    sb.official_store_branch_cache = "1.5.0"
    return sb


def _reset_dirs(sb: StoreBackend) -> None:
    for base_dir in (gl.PLUGIN_DIR, sb.icons_dir(), sb.wallpapers_dir(),
                     sb.sd_plus_bar_wallpapers_dir()):
        shutil.rmtree(base_dir, ignore_errors=True)


def _stub_fetch_layer(sb: StoreBackend, *, manifest=MANIFEST, attribution=ATTRIBUTION,
                      image=IMAGE, last_commit=BRANCH_SHA) -> None:
    """Stub exactly the fetch layer _prepare_asset sits on, as instance
    attributes -- the same shape every standing store scenario uses."""
    sb.get_manifest = lambda url, commit: manifest
    sb.get_attribution = lambda url, commit: attribution
    sb.get_web_image = lambda url, path, branch="main": image
    sb.get_last_commit = lambda url, branch="main": last_commit


def _catalog_entry() -> dict:
    return {"url": URL, "commits": {COMPATIBLE_VERSION: COMMIT_SHA}}


def _assert_data_equal(actual, expected, label: str) -> None:
    assert type(actual) is type(expected), (
        f"{label}: got {type(actual).__name__}, expected {type(expected).__name__} ({actual!r})"
    )
    for f in dataclasses.fields(expected):
        got = getattr(actual, f.name)
        want = getattr(expected, f.name)
        assert got == want, f"{label}: field {f.name!r} = {got!r}, expected {want!r}"


def _expected_full(data_cls, id_field, name_field, version_field, *, verified: bool):
    """The full store-window row every type must build from MANIFEST +
    ATTRIBUTION, differing only in the three id/name/version field names."""
    kwargs = {
        "github": URL,
        "descriptions": MANIFEST["descriptions"],
        "short_descriptions": MANIFEST["short-descriptions"],
        # The translated values (echoed "en"), which win over the fallbacks.
        "description": MANIFEST["descriptions"]["en"],
        "short_description": MANIFEST["short-descriptions"]["en"],
        "author": "acme",
        "official": False,
        "commit_sha": COMMIT_SHA,
        "local_sha": None,
        "minimum_app_version": MANIFEST["minimum-app-version"],
        "app_version": MANIFEST["app-version"],
        "repository_name": "Widget",
        "tags": MANIFEST["tags"],
        "is_compatible": True,
        "branch": None,
        "verified": verified,
        "thumbnail": MANIFEST["thumbnail"],
        "image": IMAGE,
        "copyright": ATTRIBUTION_GENERIC["copyright"],
        "original_url": ATTRIBUTION_GENERIC["original-url"],
        "license": ATTRIBUTION_GENERIC["licence"],
        "license_descriptions": ATTRIBUTION_GENERIC["licence-descriptions"],
        id_field: MANIFEST["id"],
        name_field: MANIFEST["name"],
        version_field: MANIFEST["version"],
    }
    return data_cls(**kwargs)


def _expected_update(data_cls, id_field, *, verified: bool):
    """The update-check row: only what get_*_to_update reads. Every display
    field is left at the dataclass default."""
    return data_cls(**{
        "github": URL,
        "author": "acme",
        "repository_name": "Widget",
        "commit_sha": COMMIT_SHA,
        "local_sha": None,
        id_field: None,
        "is_compatible": True,
        "verified": verified,
    })


def test_full_view_matrix_is_identical_across_types() -> None:
    """include_image=True: all four wrappers build the same row from the same
    manifest/attribution/thumbnail, field for field."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    for key, method, data_cls, id_field, name_field, version_field, _is_plugin in TYPES:
        actual = getattr(sb, method)(_catalog_entry(), include_image=True, verified=True)
        expected = _expected_full(data_cls, id_field, name_field, version_field, verified=True)
        _assert_data_equal(actual, expected, f"full/{key}")
        # The image came from the instance stub, not a captured callable: this
        # is the getattr-dispatch guarantee in one assertion.
        assert actual.image is IMAGE, f"full/{key}: image must be the stubbed object"


def test_update_view_matrix_is_identical_across_types() -> None:
    """include_image=False: the update-check row, nothing installed, field for
    field -- and no display field is fetched or set."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    for key, method, data_cls, id_field, name_field, version_field, _is_plugin in TYPES:
        actual = getattr(sb, method)(_catalog_entry(), include_image=False, verified=False)
        expected = _expected_update(data_cls, id_field, verified=False)
        _assert_data_equal(actual, expected, f"update/{key}")


def test_incompatible_entry_flags_but_still_builds() -> None:
    """A version map with no release for this app major flags the row
    incompatible and pins the newest available commit -- for every type."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    entry = {"url": URL, "commits": {INCOMPATIBLE_VERSION: INCOMPAT_SHA}}
    for key, method, _data_cls, _id, _name, _version, _is_plugin in TYPES:
        actual = getattr(sb, method)(entry, include_image=True, verified=False)
        assert actual is not None, f"incompatible/{key}: entry must still build"
        assert actual.is_compatible is False, (
            f"incompatible/{key}: is_compatible must be False, got {actual.is_compatible!r}"
        )
        assert actual.commit_sha == INCOMPAT_SHA, (
            f"incompatible/{key}: must pin the newest available commit, got {actual.commit_sha!r}"
        )


def test_plugin_branch_arm_resolves_and_records_branch() -> None:
    """A branch-pinned plugin entry (no version map) resolves its tip through
    get_last_commit and carries the branch -- the plugin-only prepare arm."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    entry = {"url": URL, "branch": "main"}
    result = sb.prepare_plugin(entry, include_image=True, verified=False)
    assert isinstance(result, PluginData)
    assert result.branch == "main", f"the branch must be recorded, got {result.branch!r}"
    assert result.commit_sha == BRANCH_SHA, (
        f"the branch tip must be resolved via get_last_commit, got {result.commit_sha!r}"
    )


def test_branch_field_is_gated_by_is_plugin() -> None:
    """check_entry_for_update resolves a branch key for any type, but only a
    plugin row carries it -- the other three drop it, exactly as before."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    branch_entry = {"url": URL, "branch": "dev"}

    plugin = sb.prepare_plugin(branch_entry, include_image=False, verified=False)
    assert plugin.branch == "dev", f"a plugin update row must keep the branch, got {plugin.branch!r}"
    assert plugin.commit_sha == BRANCH_SHA

    for key, method in (("icon", "prepare_icon"), ("wallpaper", "prepare_wallpaper"),
                        ("sd_plus", "prepare_sd_plus_bar_wallpaper")):
        row = getattr(sb, method)(branch_entry, include_image=False, verified=False)
        assert row.branch is None, (
            f"{key} update row must not carry a branch (is_plugin gate), got {row.branch!r}"
        )
        # The tip was still resolved for the commit target, so the gate is on
        # the field, not the resolution.
        assert row.commit_sha == BRANCH_SHA, f"{key}: commit target still resolves"


def test_failed_thumbnail_lists_without_image() -> None:
    """A thumbnail fetch that fails (offline / rate-limited) must not drop the
    row -- it is listed with image=None, for every type. This is the whole
    reason _fetch_thumbnail exists as the single image guard."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)
    sb.get_web_image = lambda url, path, branch="main": NoConnectionError()

    for key, method, data_cls, _id, _name, _version, _is_plugin in TYPES:
        row = getattr(sb, method)(_catalog_entry(), include_image=True, verified=False)
        assert isinstance(row, data_cls), (
            f"failed-thumbnail/{key}: the row must still build, got {row!r}"
        )
        assert row.image is None, (
            f"failed-thumbnail/{key}: a failed thumbnail lists without an image, got {row.image!r}"
        )


def test_all_three_fetches_receive_the_resolved_ref() -> None:
    """manifest, thumbnail and attribution are all fetched at the ONE resolved
    ref -- the commit sha for a version-map entry, the branch tip for the
    plugin branch arm. This pins ref_for_fetch, the single local that replaced
    four `commit or branch` expressions feeding all three fetches."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    refs: dict[str, list] = {"manifest": [], "attribution": [], "image": []}
    sb.get_manifest = lambda url, commit: (refs["manifest"].append(commit), MANIFEST)[1]
    sb.get_attribution = lambda url, commit: (refs["attribution"].append(commit), ATTRIBUTION)[1]
    sb.get_web_image = lambda url, path, branch="main": (refs["image"].append(branch), IMAGE)[1]
    sb.get_last_commit = lambda url, branch="main": BRANCH_SHA

    # Version-map path: the resolved commit sha feeds all three fetches.
    sb.prepare_icon(_catalog_entry(), include_image=True, verified=False)
    assert refs["manifest"] == [COMMIT_SHA], f"manifest fetched at {refs['manifest']!r}, want {COMMIT_SHA!r}"
    assert refs["attribution"] == [COMMIT_SHA], f"attribution fetched at {refs['attribution']!r}, want {COMMIT_SHA!r}"
    assert refs["image"] == [COMMIT_SHA], f"thumbnail fetched at {refs['image']!r}, want {COMMIT_SHA!r}"

    # Plugin branch arm: the resolved branch TIP (a sha), not the branch name,
    # feeds all three -- this is where `commit or branch` vs `branch or commit`
    # diverges (commit=tip sha, branch="main" both truthy).
    for bucket in refs.values():
        bucket.clear()
    sb.prepare_plugin({"url": URL, "branch": "main"}, include_image=True, verified=False)
    assert refs["manifest"] == [BRANCH_SHA], f"branch-arm manifest fetched at {refs['manifest']!r}, want {BRANCH_SHA!r}"
    assert refs["attribution"] == [BRANCH_SHA], f"branch-arm attribution fetched at {refs['attribution']!r}, want {BRANCH_SHA!r}"
    assert refs["image"] == [BRANCH_SHA], f"branch-arm thumbnail fetched at {refs['image']!r}, want {BRANCH_SHA!r}"


def test_full_prepare_backfills_the_origin_stamp() -> None:
    """A full prepare identifies an install the expensive way (its manifest),
    so it records the origin link for the update check to reuse. An install
    directory with no ORIGIN stamp gets one written, naming the entry's url --
    for both an install dir and a directory-per-type resolution."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    for key, method, base_dir in (
        ("plugin", "prepare_plugin", gl.PLUGIN_DIR),
        ("icon", "prepare_icon", sb.icons_dir()),
    ):
        asset_path = os.path.join(base_dir, MANIFEST["id"])
        os.makedirs(asset_path, exist_ok=True)
        with open(os.path.join(asset_path, "manifest.json"), "w") as f:
            json.dump({"id": MANIFEST["id"], "name": "Widget", "version": "1.0"}, f)
        with open(os.path.join(asset_path, "VERSION"), "w") as f:
            f.write(COMMIT_SHA)
        origin = os.path.join(asset_path, StoreBackend.ORIGIN_FILE)
        assert not os.path.exists(origin), f"{key}: precondition -- no stamp yet"

        getattr(sb, method)(_catalog_entry(), include_image=True, verified=False)

        assert os.path.isfile(origin), (
            f"{key}: a full prepare must backfill the origin stamp it paid a manifest for"
        )
        with open(origin) as f:
            assert f.read().strip() == URL, f"{key}: the stamp must name the entry's url"


def test_manifest_connection_error_propagates() -> None:
    """A NoConnectionError from the manifest fetch propagates unchanged out of
    every wrapper (process_store_data drops it)."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb, manifest=NoConnectionError())

    for key, method, _cls, _id, _name, _version, _is_plugin in TYPES:
        result = getattr(sb, method)(_catalog_entry(), include_image=True, verified=False)
        assert isinstance(result, NoConnectionError), (
            f"manifest-error/{key}: must propagate NoConnectionError, got {result!r}"
        )


def test_unparseable_url_and_missing_url_become_none() -> None:
    """An entry whose url names no repository is dropped as None -- and the
    url-guard now covers plugins too, so a url-less plugin entry is a
    logged skip (None), not a KeyError."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    bad_url = {"url": "not-a-repository", "commits": {COMPATIBLE_VERSION: COMMIT_SHA}}
    for key, method, _cls, _id, _name, _version, _is_plugin in TYPES:
        assert getattr(sb, method)(bad_url, include_image=True, verified=False) is None, (
            f"bad-url/{key}: an unparseable url must become None"
        )

    # The reconciliation: plugin gained the "url" not in entry guard.
    assert sb.prepare_plugin({"commits": {COMPATIBLE_VERSION: COMMIT_SHA}},
                             include_image=True, verified=False) is None, (
        "a url-less plugin entry must be a logged skip (None), not a KeyError"
    )


def test_no_compatible_version_sentinel_is_filtered_by_process_store_data() -> None:
    """When no version resolves at all, prepare returns the NoCompatibleVersion
    class -- kept byte-identical -- and process_store_data's isinstance filter
    drops it, so the catalog list simply omits the entry."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)
    # Force version resolution to yield nothing (both instance-stubbed, so this
    # also re-confirms getattr dispatch reaches them).
    sb.get_newest_compatible_version = lambda versions: None
    sb.get_newest_version = lambda versions: None

    entry = _catalog_entry()
    assert sb.prepare_icon(entry, include_image=True, verified=False) is NoCompatibleVersion, (
        "no resolvable version must return the NoCompatibleVersion class sentinel"
    )

    # And driven through the real process_store_data, the sentinel is filtered.
    sb.get_stores = lambda: [(URL, "main")]
    sb.fetch_and_parse_store_json = lambda url, filename, branch, n_errors=0: ([entry], n_errors)
    result = sb.get_all_icons()
    assert result == [], (
        f"the NoCompatibleVersion sentinel must be filtered by process_store_data, got {result!r}"
    )


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_asset_families")
    test_full_view_matrix_is_identical_across_types()
    test_update_view_matrix_is_identical_across_types()
    test_incompatible_entry_flags_but_still_builds()
    test_plugin_branch_arm_resolves_and_records_branch()
    test_branch_field_is_gated_by_is_plugin()
    test_failed_thumbnail_lists_without_image()
    test_all_three_fetches_receive_the_resolved_ref()
    test_full_prepare_backfills_the_origin_stamp()
    test_manifest_connection_error_propagates()
    test_unparseable_url_and_missing_url_become_none()
    test_no_compatible_version_sentinel_is_filtered_by_process_store_data()
    print("scenario_store_asset_families: PASS")


if __name__ == "__main__":
    main()
