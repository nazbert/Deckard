"""
Regression test -- the boot update check cost a full store browse,
exercised WITHOUT network:

update_everything() (startup auto-update, and the "update all assets"
action) drove the very same prepare fan-out the store WINDOW uses. For
every catalog entry -- installed or not -- it fetched manifest.json,
fetched attribution.json, downloaded the thumbnail and PIL-decoded it,
just to decide whether the handful of INSTALLED assets need an update.
Three requests and an image decode per entry, on every launch, for entries
the user never installed.

The catalog already answers the question on its own: each entry pins a
version -> commit sha map, and install_* stamps the sha it installed into
the asset's VERSION file under a directory named after the asset id. So
"which installed assets are out of date" is a comparison between the
catalog json and a directory listing -- the manifest, the attribution and
the thumbnail only ever mattered for DISPLAYING an entry.

The contract is now:
  * an update check fetches the catalog files and nothing else -- no
    thumbnail is fetched or decoded at all, and an entry that is not
    installed costs no request of its own;
  * a branch-pinned entry (custom plugins name a branch, not a version
    map) still resolves its tip, because no version map exists to match --
    but when the tip is already installed it does not go on to fetch the
    manifest;
  * an installed, out-of-date entry is still detected and reinstalled at
    the newest compatible commit;
  * the store WINDOW's full prepare (include_images=True) is untouched.
"""

import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from PIL import Image

import src.backend.Store.StoreBackend as store_backend_module
from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError


APP_MAJOR = int(gl.app_version.split(".")[0])
OLD_VERSION = f"{APP_MAJOR}.0.0"
NEW_VERSION = f"{APP_MAJOR}.1.0"

STORE_BRANCH = "1.5.0"


def _sha(seed: str) -> str:
    """A 40 hex char stand-in for a commit sha, derived from a name so
    failures name the entry they came from."""
    body = "".join(c for c in seed.lower() if c in "0123456789abcdef") or "a"
    return (body * 40)[:40]


class _Entry:
    """One catalog entry plus the local state that decides its verdict."""

    def __init__(self, repo: str, asset_id: str, installed_version: str | None):
        self.repo = repo
        self.asset_id = asset_id
        self.url = f"https://github.com/acme/{repo}"
        self.commits = {OLD_VERSION: _sha(repo + "old"), NEW_VERSION: _sha(repo + "new")}
        self.installed_version = installed_version

    @property
    def newest_sha(self) -> str:
        return self.commits[NEW_VERSION]

    @property
    def local_sha(self) -> str | None:
        if self.installed_version is None:
            return None
        return self.commits[self.installed_version]

    def catalog_json(self) -> dict:
        return {"url": self.url, "commits": dict(self.commits)}

    def install(self, base_dir: str) -> None:
        """Put the entry on disk the way install_* leaves it: a directory
        named after the asset id, holding the manifest and a VERSION file
        stamped with the commit it was installed at."""
        if self.installed_version is None:
            return
        path = os.path.join(base_dir, self.asset_id)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "manifest.json"), "w") as f:
            json.dump({"id": self.asset_id, "name": self.repo, "version": self.installed_version}, f)
        with open(os.path.join(path, "VERSION"), "w") as f:
            f.write(self.local_sha or "")


# 6 uninstalled + 2 installed plugins (one current, one outdated), plus a
# smaller spread of each of the other three asset classes.
PLUGINS = [
    _Entry(f"Uninstalled{i}Plugin", f"com_acme_Uninstalled{i}Plugin", None) for i in range(6)
] + [
    _Entry("CurrentPlugin", "com_acme_CurrentPlugin", NEW_VERSION),
    _Entry("OutdatedPlugin", "com_acme_OutdatedPlugin", OLD_VERSION),
]
ICONS = [
    _Entry("UninstalledIcons0", "com_acme_UninstalledIcons0", None),
    _Entry("UninstalledIcons1", "com_acme_UninstalledIcons1", None),
    _Entry("OutdatedIcons", "com_acme_OutdatedIcons", OLD_VERSION),
]
WALLPAPERS = [
    _Entry("UninstalledWalls0", "com_acme_UninstalledWalls0", None),
    _Entry("UninstalledWalls1", "com_acme_UninstalledWalls1", None),
    _Entry("CurrentWalls", "com_acme_CurrentWalls", NEW_VERSION),
]
SD_PLUS = [
    _Entry("UninstalledBars0", "com_acme_UninstalledBars0", None),
    _Entry("UninstalledBars1", "com_acme_UninstalledBars1", None),
    _Entry("OutdatedBars", "com_acme_OutdatedBars", OLD_VERSION),
]

ALL_ENTRIES = PLUGINS + ICONS + WALLPAPERS + SD_PLUS

CATALOGS = {
    StoreBackend.PLUGIN_FILE: PLUGINS,
    StoreBackend.ICON_FILE: ICONS,
    StoreBackend.WALLPAPERS_FILE: WALLPAPERS,
    StoreBackend.SDPLUSWALLPAPERS_FILE: SD_PLUS,
}


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


THUMBNAIL_BYTES = _png_bytes()


class _Answer:
    """What request_from_url hands back: get_remote_file reads .text for a
    text fetch and .content for a binary one."""

    def __init__(self, text: str = "", content: bytes = b""):
        self.text = text
        self.content = content


class _FakeStore:
    """Serves the catalog, manifests, attributions and thumbnails from
    memory and counts every request by kind."""

    def __init__(self):
        self.urls: list[str] = []
        self.image_fetches = 0
        self.image_decodes = 0
        self._lock = threading.Lock()

    def request_from_url(self, url: str) -> "_Answer | NoConnectionError":
        with self._lock:
            self.urls.append(url)
        path = url.split("/", 5)[-1] if url.count("/") >= 5 else url
        # Exact filename, not a suffix: "SDPlusBarWallpapers.json" ends with
        # "Wallpapers.json", and serving the wrong catalog for it hid a whole
        # asset class from the count.
        entries = CATALOGS.get(path.rsplit("/", 1)[-1])
        if entries is not None:
            return _Answer(text=json.dumps([e.catalog_json() for e in entries]))
        if path.endswith("manifest.json"):
            entry = self._entry_for(url)
            if entry is None:
                return NoConnectionError()
            return _Answer(text=json.dumps({
                "id": entry.asset_id,
                "name": entry.repo,
                "version": NEW_VERSION,
                "thumbnail": "store/Thumbnail.png",
            }))
        if path.endswith("attribution.json"):
            # Optional in the real store, so most entries 404 -- which still
            # costs the request that this scenario is here to count.
            return NoConnectionError()
        if path.endswith(".png"):
            with self._lock:
                self.image_fetches += 1
            return _Answer(content=THUMBNAIL_BYTES)
        return NoConnectionError()

    @staticmethod
    def _entry_for(url: str) -> "_Entry | None":
        for entry in ALL_ENTRIES:
            if f"/acme/{entry.repo}/" in url:
                return entry
        return None

    def requests_naming(self, repo: str) -> list[str]:
        return [url for url in self.urls if f"/acme/{repo}/" in url]

    @property
    def catalog_requests(self) -> list[str]:
        return [url for url in self.urls if url.rsplit("/", 1)[-1] in CATALOGS]

    @property
    def non_catalog_requests(self) -> list[str]:
        catalog = set(self.catalog_requests)
        return [url for url in self.urls if url not in catalog]


def _make_backend(store: _FakeStore) -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    # What __init__ would have built for the catalog fan-out.
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb._prepare_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="store-prepare")
    sb.official_authors = []
    # Pin the branch so the versions.json lookup is not part of the count.
    sb.official_store_branch_cache = STORE_BRANCH
    sb.request_from_url = store.request_from_url
    return sb


def _install_everything() -> None:
    for entry in PLUGINS:
        entry.install(gl.PLUGIN_DIR)
    for entry in ICONS:
        entry.install(os.path.join(gl.DATA_PATH, "icons"))
    for entry in WALLPAPERS:
        entry.install(os.path.join(gl.DATA_PATH, "wallpapers"))
    for entry in SD_PLUS:
        entry.install(os.path.join(gl.DATA_PATH, "sd_plus_bar_wallpapers"))


def _count_image_decodes(store: _FakeStore):
    real_open = store_backend_module.Image.open

    def counting_open(*args, **kwargs):
        store.image_decodes += 1
        return real_open(*args, **kwargs)

    store_backend_module.Image.open = counting_open
    return real_open


def test_update_everything_stays_off_the_network_for_uninstalled_entries() -> None:
    fixtures.install_stub_globals()
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: None)
    _install_everything()

    store = _FakeStore()
    sb = _make_backend(store)
    real_open = _count_image_decodes(store)

    installed: list[tuple[str, str]] = []
    sb.install_plugin = lambda data, auto_update=False: (
        installed.append((data.plugin_id, data.commit_sha)) or True
    )
    sb.install_icon = lambda data: (installed.append((data.icon_id, data.commit_sha)) or 200)
    sb.install_wallpaper = lambda data: (
        installed.append((data.wallpaper_id, data.commit_sha)) or 200
    )
    sb.install_sd_plus_bar_wallpaper = lambda data: (
        installed.append((data.id, data.commit_sha)) or 200
    )

    try:
        n_updated = sb.update_everything()
    finally:
        store_backend_module.Image.open = real_open

    print(
        f"scenario_store_update_check_cost: {len(ALL_ENTRIES)} catalog entries "
        f"({sum(1 for e in ALL_ENTRIES if e.installed_version is None)} uninstalled, "
        f"{sum(1 for e in ALL_ENTRIES if e.installed_version is not None)} installed) -> "
        f"{len(store.urls)} requests "
        f"({len(store.catalog_requests)} catalog, {len(store.non_catalog_requests)} per-entry), "
        f"{store.image_fetches} image fetches, {store.image_decodes} image decodes"
    )

    assert n_updated == 3, (
        f"the three outdated installed assets must be updated, got {n_updated!r}"
    )
    assert sorted(installed) == sorted([
        ("com_acme_OutdatedPlugin", PLUGINS[7].newest_sha),
        ("com_acme_OutdatedIcons", ICONS[2].newest_sha),
        ("com_acme_OutdatedBars", SD_PLUS[2].newest_sha),
    ]), (
        "exactly the outdated installed assets must be reinstalled at the "
        f"newest compatible commit, got {installed}"
    )

    assert store.image_fetches == 0, (
        f"an update check must not download thumbnails, got {store.image_fetches}"
    )
    assert store.image_decodes == 0, (
        f"an update check must not decode images, got {store.image_decodes}"
    )

    for entry in ALL_ENTRIES:
        if entry.installed_version is not None:
            continue
        assert store.requests_naming(entry.repo) == [], (
            f"an entry the user never installed ({entry.repo}) must cost no "
            f"request of its own, got {store.requests_naming(entry.repo)}"
        )

    assert store.non_catalog_requests == [], (
        "a commit-pinned catalog answers the update question on its own -- "
        f"nothing beyond the catalog files may be fetched, got {store.non_catalog_requests}"
    )
    assert len(store.catalog_requests) == len(CATALOGS), (
        f"exactly one fetch per catalog file, got {store.catalog_requests}"
    )


def test_branch_pinned_entry_resolves_its_tip_but_not_its_manifest() -> None:
    """A custom plugin names a branch, not a version map, so there is no
    commit list to match against what is installed: its tip has to be
    resolved. An already-current one must stop there -- the manifest would
    only restate the id the install directory already carries."""
    fixtures.install_stub_globals(app_settings={
        "store": {
            "enable-custom-plugins": True,
            "custom-plugins": [{"url": "https://github.com/acme/CustomPlugin", "branch": "main"}],
        },
    })
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: None)

    tip = _sha("customtip")
    path = os.path.join(gl.PLUGIN_DIR, "com_acme_CustomPlugin")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "VERSION"), "w") as f:
        f.write(tip)

    store = _FakeStore()
    sb = _make_backend(store)
    commit_lookups: list[str] = []

    def fake_last_commit(repo_url: str, branch_name: str = "main"):
        commit_lookups.append(f"{repo_url}@{branch_name}")
        return tip

    sb.get_last_commit = fake_last_commit
    sb.install_plugin = lambda data, auto_update=False: True

    to_update = sb.get_plugins_to_update()
    assert not isinstance(to_update, NoConnectionError)
    ids = [plugin.plugin_id for plugin in to_update]
    assert "com_acme_CustomPlugin" not in ids, (
        f"a custom plugin sitting on the branch tip needs no update, got {ids}"
    )
    assert commit_lookups == ["https://github.com/acme/CustomPlugin@main"], (
        f"exactly one tip lookup for the one branch-pinned entry, got {commit_lookups}"
    )
    assert store.non_catalog_requests == [], (
        "a branch-pinned entry whose tip is already installed must not go on "
        f"to fetch its manifest, got {store.non_catalog_requests}"
    )


def test_store_window_still_gets_the_full_prepare() -> None:
    """The store window builds its rows from the same prepare functions
    with include_images=True: those still fetch the manifest, the
    attribution and the thumbnail for every entry, installed or not."""
    fixtures.install_stub_globals()
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: None)

    store = _FakeStore()
    sb = _make_backend(store)
    real_open = _count_image_decodes(store)
    try:
        plugins = sb.get_all_plugins()
    finally:
        store_backend_module.Image.open = real_open

    assert not isinstance(plugins, NoConnectionError)
    assert len(plugins) == len(PLUGINS), (
        f"the store window must still list every catalog entry, got {len(plugins)}"
    )
    assert store.image_fetches == len(PLUGINS), (
        f"every listed entry must still get its thumbnail, got {store.image_fetches}"
    )
    assert store.image_decodes == len(PLUGINS), (
        f"every fetched thumbnail must still be decoded, got {store.image_decodes}"
    )
    uninstalled = next(e for e in PLUGINS if e.installed_version is None)
    assert store.requests_naming(uninstalled.repo), (
        "the store window must still describe entries the user has not installed"
    )
    outdated = next(p for p in plugins if p.plugin_id == "com_acme_OutdatedPlugin")
    assert outdated.local_sha == PLUGINS[7].local_sha, (
        f"the full prepare must still read the installed sha, got {outdated.local_sha!r}"
    )


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_update_check_cost")
    test_update_everything_stays_off_the_network_for_uninstalled_entries()
    test_branch_pinned_entry_resolves_its_tip_but_not_its_manifest()
    test_store_window_still_gets_the_full_prepare()
    print("scenario_store_update_check_cost: PASS")


if __name__ == "__main__":
    main()
