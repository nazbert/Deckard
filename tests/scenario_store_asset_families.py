"""Equivalence matrix for the four store prepare families.

prepare_plugin, prepare_icon, prepare_wallpaper and
prepare_sd_plus_bar_wallpaper are thin wrappers over one _prepare_asset,
selected by an AssetTypeDescriptor.
"""

# All four run through one stubbed fetch layer, and the constructed Data is
# asserted field for field.

# The same descriptor drives install, uninstall and the update legs, so those
# are pinned here as well. Every expected table below is a literal, never read
# off the descriptor, so a descriptor whose field names drift disagrees.
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

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.StoreCache import StoreCache
from src.backend.Store.store_result import Ok, Err, ErrReason, StoreFetchError
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

# The one manifest and attribution every type is fed. Version resolution keys
# off the commit map, so the manifest's own "version" is a distinct value and
# a confusion between the commit sha and the manifest version shows up here.
MANIFEST = {
    "id": "com_acme_Widget",
    "name": "Widget",
    "version": "9.9.9",
    "thumbnail": "store/Thumbnail.png",
    # Fallbacks used only when a translation is absent. The translation stub
    # below echoes "en", so the translated values win.
    "description": "long fallback (unused)",
    "short-description": "short fallback (unused)",
    # Distinct strings, so a swap of the long and short pairing shows.
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
# Literal, so the descriptor table is never consulted to build this.
TYPES = [
    ("plugin", "prepare_plugin", PluginData, "plugin_id", "plugin_name", "plugin_version", True),
    ("icon", "prepare_icon", IconData, "icon_id", "icon_name", "icon_version", False),
    ("wallpaper", "prepare_wallpaper", WallpaperData, "wallpaper_id", "wallpaper_name", "wallpaper_version", False),
    ("sd_plus", "prepare_sd_plus_bar_wallpaper", SDPlusBarWallpaperData,
     "id", "name", "version", False),
]

# The install and update side. The public method names are hard-coded here
# and never read off the descriptor, so a descriptor whose attr drifts breaks
# the dispatch these drive. Every install answers one Ok(None) success value.
# (key, data_cls, id_field, get_all, get_to_update, update_all, install, install_success).
UPDATE_TYPES = [
    ("plugin", PluginData, "plugin_id", "get_all_plugins", "get_plugins_to_update",
     "update_all_plugins", "install_plugin", Ok(None)),
    ("icon", IconData, "icon_id", "get_all_icons", "get_icons_to_update",
     "update_all_icons", "install_icon", Ok(None)),
    ("wallpaper", WallpaperData, "wallpaper_id", "get_all_wallpapers", "get_wallpapers_to_update",
     "update_all_wallpapers", "install_wallpaper", Ok(None)),
    ("sd_plus", SDPlusBarWallpaperData, "id", "get_all_sd_plus_bar_wallpapers",
     "get_sd_plus_bar_wallpapers_to_update", "update_all_sd_plus_bar_wallpapers",
     "install_sd_plus_bar_wallpaper", Ok(None)),
]

# The five update-decision branches, against one fixture set per type. Only
# the last one, installed with a newer known sha and compatible, is offered
# for update. The no-known-target branch is reachable for every type but is
# not observable, because a None target can never install successfully.
# (tag, local_sha, commit_sha, is_compatible, offered_for_update).
DECISION_FIXTURES = [
    ("not_installed", None, "newsha", True, False),
    ("up_to_date", "samesha", "samesha", True, False),
    ("no_known_target", "oldsha", None, True, False),
    ("incompatible", "oldsha", "nextmajorsha", False, False),
    ("update", "oldsha", "newsha", True, True),
]


def _stub_globals() -> None:
    fixtures.install_stub_globals()
    # Echo the "en" translation, so the long and short descriptions carry
    # distinct values through _translate_descriptions. A None-returning stub
    # would leave that pairing invisible.
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: (translations or {}).get("en"))


def _make_backend() -> StoreBackend:
    """A backend with __init__ skipped, because it spawns an authors-fetch
    thread, and only the attributes the prepare path touches."""
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
    """Stub the fetch layer _prepare_asset sits on, as instance attributes,
    in the shape every store scenario uses."""
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
    """The full store-window row every type builds from the manifest and the
    attribution, differing only in the three field names."""
    kwargs = {
        "github": URL,
        "descriptions": MANIFEST["descriptions"],
        "short_descriptions": MANIFEST["short-descriptions"],
        # The translated values, echoed from "en", win over the fallbacks.
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
    """The update-check row holds only what get_*_to_update reads. Every
    display field stays at the dataclass default."""
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


def test_full_view_matrix_identical() -> None:
    """With include_image True, all four wrappers build the same row from the
    same manifest, attribution and thumbnail, field for field."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    for key, method, data_cls, id_field, name_field, version_field, _is_plugin in TYPES:
        actual = getattr(sb, method)(_catalog_entry(), include_image=True, verified=True)
        expected = _expected_full(data_cls, id_field, name_field, version_field, verified=True)
        _assert_data_equal(actual, expected, f"full/{key}")
        # The image came from the instance stub rather than a captured
        # callable, which is the getattr dispatch in one assertion.
        assert actual.image is IMAGE, f"full/{key}: image must be the stubbed object"


def test_update_view_matrix_identical() -> None:
    """With include_image False the update-check row is built field for
    field, with nothing installed and no display field fetched or set."""
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
    incompatible and pins the newest available commit, for every type."""
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
    """A branch-pinned plugin entry with no version map resolves its tip
    through get_last_commit and carries the branch."""
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


def test_branch_field_gated_by_is_plugin() -> None:
    """check_entry_for_update resolves a branch key for any type, but only a
    plugin row carries it. The other three drop it."""
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
        # The tip is still resolved for the commit target, so the gate sits
        # on the field rather than on the resolution.
        assert row.commit_sha == BRANCH_SHA, f"{key}: commit target still resolves"


def test_failed_thumbnail_lists_without_image() -> None:
    """A thumbnail fetch that fails must not drop the row. It is listed with
    image None, for every type, which is why _fetch_thumbnail is the single
    image guard."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)
    sb.get_web_image = lambda url, path, branch="main": None

    for key, method, data_cls, _id, _name, _version, _is_plugin in TYPES:
        row = getattr(sb, method)(_catalog_entry(), include_image=True, verified=False)
        assert isinstance(row, data_cls), (
            f"failed-thumbnail/{key}: the row must still build, got {row!r}"
        )
        assert row.image is None, (
            f"failed-thumbnail/{key}: a failed thumbnail lists without an image, got {row.image!r}"
        )


def test_fetches_receive_resolved_ref() -> None:
    """The manifest, thumbnail and attribution are all fetched at one
    resolved ref, the commit sha for a version-map entry and the branch tip
    for the plugin branch arm. This pins ref_for_fetch."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    refs: dict[str, list] = {"manifest": [], "attribution": [], "image": []}
    sb.get_manifest = lambda url, commit: (refs["manifest"].append(commit), MANIFEST)[1]
    sb.get_attribution = lambda url, commit: (refs["attribution"].append(commit), ATTRIBUTION)[1]
    sb.get_web_image = lambda url, path, branch="main": (refs["image"].append(branch), IMAGE)[1]
    sb.get_last_commit = lambda url, branch="main": BRANCH_SHA

    # On the version-map path the resolved commit sha feeds all three
    # fetches.
    sb.prepare_icon(_catalog_entry(), include_image=True, verified=False)
    assert refs["manifest"] == [COMMIT_SHA], f"manifest fetched at {refs['manifest']!r}, want {COMMIT_SHA!r}"
    assert refs["attribution"] == [COMMIT_SHA], f"attribution fetched at {refs['attribution']!r}, want {COMMIT_SHA!r}"
    assert refs["image"] == [COMMIT_SHA], f"thumbnail fetched at {refs['image']!r}, want {COMMIT_SHA!r}"

    # On the plugin branch arm the resolved branch tip, a sha, feeds all
    # three rather than the branch name. Both are truthy, so the order of a
    # "commit or branch" expression decides which one wins.
    for bucket in refs.values():
        bucket.clear()
    sb.prepare_plugin({"url": URL, "branch": "main"}, include_image=True, verified=False)
    assert refs["manifest"] == [BRANCH_SHA], f"branch-arm manifest fetched at {refs['manifest']!r}, want {BRANCH_SHA!r}"
    assert refs["attribution"] == [BRANCH_SHA], f"branch-arm attribution fetched at {refs['attribution']!r}, want {BRANCH_SHA!r}"
    assert refs["image"] == [BRANCH_SHA], f"branch-arm thumbnail fetched at {refs['image']!r}, want {BRANCH_SHA!r}"


def test_prepare_backfills_origin_stamp() -> None:
    """A full prepare identifies an install through its manifest, which is
    the expensive way, so it records the origin link for the update check to
    reuse. An install directory with no ORIGIN stamp gets one written."""
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


def test_manifest_fetch_error_propagates() -> None:
    """A StoreFetchError from the manifest fetch propagates unchanged out of
    every wrapper, so process_store_data's per-future collect drops just that
    entry."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    def raise_fetch_error(url, commit):
        raise StoreFetchError(url, "manifest unreachable")

    sb.get_manifest = raise_fetch_error

    for key, method, _cls, _id, _name, _version, _is_plugin in TYPES:
        try:
            getattr(sb, method)(_catalog_entry(), include_image=True, verified=False)
            raise AssertionError(f"manifest-error/{key}: must propagate StoreFetchError")
        except StoreFetchError:
            pass


def test_unparseable_url_and_missing_url_become_none() -> None:
    """An entry whose url names no repository is dropped as None. The url
    guard covers plugins too, so a url-less plugin entry is a logged skip
    rather than a KeyError."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)

    bad_url = {"url": "not-a-repository", "commits": {COMPATIBLE_VERSION: COMMIT_SHA}}
    for key, method, _cls, _id, _name, _version, _is_plugin in TYPES:
        assert getattr(sb, method)(bad_url, include_image=True, verified=False) is None, (
            f"bad-url/{key}: an unparseable url must become None"
        )

    # The plugin arm carries the same "url not in entry" guard.
    assert sb.prepare_plugin({"commits": {COMPATIBLE_VERSION: COMMIT_SHA}},
                             include_image=True, verified=False) is None, (
        "a url-less plugin entry must be a logged skip (None), not a KeyError"
    )


def test_no_compatible_version_filtered() -> None:
    """When no version resolves, prepare returns None and
    process_store_data's isinstance filter drops it, so the catalog list
    omits the entry."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)
    _stub_fetch_layer(sb)
    # Force version resolution to yield nothing. Both are instance stubs, so
    # this also confirms the getattr dispatch reaches them.
    sb.get_newest_compatible_version = lambda versions: None
    sb.get_newest_version = lambda versions: None

    entry = _catalog_entry()
    assert sb.prepare_icon(entry, include_image=True, verified=False) is None, (
        "no resolvable version must drop the entry (prepare returns None)"
    )

    # Driven through the real process_store_data, the None is filtered.
    # get_all_* returns a StoreResult, so the filtered catalog is Ok([]).
    sb.get_stores = lambda: [(URL, "main")]
    sb.fetch_and_parse_store_json = lambda url, filename, branch, n_errors=0: ([entry], n_errors)
    result = sb.get_all_icons()
    assert result == Ok([]), (
        f"a no-compatible-version entry (prepare returns None) must be filtered by process_store_data, got {result!r}"
    )


def test_canonical_properties_map_fields() -> None:
    """The asset_id, asset_name and asset_version properties read each type's
    own fields. Shared backend code and log lines use those names instead of
    getattr on a per-type field name."""
    cases = [
        (PluginData(plugin_id="pi", plugin_name="pn", plugin_version="pv"), "pi", "pn", "pv"),
        (IconData(icon_id="ii", icon_name="in", icon_version="iv"), "ii", "in", "iv"),
        (WallpaperData(wallpaper_id="wi", wallpaper_name="wn", wallpaper_version="wv"), "wi", "wn", "wv"),
        (SDPlusBarWallpaperData(id="si", name="sn", version="sv"), "si", "sn", "sv"),
    ]
    for data, want_id, want_name, want_version in cases:
        label = type(data).__name__
        assert data.asset_id == want_id, f"{label}.asset_id = {data.asset_id!r}, want {want_id!r}"
        assert data.asset_name == want_name, f"{label}.asset_name = {data.asset_name!r}, want {want_name!r}"
        assert data.asset_version == want_version, (
            f"{label}.asset_version = {data.asset_version!r}, want {want_version!r}"
        )


def test_update_decision_matrix_per_type() -> None:
    """The five-branch update decision, run against one fixture set for every
    type through the real get_*_to_update. Only the compatible, newer entry
    with a known sha is offered, and its identity is read off asset_id."""
    _stub_globals()
    sb = _make_backend()

    for key, data_cls, id_field, get_all, get_to_update, *_rest in UPDATE_TYPES:
        assets = [
            data_cls(**{id_field: f"com_acme_{tag}", "github": URL,
                        "local_sha": local, "commit_sha": commit, "is_compatible": compat})
            for tag, local, commit, compat, _offered in DECISION_FIXTURES
        ]
        setattr(sb, get_all, lambda include_images=True, _a=assets: Ok(_a))

        to_update = getattr(sb, get_to_update)()
        assert not isinstance(to_update, Err), f"{key}: catalog must resolve"
        got = sorted(a.asset_id for a in to_update.value)
        want = sorted(f"com_acme_{tag}" for tag, _l, _c, _cmp, offered in DECISION_FIXTURES if offered)
        assert got == want, f"{key}: only the compatibly-outdated entry may update; got {got}, want {want}"


def test_update_all_counts_only_successful_installs() -> None:
    """update_all_* counts a reinstall only when the install answers Ok. Both
    failure legs return an Err, and an Err is truthy, so a truthiness check
    would count one. The protocol is narrowing on the result type."""
    _stub_globals()
    sb = _make_backend()

    for key, data_cls, id_field, get_all, _get_to_update, update_all, install, success in UPDATE_TYPES:
        def _asset(tag, _cls=data_cls, _f=id_field):
            return _cls(**{_f: f"com_acme_{tag}", "github": URL,
                           "local_sha": "old", "commit_sha": "new", "is_compatible": True})

        good, bad_hard, bad_conn = _asset("Good"), _asset("BadHard"), _asset("BadConn")
        setattr(sb, get_all, lambda include_images=True, _a=[good, bad_hard, bad_conn]: Ok(_a))

        installed: list = []

        def fake_install(data, auto_update=False, _s=success, _installed=installed,
                         _good=good, _bhard=bad_hard):
            _installed.append(data.asset_id)
            if data is _good:
                return _s
            if data is _bhard:
                # A truthy failure. An Err is truthy, so a truthiness check
                # counts it and a narrowing check does not.
                return Err(ErrReason.INSTALL_FAILED, "404-shaped")
            return Err(ErrReason.NO_CONNECTION, "offline")

        setattr(sb, install, fake_install)

        result = getattr(sb, update_all)()
        assert isinstance(result, Ok) and result.value == 1, (
            f"{key}: only the successful reinstall may be counted; two Err legs "
            f"are both failures, got {result!r}"
        )
        assert installed == ["com_acme_Good", "com_acme_BadHard", "com_acme_BadConn"], (
            f"{key}: every outdated entry is attempted, got {installed}"
        )


def test_update_everything_dispatches_legs() -> None:
    """update_everything sums every class's update_all_* leg, reached by its
    public name in ASSET_TYPES order. That pins the loop, its order with
    plugins reloading first, and each descriptor's update_all_attr."""
    _stub_globals()
    sb = _make_backend()

    called: list = []
    for name, ret in (("update_all_plugins", 2), ("update_all_icons", 1),
                      ("update_all_wallpapers", 3), ("update_all_sd_plus_bar_wallpapers", 4)):
        setattr(sb, name, lambda _n=name, _r=ret: (called.append(_n), Ok(_r))[1])

    total = sb.update_everything()
    assert isinstance(total, Ok) and total.value == 10, (
        f"update_everything must sum every leg (2+1+3+4), got {total!r}"
    )
    assert called == [
        "update_all_plugins", "update_all_icons",
        "update_all_wallpapers", "update_all_sd_plus_bar_wallpapers",
    ], (
        "the update_all_* legs must dispatch in ASSET_TYPES order -- plugins "
        f"first, the only leg that reloads the plugin manager, got {called}"
    )


def test_install_passes_dir_and_expected_id() -> None:
    """Every installer hands download_repo the per-type install directory and
    the asset id as expected_id. A swapped base_dir_attr names the wrong
    type's tree and fails here."""
    _stub_globals()
    sb = _make_backend()

    captured: dict = {}

    def fake_download(**kwargs):
        captured.clear()
        captured.update(kwargs)
        # An Err returns early, which keeps the plugin body off the reload
        # path.
        return Err(ErrReason.NO_CONNECTION, "offline")

    sb.download_repo = fake_download

    cases = [
        ("plugin", "install_plugin", PluginData, "plugin_id", gl.PLUGIN_DIR),
        ("icon", "install_icon", IconData, "icon_id", sb.icons_dir()),
        ("wallpaper", "install_wallpaper", WallpaperData, "wallpaper_id", sb.wallpapers_dir()),
        ("sd_plus", "install_sd_plus_bar_wallpaper", SDPlusBarWallpaperData, "id",
         sb.sd_plus_bar_wallpapers_dir()),
    ]
    for key, install, data_cls, id_field, base_dir in cases:
        asset_id = f"com_acme_{key}"
        data = data_cls(**{id_field: asset_id, "github": URL, "commit_sha": COMMIT_SHA})
        getattr(sb, install)(data)
        assert captured.get("repo_url") == URL, f"{key}: repo_url {captured.get('repo_url')!r}"
        assert captured.get("expected_id") == asset_id, (
            f"{key}: expected_id must be the asset id {asset_id!r}, got {captured.get('expected_id')!r}"
        )
        assert captured.get("directory") == os.path.join(base_dir, asset_id), (
            f"{key}: directory must be {os.path.join(base_dir, asset_id)!r}, "
            f"got {captured.get('directory')!r}"
        )


def test_install_refuses_unsafe_id_and_url() -> None:
    """The three data-only installers reject a traversal id and a url-less
    entry with Err(INVALID_ASSET), before any download."""
    _stub_globals()
    sb = _make_backend()

    def exploding_download(**kwargs):
        raise AssertionError("download_repo must not run for a refused install")

    sb.download_repo = exploding_download

    cases = [
        ("icon", "install_icon", IconData, "icon_id"),
        ("wallpaper", "install_wallpaper", WallpaperData, "wallpaper_id"),
        ("sd_plus", "install_sd_plus_bar_wallpaper", SDPlusBarWallpaperData, "id"),
    ]
    for key, install, data_cls, id_field in cases:
        unsafe = data_cls(**{id_field: "../../../etc", "github": URL})
        r_unsafe = getattr(sb, install)(unsafe)
        assert isinstance(r_unsafe, Err) and r_unsafe.reason is ErrReason.INVALID_ASSET, (
            f"{key}: an unsafe id must be refused with INVALID_ASSET, got {r_unsafe!r}"
        )

        no_url = data_cls(**{id_field: f"com_acme_{key}", "github": None})
        r_no_url = getattr(sb, install)(no_url)
        assert isinstance(r_no_url, Err) and r_no_url.reason is ErrReason.INVALID_ASSET, (
            f"{key}: a url-less entry must be refused with INVALID_ASSET, got {r_no_url!r}"
        )


def test_uninstall_removes_dir_keeps_returns() -> None:
    """The three data-only uninstallers rmtree the per-type directory and
    return None. An unsafe id returns 400 and touches nothing."""
    _stub_globals()
    sb = _make_backend()
    _reset_dirs(sb)

    cases = [
        ("icon", "uninstall_icon", IconData, "icon_id", sb.icons_dir()),
        ("wallpaper", "uninstall_wallpaper", WallpaperData, "wallpaper_id", sb.wallpapers_dir()),
        ("sd_plus", "uninstall_sd_plus_bar_wallpaper", SDPlusBarWallpaperData, "id",
         sb.sd_plus_bar_wallpapers_dir()),
    ]
    for key, uninstall, data_cls, id_field, base_dir in cases:
        asset_id = f"com_acme_{key}"
        asset_path = os.path.join(base_dir, asset_id)
        os.makedirs(asset_path, exist_ok=True)
        with open(os.path.join(asset_path, "art.png"), "w") as f:
            f.write("installed art")

        result = getattr(sb, uninstall)(data_cls(**{id_field: asset_id}))
        assert result is None, f"{key}: a normal uninstall returns None, got {result!r}"
        assert not os.path.exists(asset_path), f"{key}: the installed dir must be removed"

        unsafe_path = os.path.join(base_dir, "keepme")
        os.makedirs(unsafe_path, exist_ok=True)
        assert getattr(sb, uninstall)(data_cls(**{id_field: "../../../etc"})) == 400, (
            f"{key}: an unsafe id must return 400"
        )
        assert os.path.isdir(unsafe_path), f"{key}: a refused uninstall must delete nothing"


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_asset_families")
    test_full_view_matrix_identical()
    test_update_view_matrix_identical()
    test_incompatible_entry_flags_but_still_builds()
    test_plugin_branch_arm_resolves_and_records_branch()
    test_branch_field_gated_by_is_plugin()
    test_failed_thumbnail_lists_without_image()
    test_fetches_receive_resolved_ref()
    test_prepare_backfills_origin_stamp()
    test_manifest_fetch_error_propagates()
    test_unparseable_url_and_missing_url_become_none()
    test_no_compatible_version_filtered()
    test_canonical_properties_map_fields()
    test_update_decision_matrix_per_type()
    test_update_all_counts_only_successful_installs()
    test_update_everything_dispatches_legs()
    test_install_passes_dir_and_expected_id()
    test_install_refuses_unsafe_id_and_url()
    test_uninstall_removes_dir_keeps_returns()
    print("scenario_store_asset_families: PASS")


if __name__ == "__main__":
    main()
