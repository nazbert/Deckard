"""
Regression test -- the boot update check cost a full store browse, and
identity by catalog sha silently stopped updating everything, exercised
WITHOUT network:

1. update_everything() (startup auto-update, and the "update all assets"
   action) drove the very same prepare fan-out the store WINDOW uses. For
   every catalog entry -- installed or not -- it fetched manifest.json,
   fetched attribution.json, downloaded the thumbnail and PIL-decoded it,
   just to decide whether the handful of INSTALLED assets need an update.
   Three requests and an image decode per entry, on every launch, for
   entries the user never installed.

2. Deciding instead that "an entry is installed when one of the commits it
   lists is on disk" is wrong for the store as it is actually maintained:
   almost every entry carries a SINGLE version key whose sha is REPLACED in
   place when the asset is bumped. The sha an install sits on therefore
   stops being listed at exactly the moment an update exists, so the entry
   reads as not installed and auto-update becomes a permanent silent no-op.

The link the catalog cannot supply is which repository an installed
directory came from: entries name repositories, install directories are
named after manifest ids. So install_* stamps the origin repository into
the tree next to VERSION, and identity is that stamp. Installs that predate
it are identified once through their manifest -- bounded by the number of
installs, not the size of the catalog -- and stamped as they are.

The contract is now:
  * an update check fetches the catalog files and nothing else once the
    installs are stamped -- no thumbnail is fetched or decoded at all, and
    an entry that is not installed costs no request of its own;
  * an install whose catalog sha was rewritten in place is still detected
    and updated;
  * a directory copied aside under another name never claims an entry, and
    is never installed over (that download is doomed by the staged-id
    check);
  * two entries that share a commit resolve to their own installs;
  * an install with no readable sha is repaired, except a symlinked one,
    which belongs to whoever made the link;
  * a branch-pinned entry still resolves its tip, because no version map
    exists to match -- but not its manifest when that tip is installed;
  * the store WINDOW's full prepare (include_images=True) is untouched, and
    backfills the stamp for what it identifies.
"""

import io
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from PIL import Image

import src.backend.Store.StoreBackend as store_backend_module
from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Ok, Err, StoreFetchError


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
    """One catalog entry plus the local state that decides its verdict.

    `installed` is which sha the install directory holds ("old", "new" or
    None for not installed); `lists_old_sha` mirrors the two shapes the real
    store produces -- one version key whose sha is replaced in place
    (default), or a version map that still lists the older release.
    """

    def __init__(self, repo: str, asset_id: str, installed: str | None = None,
                 lists_old_sha: bool = False, stamped: bool = True,
                 symlink: bool = False, readable_sha: bool = True,
                 shared_sha: str | None = None, branch: str | None = None,
                 stamped_url: str | None = None, bad_manifest: bool = False,
                 versions: dict | None = None):
        self.repo = repo
        self.asset_id = asset_id
        self.url = f"https://github.com/acme/{repo}"
        # What the ORIGIN file says, when that is not the entry's own url:
        # a repository that was renamed or transferred leaves the tree
        # stamped with the name it was installed under.
        self.stamped_url = stamped_url
        # Serves something that is not json where the manifest should be.
        self.bad_manifest = bad_manifest
        # A catalog whose version keys are not versions.
        self.versions = versions
        self.old_sha = _sha(repo + "old")
        self.new_sha = shared_sha or _sha(repo + "new")
        self.installed = installed
        self.lists_old_sha = lists_old_sha
        self.stamped = stamped
        self.symlink = symlink
        self.readable_sha = readable_sha
        self.branch = branch

    @property
    def local_sha(self) -> str | None:
        if self.installed is None:
            return None
        return self.old_sha if self.installed == "old" else self.new_sha

    def catalog_json(self) -> dict:
        if self.branch is not None:
            return {"url": self.url, "branch": self.branch}
        if self.versions is not None:
            return {"url": self.url, "commits": dict(self.versions)}
        commits = {NEW_VERSION: self.new_sha}
        if self.lists_old_sha:
            commits = {OLD_VERSION: self.old_sha, NEW_VERSION: self.new_sha}
        return {"url": self.url, "commits": commits}

    def install(self, base_dir: str, as_name: str | None = None) -> str | None:
        """Put the entry on disk the way install_* leaves it: a directory
        named after the asset id, holding the manifest, a VERSION file
        stamped with the commit it was installed at, and the ORIGIN stamp
        naming the repository it came from."""
        if self.installed is None:
            return None
        os.makedirs(base_dir, exist_ok=True)
        path = os.path.join(base_dir, as_name or self.asset_id)
        if self.symlink:
            target = os.path.join(gl.DATA_PATH, "checkouts", self.asset_id)
            os.makedirs(target, exist_ok=True)
            _write_asset_files(target, self.asset_id, self.local_sha,
                               (self.stamped_url or self.url) if self.stamped else None,
                               self.readable_sha)
            if os.path.islink(path):
                os.remove(path)
            os.symlink(target, path)
            return path
        os.makedirs(path, exist_ok=True)
        _write_asset_files(path, self.asset_id, self.local_sha,
                           (self.stamped_url or self.url) if self.stamped else None,
                           self.readable_sha)
        return path


def _write_asset_files(path: str, asset_id: str, sha: str | None,
                       origin: str | None, readable_sha: bool) -> None:
    with open(os.path.join(path, "manifest.json"), "w") as f:
        json.dump({"id": asset_id, "name": asset_id, "version": NEW_VERSION}, f)
    if readable_sha and sha is not None:
        with open(os.path.join(path, "VERSION"), "w") as f:
            f.write(sha)
    if origin is not None:
        with open(os.path.join(path, StoreBackend.ORIGIN_FILE), "w") as f:
            f.write(origin + "\n")


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
    """Serves the given catalogs, plus manifests, attributions and
    thumbnails, from memory -- and counts every request by kind."""

    def __init__(self, catalogs: dict):
        self.catalogs = catalogs
        self.entries = [entry for entries in catalogs.values() for entry in entries]
        self.urls: list[str] = []
        self.manifest_fetches = 0
        self.image_fetches = 0
        self.image_decodes = 0
        self._lock = threading.Lock()

    def request_from_url(self, url: str) -> "_Answer":
        with self._lock:
            self.urls.append(url)
        path = url.split("/", 5)[-1] if url.count("/") >= 5 else url
        # Exact filename, not a suffix: "SDPlusBarWallpapers.json" ends with
        # "Wallpapers.json", and serving the wrong catalog for it hid a whole
        # asset class from the count.
        entries = self.catalogs.get(path.rsplit("/", 1)[-1])
        if entries is not None:
            return _Answer(text=json.dumps([e.catalog_json() for e in entries]))
        if path.endswith("manifest.json"):
            entry = self._entry_for(url)
            if entry is None:
                raise StoreFetchError(url, "not found")
            if entry.bad_manifest:
                # What a truncated write or a proxy error page serves where
                # json is expected.
                with self._lock:
                    self.manifest_fetches += 1
                return _Answer(text="<html>504 Gateway Timeout</html>")
            with self._lock:
                self.manifest_fetches += 1
            return _Answer(text=json.dumps({
                "id": entry.asset_id,
                "name": entry.repo,
                "version": NEW_VERSION,
                "thumbnail": "store/Thumbnail.png",
            }))
        if path.endswith("attribution.json"):
            # Optional in the real store, so most entries 404 -- which still
            # costs the request that this scenario is here to count.
            raise StoreFetchError(url, "not found")
        if path.endswith(".png"):
            with self._lock:
                self.image_fetches += 1
            return _Answer(content=THUMBNAIL_BYTES)
        raise StoreFetchError(url, "not found")

    def _entry_for(self, url: str) -> "_Entry | None":
        for entry in self.entries:
            if f"/acme/{entry.repo}/" in url:
                return entry
        return None

    def requests_naming(self, repo: str) -> list[str]:
        return [url for url in self.urls if f"/acme/{repo}/" in url]

    @property
    def catalog_requests(self) -> list[str]:
        return [url for url in self.urls if url.rsplit("/", 1)[-1] in self.catalogs]

    @property
    def non_catalog_requests(self) -> list[str]:
        catalog = set(self.catalog_requests)
        return [url for url in self.urls if url not in catalog]


def _catalogs(plugins=(), icons=(), wallpapers=(), sd_plus=()) -> dict:
    return {
        StoreBackend.PLUGIN_FILE: list(plugins),
        StoreBackend.ICON_FILE: list(icons),
        StoreBackend.WALLPAPERS_FILE: list(wallpapers),
        StoreBackend.SDPLUSWALLPAPERS_FILE: list(sd_plus),
    }


def _asset_dirs(sb: StoreBackend) -> tuple[str, ...]:
    return (gl.PLUGIN_DIR, sb.icons_dir(), sb.wallpapers_dir(),
            sb.sd_plus_bar_wallpapers_dir())


def _reset_local_state() -> None:
    """Every test starts from a known disk: no installs, no store cache
    (a cached manifest would hide a fetch this scenario counts)."""
    sb = StoreBackend.__new__(StoreBackend)
    for base_dir in _asset_dirs(sb) + (os.path.join(gl.DATA_PATH, "checkouts"),):
        shutil.rmtree(base_dir, ignore_errors=True)
        os.makedirs(base_dir, exist_ok=True)
    shutil.rmtree(os.path.join(gl.DATA_PATH, "Store"), ignore_errors=True)


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


class _Offline:
    """Nothing in this scenario may reach the network. request_from_url is
    stubbed per backend, but get_last_commit and download_to_file call the
    shared session directly -- so the session itself is made to refuse."""

    def __enter__(self):
        self._get = store_backend_module.http_client.get
        self._download = store_backend_module.http_client.download_to_file

        def refuse(*args, **kwargs):
            raise AssertionError(f"the update check reached the network: {args!r}")

        store_backend_module.http_client.get = refuse
        store_backend_module.http_client.download_to_file = refuse
        return self

    def __exit__(self, *exc):
        store_backend_module.http_client.get = self._get
        store_backend_module.http_client.download_to_file = self._download
        return False


class _CountingDecodes:
    def __init__(self, store: _FakeStore):
        self.store = store

    def __enter__(self):
        self._real_open = store_backend_module.Image.open

        def counting_open(*args, **kwargs):
            self.store.image_decodes += 1
            return self._real_open(*args, **kwargs)

        store_backend_module.Image.open = counting_open
        return self

    def __exit__(self, *exc):
        store_backend_module.Image.open = self._real_open
        return False


# The fields install_* actually reads off the data object it is handed. An
# update-check view that leaves any of them unset installs nothing (or
# installs the wrong thing), so the stubs below refuse instead of counting.
INSTALL_FIELDS = {
    "plugin": ("github", "plugin_id", "commit_sha"),
    "icon": ("github", "icon_id", "commit_sha"),
    "wallpaper": ("github", "wallpaper_id", "commit_sha"),
    "sd_plus": ("github", "id", "commit_sha"),
}


def _recording_installs(sb: StoreBackend) -> list[tuple[str, str]]:
    """Stubs every install_* to record (asset id, commit sha) after
    asserting the data object carries everything the real one dereferences."""
    installed: list[tuple[str, str]] = []

    def record(kind: str, data, ok):
        for field in INSTALL_FIELDS[kind]:
            assert getattr(data, field), (
                f"install_{kind} reads {field}, which the update check left "
                f"{getattr(data, field)!r} on {data!r}"
            )
        assert hasattr(data, "branch"), "install_plugin reads branch"
        assert data.is_compatible is not False, (
            f"an incompatible entry must never reach install_{kind}: {data!r}"
        )
        installed.append((getattr(data, INSTALL_FIELDS[kind][1]), data.commit_sha))
        return ok

    sb.install_plugin = lambda data, auto_update=False: record("plugin", data, True)
    sb.install_icon = lambda data: record("icon", data, 200)
    sb.install_wallpaper = lambda data: record("wallpaper", data, 200)
    sb.install_sd_plus_bar_wallpaper = lambda data: record("sd_plus", data, 200)
    return installed


def _stub_globals(**kwargs) -> None:
    fixtures.install_stub_globals(**kwargs)
    gl.lm = SimpleNamespace(get_custom_translation=lambda translations: None)


# 6 uninstalled + 2 installed plugins, plus a smaller spread of the other
# three asset classes. The outdated ones carry the shape the real store
# produces: ONE version key whose sha was replaced in place, so the sha the
# install sits on is no longer listed anywhere in the catalog.
def _main_catalogs() -> dict:
    plugins = [_Entry(f"Uninstalled{i}Plugin", f"com_acme_Uninstalled{i}Plugin") for i in range(6)]
    plugins += [
        _Entry("CurrentPlugin", "com_acme_CurrentPlugin", installed="new"),
        _Entry("OutdatedPlugin", "com_acme_OutdatedPlugin", installed="old"),
    ]
    icons = [
        _Entry("UninstalledIcons0", "com_acme_UninstalledIcons0"),
        _Entry("UninstalledIcons1", "com_acme_UninstalledIcons1"),
        _Entry("OutdatedIcons", "com_acme_OutdatedIcons", installed="old"),
    ]
    wallpapers = [
        _Entry("UninstalledWalls0", "com_acme_UninstalledWalls0"),
        _Entry("UninstalledWalls1", "com_acme_UninstalledWalls1"),
        _Entry("CurrentWalls", "com_acme_CurrentWalls", installed="new"),
    ]
    sd_plus = [
        _Entry("UninstalledBars0", "com_acme_UninstalledBars0"),
        _Entry("UninstalledBars1", "com_acme_UninstalledBars1"),
        _Entry("OutdatedBars", "com_acme_OutdatedBars", installed="old"),
    ]
    return _catalogs(plugins, icons, wallpapers, sd_plus)


def _install_catalogs(sb: StoreBackend, catalogs: dict) -> None:
    for filename, base_dir in (
        (StoreBackend.PLUGIN_FILE, gl.PLUGIN_DIR),
        (StoreBackend.ICON_FILE, sb.icons_dir()),
        (StoreBackend.WALLPAPERS_FILE, sb.wallpapers_dir()),
        (StoreBackend.SDPLUSWALLPAPERS_FILE, sb.sd_plus_bar_wallpapers_dir()),
    ):
        for entry in catalogs[filename]:
            entry.install(base_dir)


def test_update_everything_stays_off_the_network_for_uninstalled_entries() -> None:
    _stub_globals()
    _reset_local_state()

    catalogs = _main_catalogs()
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)
    installed = _recording_installs(sb)

    with _Offline(), _CountingDecodes(store):
        n_updated = sb.update_everything()

    entries = store.entries
    print(
        f"scenario_store_update_check_cost: {len(entries)} catalog entries "
        f"({sum(1 for e in entries if e.installed is None)} uninstalled, "
        f"{sum(1 for e in entries if e.installed is not None)} installed) -> "
        f"{len(store.urls)} requests "
        f"({len(store.catalog_requests)} catalog, {len(store.non_catalog_requests)} per-entry), "
        f"{store.manifest_fetches} manifest fetches, "
        f"{store.image_fetches} image fetches, {store.image_decodes} image decodes"
    )

    # The identity regression: every outdated entry here had its sha
    # replaced in place under the same version key, which is what the real
    # store does on a bump.
    assert isinstance(n_updated, Ok) and n_updated.value == 3, (
        f"the three outdated installed assets must be updated -- an install "
        f"whose sha the catalog replaced in place is still that install, "
        f"got {n_updated!r}"
    )
    assert sorted(installed) == sorted([
        ("com_acme_OutdatedPlugin", _sha("OutdatedPluginnew")),
        ("com_acme_OutdatedIcons", _sha("OutdatedIconsnew")),
        ("com_acme_OutdatedBars", _sha("OutdatedBarsnew")),
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

    for entry in entries:
        if entry.installed is not None:
            continue
        assert store.requests_naming(entry.repo) == [], (
            f"an entry the user never installed ({entry.repo}) must cost no "
            f"request of its own, got {store.requests_naming(entry.repo)}"
        )

    assert store.non_catalog_requests == [], (
        "stamped installs answer the update question locally -- nothing "
        f"beyond the catalog files may be fetched, got {store.non_catalog_requests}"
    )
    assert len(store.catalog_requests) == 4, (
        f"exactly one fetch per catalog file, got {store.catalog_requests}"
    )


def test_backup_directory_never_claims_the_entry() -> None:
    """A directory copied aside carries the same ORIGIN stamp as the real
    install. It must never be the one an entry resolves to: its name is not
    the id its manifest claims, so installing over it would download an
    archive on every launch only for the staged-id check to refuse it."""
    _stub_globals()
    _reset_local_state()

    entry = _Entry("Alpha", "com_acme_Alpha", installed="old")
    catalogs = _catalogs(plugins=[entry])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    entry.install(gl.PLUGIN_DIR)
    entry.install(gl.PLUGIN_DIR, as_name="com_acme_Alpha_backup")
    installed = _recording_installs(sb)

    with _Offline():
        to_update = sb.update_all_plugins()

    assert isinstance(to_update, Ok) and to_update.value == 1, (
        f"exactly the real install may be updated, got {to_update!r}"
    )
    assert installed == [("com_acme_Alpha", entry.new_sha)], (
        f"the copy kept aside must never be installed over, got {installed}"
    )

    # And with ONLY the copy present there is nothing safe to update.
    _reset_local_state()
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    entry.install(gl.PLUGIN_DIR, as_name="com_acme_Alpha_backup")
    installed = _recording_installs(sb)
    with _Offline():
        n = sb.update_all_plugins()
    assert isinstance(n, Ok) and (n.value, installed) == (0, []), (
        f"a directory whose name is not its manifest id must not be installed "
        f"over -- that download is refused by the staged-id check, got {n}, {installed}"
    )


def test_two_entries_sharing_a_commit_resolve_to_their_own_install() -> None:
    """A fork republished at the same commit gives two entries the same
    sha. Identity is the repository each install came from, so each entry
    resolves to its own directory and neither claims the other's."""
    _stub_globals()
    _reset_local_state()

    shared = _sha("sharedforkcommit")
    upstream = _Entry("Upstream", "com_acme_Upstream", installed="old", shared_sha=shared)
    fork = _Entry("Fork", "com_acme_Fork", installed="old", shared_sha=shared)
    catalogs = _catalogs(plugins=[upstream, fork])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    upstream.install(gl.PLUGIN_DIR)
    fork.install(gl.PLUGIN_DIR)

    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert not isinstance(to_update, Err)
    ids = sorted(plugin.plugin_id for plugin in to_update.value)
    assert ids == ["com_acme_Fork", "com_acme_Upstream"], (
        f"each entry must resolve to its own install, got {ids}"
    )
    for plugin in to_update.value:
        assert plugin.plugin_id.split("_")[-1] in plugin.github, (
            f"{plugin.plugin_id} resolved to {plugin.github}"
        )


def test_legacy_install_is_identified_once_and_then_stamped() -> None:
    """An install made before the stamp existed is identified the old way --
    one manifest fetch, matched against the directory names -- and stamped
    as it is, so the cost is one fetch per surviving legacy install, once,
    and nothing on any later launch."""
    _stub_globals()
    _reset_local_state()

    legacy = _Entry("Legacy", "com_acme_Legacy", installed="old", stamped=False)
    others = [_Entry(f"Other{i}", f"com_acme_Other{i}") for i in range(5)]
    catalogs = _catalogs(plugins=[legacy] + others)
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    print(
        f"scenario_store_update_check_cost: legacy sweep over "
        f"{len(catalogs[StoreBackend.PLUGIN_FILE])} entries with 1 unstamped install "
        f"-> {store.manifest_fetches} manifest fetches, {store.image_fetches} image fetches"
    )
    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Legacy", legacy.new_sha)]), (
        f"the legacy install must still be identified and updated, got {n}, {installed}"
    )
    assert store.image_fetches == 0, "the legacy lookup must not fetch images"
    assert store.manifest_fetches == 1, (
        f"identifying one legacy install must cost one manifest fetch, not one "
        f"per catalog entry, got {store.manifest_fetches}"
    )

    origin = os.path.join(gl.PLUGIN_DIR, "com_acme_Legacy", StoreBackend.ORIGIN_FILE)
    assert os.path.isfile(origin), "identifying a legacy install must stamp it"
    with open(origin) as f:
        assert f.read().strip() == legacy.url

    # Second launch, cold store cache: the stamp answers, so nothing is fetched.
    shutil.rmtree(os.path.join(gl.DATA_PATH, "Store"), ignore_errors=True)
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _recording_installs(sb)
    with _Offline():
        sb.update_all_plugins()
    assert store.manifest_fetches == 0, (
        f"a stamped install must cost no manifest fetch, got {store.manifest_fetches}"
    )
    assert store.non_catalog_requests == [], (
        f"nothing beyond the catalog may be fetched once stamped, got {store.non_catalog_requests}"
    )


def test_symlinked_install_is_left_to_its_owner() -> None:
    """A symlinked install points at a working tree the user manages.
    Installing over it replaces the link with a downloaded copy, so
    auto-update leaves it alone -- deliberately, not by accident."""
    _stub_globals()
    _reset_local_state()

    # A link whose target was once installed carries the stamp, so the
    # entry resolves to it and the symlink guard is what stops the update.
    linked = _Entry("Linked", "com_acme_Linked", installed="old", symlink=True)
    catalogs = _catalogs(plugins=[linked])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    path = linked.install(gl.PLUGIN_DIR)
    assert path is not None and os.path.islink(path)

    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert not isinstance(to_update, Err)
    assert to_update.value == [], (
        f"a stamped symlinked install must not be auto-updated, got {to_update}"
    )

    # An unstamped link is not a pending legacy install either: it is never
    # looked up, and nothing is written into the tree behind it.
    _reset_local_state()
    unstamped = _Entry("Linked", "com_acme_Linked", installed="old", symlink=True, stamped=False)
    store = _FakeStore(_catalogs(plugins=[unstamped]))
    sb = _make_backend(store)
    path = unstamped.install(gl.PLUGIN_DIR)
    assert path is not None
    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert isinstance(to_update, Ok) and to_update.value == [], (
        f"an unstamped symlink must not be auto-updated, got {to_update}"
    )
    assert store.manifest_fetches == 0, (
        f"a symlinked install must not be looked up at all, got {store.manifest_fetches}"
    )
    assert not os.path.exists(os.path.join(path, StoreBackend.ORIGIN_FILE)), (
        "nothing may be written into a tree this app does not own"
    )


def test_install_with_unreadable_sha_is_repaired() -> None:
    """Neither .git nor VERSION: a half-written or hand-copied tree. It
    compares unequal to every commit, so it is reinstalled -- the repair the
    old manifest-id identity performed and that must not be lost."""
    _stub_globals()
    _reset_local_state()

    broken = _Entry("Broken", "com_acme_Broken", installed="old", readable_sha=False)
    catalogs = _catalogs(plugins=[broken])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    broken.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Broken", broken.new_sha)]), (
        f"an install with no readable sha must be repaired, got {n}, {installed}"
    )


def test_branch_pinned_entry_resolves_its_tip_but_not_its_manifest() -> None:
    """A custom plugin names a branch, not a version map, so its tip has to
    be resolved. Identity still comes from the stamp, so the manifest that
    would only restate the id is never fetched."""
    _reset_local_state()
    _stub_globals(app_settings={
        "store": {
            "enable-custom-plugins": True,
            "custom-plugins": [{"url": "https://github.com/acme/CustomPlugin", "branch": "main"}],
        },
    })

    custom = _Entry("CustomPlugin", "com_acme_CustomPlugin", installed="new", branch="main")
    store = _FakeStore(_catalogs())
    sb = _make_backend(store)
    custom.install(gl.PLUGIN_DIR)

    commit_lookups: list[str] = []

    def fake_last_commit(repo_url: str, branch_name: str = "main"):
        commit_lookups.append(f"{repo_url}@{branch_name}")
        return custom.new_sha

    sb.get_last_commit = fake_last_commit
    _recording_installs(sb)

    with _Offline():
        to_update = sb.get_plugins_to_update()

    assert not isinstance(to_update, Err)
    ids = [plugin.plugin_id for plugin in to_update.value]
    assert "com_acme_CustomPlugin" not in ids, (
        f"a custom plugin sitting on the branch tip needs no update, got {ids}"
    )
    assert commit_lookups == ["https://github.com/acme/CustomPlugin@main"], (
        f"exactly one tip lookup for the one branch-pinned entry, got {commit_lookups}"
    )
    assert store.manifest_fetches == 0, (
        f"a stamped branch-pinned install must not cost a manifest, got {store.manifest_fetches}"
    )


def test_store_window_still_gets_the_full_prepare() -> None:
    """The store window builds its rows from the same prepare functions
    with include_images=True: those still fetch the manifest, the
    attribution and the thumbnail for every entry, installed or not -- and
    stamp what they identify, so the update check need not."""
    _stub_globals()
    _reset_local_state()

    legacy = _Entry("Legacy", "com_acme_Legacy", installed="old", stamped=False)
    plugins_catalog = [legacy] + [_Entry(f"Other{i}", f"com_acme_Other{i}") for i in range(3)]
    catalogs = _catalogs(plugins=plugins_catalog)
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)

    with _Offline(), _CountingDecodes(store):
        result = sb.get_all_plugins()

    # get_all_* is the typed read boundary now: Ok carrying the catalog list.
    assert isinstance(result, Ok)
    plugins = result.value
    assert len(plugins) == len(plugins_catalog), (
        f"the store window must still list every catalog entry, got {len(plugins)}"
    )
    assert store.image_fetches == len(plugins_catalog), (
        f"every listed entry must still get its thumbnail, got {store.image_fetches}"
    )
    assert store.image_decodes == len(plugins_catalog), (
        f"every fetched thumbnail must still be decoded, got {store.image_decodes}"
    )
    uninstalled = plugins_catalog[1]
    assert store.requests_naming(uninstalled.repo), (
        "the store window must still describe entries the user has not installed"
    )
    shown = next(p for p in plugins if p.plugin_id == "com_acme_Legacy")
    assert shown.local_sha == legacy.local_sha, (
        f"the full prepare must still read the installed sha, got {shown.local_sha!r}"
    )
    assert os.path.isfile(os.path.join(gl.PLUGIN_DIR, "com_acme_Legacy", StoreBackend.ORIGIN_FILE)), (
        "a full prepare that identifies an install must record the link it "
        "paid a request for"
    )


def test_one_bad_entry_does_not_abort_the_identification_pass() -> None:
    """The pre-pass walks REMOTE data: a manifest that is not json (a
    truncated write, a proxy error page) and a catalog whose version keys
    are not versions both raise. It runs before the fan-out and outside its
    collect loop, so a raise there used to abort update_everything with all
    four legs silently doing nothing -- and every existing install is
    unresolved on the first launch after the stamp ships, so the pass runs
    for everyone.
    """
    _stub_globals()
    _reset_local_state()

    legacy = _Entry("Legacy", "com_acme_Legacy", installed="old", stamped=False)
    # Claimed by nothing, so the walk covers the whole catalog including the
    # entries that raise.
    orphan = _Entry("Orphan", "com_acme_Orphan", installed="old", stamped=False)
    broken_manifest = _Entry("BrokenManifest", "com_acme_BrokenManifest", bad_manifest=True)
    broken_versions = _Entry("BrokenVersions", "com_acme_BrokenVersions",
                             versions={"latest": _sha("brokenversions")})
    healthy = _Entry("Healthy", "com_acme_Healthy", installed="old")
    catalogs = _catalogs(plugins=[broken_manifest, broken_versions, legacy, healthy])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    _install_catalogs(sb, catalogs)
    orphan.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and n.value == 2, f"a raising entry must not stop the pass, got {n!r}"
    assert sorted(installed) == sorted([
        ("com_acme_Legacy", legacy.new_sha),
        ("com_acme_Healthy", healthy.new_sha),
    ]), (
        f"every healthy entry must still be processed, got {installed}"
    )
    assert sb._installed_index is None, (
        "the pass state must be cleared however the pass ends"
    )

    # And the orphan nothing claimed is not walked for again this session.
    fetches_after_first_walk = store.manifest_fetches
    with _Offline():
        sb.update_all_plugins()
    assert store.manifest_fetches == fetches_after_first_walk, (
        f"a directory nothing claims must not be walked for again this "
        f"session, got {store.manifest_fetches - fetches_after_first_walk} more fetches"
    )


def test_stamp_naming_a_repository_the_catalog_dropped_is_re_resolved() -> None:
    """A repository that was renamed or transferred leaves the tree stamped
    with a name no entry claims. That is the original silent-no-update shape
    again, so such a stamp is not trusted: the install is identified through
    the manifest like an unstamped one, and the stamp is rewritten."""
    _stub_globals()
    _reset_local_state()

    renamed = _Entry("NewName", "com_acme_Widget", installed="old",
                     stamped_url="https://github.com/acme/OldName")
    catalogs = _catalogs(plugins=[renamed])
    store = _FakeStore(catalogs)
    sb = _make_backend(store)
    renamed.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Widget", renamed.new_sha)]), (
        f"an install whose repository was renamed must still be updated, got {n}, {installed}"
    )
    origin = os.path.join(gl.PLUGIN_DIR, "com_acme_Widget", StoreBackend.ORIGIN_FILE)
    with open(origin) as f:
        assert f.read().strip() == renamed.url, (
            "the stamp must be rewritten to the repository that actually claims the install"
        )


def test_stamp_matches_the_catalog_case_insensitively() -> None:
    """GitHub owner and repository names are case-insensitive, so a tree
    stamped Acme/Widget is the same install the catalog spells acme/Widget
    -- and must not be identified all over again."""
    _stub_globals()
    _reset_local_state()

    entry = _Entry("Widget", "com_acme_Widget", installed="old",
                   stamped_url="https://github.com/ACME/Widget")
    store = _FakeStore(_catalogs(plugins=[entry]))
    sb = _make_backend(store)
    entry.install(gl.PLUGIN_DIR)
    installed = _recording_installs(sb)

    with _Offline():
        n = sb.update_all_plugins()

    assert isinstance(n, Ok) and (n.value, installed) == (1, [("com_acme_Widget", entry.new_sha)]), (
        f"a differently cased stamp names the same repository, got {n}, {installed}"
    )
    assert store.manifest_fetches == 0, (
        f"a differently cased stamp must not need re-identifying, got {store.manifest_fetches}"
    )


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_store_update_check_cost")
    test_update_everything_stays_off_the_network_for_uninstalled_entries()
    test_backup_directory_never_claims_the_entry()
    test_two_entries_sharing_a_commit_resolve_to_their_own_install()
    test_legacy_install_is_identified_once_and_then_stamped()
    test_symlinked_install_is_left_to_its_owner()
    test_install_with_unreadable_sha_is_repaired()
    test_branch_pinned_entry_resolves_its_tip_but_not_its_manifest()
    test_store_window_still_gets_the_full_prepare()
    test_one_bad_entry_does_not_abort_the_identification_pass()
    test_stamp_naming_a_repository_the_catalog_dropped_is_re_resolved()
    test_stamp_matches_the_catalog_case_insensitively()
    print("scenario_store_update_check_cost: PASS")


if __name__ == "__main__":
    main()
