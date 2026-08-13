"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
 
import re
import sys
import zipfile
import requests
import json
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple, TypeGuard
from PIL import Image
from io import BytesIO
from loguru import logger as log
import subprocess
import time
import os
import shutil
from packaging import version
import threading

# Import GLib
from gi.repository import GLib

# Import own modules
from autostart import is_flatpak
from src.backend.Store.StoreCache import StoreCache
from src.backend.Store.StoreURL import RepoRef, parse_repo_url
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.backend import http_client

# Import signals
from src.Signals import Signals

# Import globals
import globals as gl
from src.windows.Store.StoreData import PluginData, IconData, SDPlusBarWallpaperData, WallpaperData
from src.backend.Store.asset_types import (
    ASSET_TYPES,
    AssetTypeDescriptor,
    ICON,
    PLUGIN,
    SD_PLUS_BAR,
    WALLPAPER,
)


class NoConnectionError:
    # Falsy so callers can treat any error result as a failed operation.
    def __bool__(self) -> bool:
        return False


class _ResolvedVersion(NamedTuple):
    """What version resolution decides before an entry is fetched: whether a
    compatible release exists, which commit to fetch, and (plugins only) the
    branch a custom entry pins. _prepare_asset distinguishes this from the
    two early-out sentinels version resolution can also produce -- the
    NoCompatibleVersion class and a NoConnectionError -- by its type."""
    compatible: bool
    commit: str | None
    branch: str | None


def same_repository(a: RepoRef | None, b: RepoRef | None) -> bool:
    """Whether two parsed store urls name the same GitHub repository.

    Applied at the comparison sites only, never inside parse_repo_url:
    GitHub treats owner and repository names case-insensitively, so a tree
    stamped from acme/Widget must still match a catalog entry spelled
    Acme/Widget -- but the cache keys built from those same helpers are
    byte-compatible with what is already on disk and must stay so.
    """
    if a is None or b is None:
        return False
    return (a.user.casefold(), a.repo.casefold()) == (b.user.casefold(), b.repo.casefold())


def repository_key(ref: RepoRef) -> tuple[str, str]:
    """The case-insensitive identity of a repository, for set membership."""
    return (ref.user.casefold(), ref.repo.casefold())


class InstalledAsset(NamedTuple):
    """One directory under an asset directory, read locally.

    asset_id is the directory NAME, which is what install_* installs over
    and what download_repo validates the staged tree against; manifest_id is
    what the tree on disk claims to be. They agree for a canonical install
    and differ for a copy kept aside under another name.
    """
    asset_id: str
    path: str
    sha: str                  # "" when neither .git nor VERSION can be read
    origin: RepoRef | None    # from the ORIGIN stamp; None for a legacy install
    manifest_id: str | None   # None when the tree has no readable manifest
    is_symlink: bool


class UpdateCheck(NamedTuple):
    """What deciding "does this catalog entry need updating" actually needs:
    the installed asset the entry names (if any), the sha that asset is at,
    and the sha it should be at.

    Everything else a store entry carries -- name, descriptions, tags,
    licence, thumbnail -- exists to DISPLAY it, and each of those costs a
    request. Keeping the update check to this tuple is what keeps a launch
    off the network for entries the user never installed.
    """
    url: str
    ref: RepoRef
    asset_id: str | None      # None: the entry is not installed
    local_sha: str | None     # the installed asset's sha, None when not installed
    commit_sha: str | None    # the sha the entry should be installed at
    branch: str | None
    compatible: bool


class StoreBackend:
    STORE_REPO_URL = "https://github.com/StreamController/StreamController-Store" #"https://github.com/StreamController/StreamController-Store"
    STORE_CACHE_PATH = "Store/cache"
    # STORE_CACHE_PATH = os.path.join(gl.DATA_PATH, STORE_CACHE_PATH)
    STORE_BRANCH = "1.5.0"

    # Written into every installed tree next to VERSION: the repository the
    # tree was downloaded from. The catalog names repositories and install
    # directories are named after manifest ids, so without this the two can
    # only be connected by fetching the remote manifest of every entry.
    ORIGIN_FILE = "ORIGIN"

    WALLPAPERS_FILE = "Wallpapers.json"
    PLUGIN_FILE = "Plugins.json"
    ICON_FILE = "Icons.json"
    SDPLUSWALLPAPERS_FILE = "SDPlusBarWallpapers.json"


    # Cap concurrent GitHub fetches: enough to overlap the catalog's ~150
    # small requests (the fetch itself is what dominates store load time),
    # few enough not to present as a scrape burst to raw.githubusercontent.
    # Aliased to the shared session's pool size so the semaphore cap and the
    # connection pool can't drift apart -- a cap above the pool would make
    # the surplus threads open throwaway connections.
    MAX_CONCURRENT_REQUESTS = http_client.POOL_MAXSIZE

    # Whitelist for manifest-supplied asset ids (plugin/icon/wallpaper "id"
    # fields). These come from a REMOTE manifest.json and are used as single
    # path components under the app's data dirs -- including as rmtree and
    # install targets. Must start alphanumeric (rejects ".", "..", hidden
    # dirs) and may only continue with [A-Za-z0-9._-] (rejects "/", "\\",
    # whitespace, absolute paths). Length-capped to stay a sane dirname.
    ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @classmethod
    def is_safe_asset_id(cls, asset_id) -> TypeGuard[str]:
        """Whether a manifest-supplied id is safe to use as a single path
        component. Reject (don't normalize): an id that fails this check is
        a hostile or broken manifest, and quietly repairing it would install
        into / delete a path the author never named."""
        return isinstance(asset_id, str) and bool(cls.ASSET_ID_PATTERN.fullmatch(asset_id))

    # A git commit sha is exactly 40 lowercase hex chars. `commit_sha` reaches
    # git as an argv token (no shell), so this is a sanity gate, not a shell
    # guard -- but a malformed value should fail loudly rather than be handed
    # to `git reset --hard`.
    COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

    # A branch/ref name that came from a REMOTE store catalog (plugin["branch"]).
    # Even as an argv token it must never carry shell metacharacters, newlines,
    # NUL, or a leading "-" (which git would read as an option). Kept permissive
    # enough for real ref names (slashes, dots, dashes-in-the-middle) but no
    # whitespace or shell-significant characters.
    SAFE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

    # What is installed on disk, per asset directory, scanned once for the
    # duration of an update-check pass (process_store_data with
    # include_images False) instead of once per catalog entry. None outside
    # such a pass -- prepare_* then scans for itself, so nothing depends on
    # the snapshot existing, and a whole-dict assignment is atomic, so a
    # worker sees either this pass's snapshot or no snapshot at all.
    # Declared on the class so instances built without __init__ (the test
    # harness) read the same default.
    _installed_index: "dict[str, dict[str, InstalledAsset]] | None" = None

    # Install directories no catalog entry claimed this session, so the
    # legacy walk stops re-visiting them. Rebound rather than mutated, so
    # the class-level default is never shared state between instances.
    _unresolvable_installs: "frozenset[str]" = frozenset()

    @classmethod
    def is_safe_commit_sha(cls, commit_sha) -> TypeGuard[str]:
        return isinstance(commit_sha, str) and bool(cls.COMMIT_SHA_PATTERN.fullmatch(commit_sha))

    @classmethod
    def is_safe_ref_name(cls, ref_name) -> TypeGuard[str]:
        """Whether a remote-catalog branch/ref name is safe to pass to git.
        Rejects shell metachars, whitespace, newlines, and leading '-' so a
        catalog `branch: "main; rm -rf ~"` can neither inject a shell nor be
        misread by git as an option."""
        return isinstance(ref_name, str) and bool(cls.SAFE_REF_PATTERN.fullmatch(ref_name))

    def __init__(self):
        self.store_cache = StoreCache()

        # Shared by every fetch path: catalog prepare_* tasks on
        # _prepare_pool plus direct calls from UI worker threads (installs,
        # updates) -- caps process-wide store HTTP concurrency.
        self._fetch_limiter = threading.Semaphore(self.MAX_CONCURRENT_REQUESTS)

        # Fan-out pool for process_store_data's catalog prepare_* tasks.
        # Sized to the fetch cap -- more workers would only queue on
        # _fetch_limiter. Nothing running ON the pool ever submits to it
        # (prepare_* doesn't re-enter), so it cannot starve itself.
        self._prepare_pool = ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT_REQUESTS, thread_name_prefix="store-prepare")

        self.official_store_branch_cache: str = None

        # Set default fallback official authors
        self.official_authors = ["Core447", "StreamController"]
        
        # Start fetching the real official authors in a background thread
        threading.Thread(target=self._fetch_official_authors_background, daemon=True).start()

    def _fetch_official_authors_background(self):
        """Fetches official authors in a background thread and updates self.official_authors."""
        try:
            authors = self.get_official_authors()
            if not isinstance(authors, NoConnectionError):
                self.official_authors = authors
                log.info(f"Official authors updated: {authors}")
        except Exception as e:
            log.warning(f"Failed to fetch official authors, using fallback: {e}")

    def get_stores(self) -> list[tuple[str, str]]:
        settings = gl.settings_manager.app()

        stores = []
        branch = self.get_official_store_branch()
        if not isinstance(branch, str) or not branch:
            # get_official_store_branch guarantees a str; keep the invariant
            # enforced at this boundary anyway -- a non-str branch would end
            # up interpolated into build_url URLs and cache keys.
            log.error(f"Official store branch resolved to {branch!r}; using {self.STORE_BRANCH}")
            branch = self.STORE_BRANCH
        log.info(f"Official store branch: {branch}")
        stores.append((self.STORE_REPO_URL, branch))

        if settings.enable_custom_stores:
            for store in settings.custom_stores:
                url = store.get("url")
                if not url:
                    continue
                if parse_repo_url(url) is None:
                    # A store whose url cannot be parsed used to raise an
                    # opaque "x not in list" out of the cache-key helper,
                    # through fetch_and_parse_store_json (which only catches
                    # json errors) and out of the whole catalog load -- one
                    # bad settings entry blanked every store page.
                    log.error(f"Skipping custom store {url!r}: not a store repository url")
                    continue
                custom_branch = store.get("branch")
                if not isinstance(custom_branch, str) or not custom_branch:
                    # Third-party stores follow the "main" convention; this
                    # deliberately differs from the OFFICIAL store's fallback
                    # (STORE_BRANCH, currently "1.5.0" -- a version-pinned tag
                    # of THIS app's own store repo, which custom repos don't
                    # share). Not a copy-paste slip.
                    custom_branch = "main"
                stores.append((url, custom_branch))

        return stores
    
    def get_custom_plugins(self) -> list[tuple[str, str]]:
        settings = gl.settings_manager.app()

        plugins = []
        if settings.enable_custom_plugins:
            for plugin in settings.custom_plugins:
                url = plugin.get("url")
                if not url:
                    # An empty row: added in the settings window and never
                    # filled in. Silent, like get_stores -- it is not an error.
                    continue
                if parse_repo_url(url) is None:
                    log.error(f"Skipping custom plugin {url!r}: not a store repository url")
                    continue
                plugins.append((url, plugin.get("branch")))

        return plugins
    
    def get_official_store_branch(self) -> str:
        """Always returns a str branch name. On any failure (fetch failed
        AND cache too stale, truncated/corrupt versions.json) it falls back
        to STORE_BRANCH -- returning an error object here used to leak into
        get_stores' (url, branch) tuples and get interpolated into URLs and
        cache keys by build_url. The fallback is deliberately NOT cached in
        official_store_branch_cache, so a later successful fetch corrects it.
        """
        if self.official_store_branch_cache is not None:
            return self.official_store_branch_cache
        versions_file = self.get_remote_file(self.STORE_REPO_URL, "versions.json", branch_name="versions", force_refetch=True)
        if isinstance(versions_file, NoConnectionError) or versions_file is None:
            log.warning(f"Could not fetch versions.json; falling back to store branch {self.STORE_BRANCH}")
            return self.STORE_BRANCH
        try:
            versions = json.loads(versions_file)
        except (json.decoder.JSONDecodeError, TypeError) as e:
            # A truncated cached versions.json (served by the stale-cache
            # fallback) used to raise out of here, freeze the store tab's
            # spinner and mark the page loaded-forever.
            log.error(f"Corrupt versions.json; falling back to store branch {self.STORE_BRANCH}: {e}")
            return self.STORE_BRANCH
        if not isinstance(versions, dict):
            log.error(f"versions.json is not an object; falling back to store branch {self.STORE_BRANCH}")
            return self.STORE_BRANCH
        v = versions.get(gl.app_version, "main")
        if not isinstance(v, str) or not v:
            log.error(f"versions.json maps {gl.app_version} to {v!r}; falling back to store branch {self.STORE_BRANCH}")
            return self.STORE_BRANCH
        self.official_store_branch_cache = v
        return v

    def request_from_url(self, url: str) -> "requests.Response | NoConnectionError":
        # Callers run on worker threads (the prepare pool, UI install
        # threads). Connection AND body read stay inside the limiter, which
        # is what keeps a catalog load from presenting as a scrape burst --
        # the shared session's 429/5xx retries happen inside the adapter, so
        # they hold the same slot and cannot widen that burst either.
        try:
            with self._fetch_limiter:
                req = http_client.get(url, stream=True, timeout=30)
                try:
                    if req.status_code == 200:
                        req.content  # read the body while the connection is open
                        return req
                    log.error(f"Request to {url} failed with status code {req.status_code}")
                    # Read the error body too, even though it is discarded:
                    # closing a streamed response whose body was never
                    # consumed CLOSES the socket instead of handing it back
                    # to the shared session's pool. A catalog is full of
                    # legitimate 404s (attribution.json is optional, so most
                    # entries miss it), and every one of them would otherwise
                    # cost the next fetch a fresh TCP + TLS handshake --
                    # exactly what the pooled session exists to avoid.
                    # GitHub's error bodies are a few bytes; a 200 body from
                    # the same host is already read unbounded above.
                    req.content
                    return NoConnectionError()
                finally:
                    req.close()  # content stays cached on the Response
        except requests.exceptions.RequestException as e:
            log.error(e)
            return NoConnectionError()
    
    def build_url(self, repo_url: str, file_path: str, branch_name: str = "main") -> str:
        """
        Replaces the domain in the given repository URL with "raw.githubusercontent.com" and constructs the URL for the specified file path in the repository's branch.

        Parameters:
            repo_url (str): The URL of the repository.
            file_path (str): The path of the file in the repository.
            branch_name (str, optional): The name of the branch or commit sha in the repository. Defaults to "main".

        Returns:
            str: The constructed URL for the specified file path in the repository's branch.
        """
        repo_url = repo_url.replace("github.com", "raw.githubusercontent.com")
        return f"{repo_url}/{branch_name}/{file_path}"

    def get_remote_file(self, repo_url: str, file_path: str, branch_name: str = "main", data_type: str = "text", force_refetch: bool = False):
        """
        This function retrieves the content of a remote file from a GitHub repository.

        Parameters:
            repo_url (str): The URL of the GitHub repository.
            file_path (str): The path to the file within the repository.
            branch_name (str, optional): The name of the branch to retrieve the file from. Defaults to "main".
                                         Alternatively, you can specify a specific commit hash.

        Returns:
            str: The content of the remote file.

        Note:
            - The function uses an LRU cache to improve performance by caching previously retrieved files.
            - If the file is located in a different domain than github.com, the function will replace the domain
              with raw.githubusercontent.com.
        """
        byte_suffix = ""
        if data_type == "content":
            byte_suffix = "b"

        # data_type is part of the cache key: without it a binary fetch
        # (data_type="content") of some repo/path landed under the same
        # index entry as a text fetch of that path -- one cache file opened
        # with conflicting modes depending on who asked first.
        is_cached = False
        if not force_refetch:
            is_cached = self.store_cache.is_cached(
                url=repo_url,
                branch=branch_name,
                path=file_path,
                data_type=data_type
            )
        if is_cached:
            with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=f"r{byte_suffix}") as f:
                return f.read()
        else:
            pass

        url = self.build_url(repo_url, file_path, branch_name)

        answer = self.request_from_url(url)

        if isinstance(answer, NoConnectionError):
            # Fetch failed (offline, or raw.githubusercontent rate-limiting
            # us with 429s): fall back to the cached copy, even when the
            # caller forced a refetch -- a slightly stale catalog beats an
            # empty/errored store page. Bounded by the entry's FETCHED age;
            # its "date" field is a last-use clock that every read renews,
            # so it cannot bound staleness (see StoreCache).
            if self.store_cache.is_cached(url=repo_url, branch=branch_name, path=file_path, data_type=data_type):
                fetched = self.store_cache.get_fetched_date(url=repo_url, branch=branch_name, path=file_path, data_type=data_type)
                if fetched is not None and time.time() - fetched <= StoreCache.DAYS_TO_KEEP * 24 * 60 * 60:
                    log.warning(f"Serving cached copy of {file_path} from {repo_url} after failed fetch")
                    with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=f"r{byte_suffix}") as f:
                        return f.read()
            return answer

        if answer is None:
            return

        with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=f"w{byte_suffix}") as f:
            if answer is None:
                return
            if data_type == "text":
                f.write(answer.text)
            elif data_type == "content":
                f.write(answer.content)

        if data_type == "text":
            return answer.text
        elif data_type == "content":
            return answer.content
        
    def get_last_commit(self, repo_url: str, branch_name: str = "main"):
        """Resolves the tip sha of a branch via the GitHub API.

        Runs under the fetch semaphore like every sibling fetch --
        prepare_plugin calls this for every branch-pinned custom plugin
        during the catalog fan-out, and an uncapped requests.get() here
        would evade the limiter.

        Returns the sha str, None when there is no sha to resolve (a url
        that names no repository, a non-200 answer, an empty/unparseable
        commit list), or NoConnectionError on a network failure -- the same
        contract as request_from_url, so callers isinstance-check instead of
        catching requests exceptions out of a gather.
        """
        ref = parse_repo_url(repo_url)
        if ref is None:
            log.error(f"Cannot resolve a commit of {repo_url!r}: not a store repository url")
            return None

        url = f"https://api.github.com/repos/{ref.user}/{ref.repo}/commits?sha={branch_name}&per_page=1"

        try:
            with self._fetch_limiter:
                response = http_client.get(url, timeout=30)
        except requests.exceptions.RequestException as e:
            log.error(f"Failed to fetch the last commit of {repo_url}@{branch_name}: {e}")
            return NoConnectionError()

        if response.status_code != 200:
            return None

        try:
            commits = response.json()
        except ValueError as e:
            log.error(f"Unparseable commits answer for {repo_url}@{branch_name}: {e}")
            return None
        if not isinstance(commits, list) or len(commits) == 0:
            return None
        return commits[0].get("sha")
    
    def get_official_authors(self) -> "list | NoConnectionError":
        authors_json = self.get_remote_file(self.STORE_REPO_URL, "OfficialAuthors.json", self.STORE_BRANCH, force_refetch=True)
        if isinstance(authors_json, NoConnectionError):
            return authors_json
        authors_json = json.loads(authors_json)
        return authors_json
    
    def fetch_and_parse_store_json(self, url: str, filename: str, branch: str, n_stores_with_errors: int = 0):
        try:
            store_file_json = self.get_remote_file(url, filename, branch, force_refetch=True)
            if isinstance(store_file_json, NoConnectionError):
                n_stores_with_errors += 1
                return None, n_stores_with_errors
            store_file_json = json.loads(store_file_json)
            return store_file_json, n_stores_with_errors
        except (json.decoder.JSONDecodeError, TypeError) as e:
            n_stores_with_errors += 1
            log.error(e)
            return None, n_stores_with_errors

    def process_store_data(self, filename: str, process_func: Callable[..., Any], get_custom_func: Callable[..., Any] | None, data_class, include_images=True, base_dir: str | None = None):
        """Fetches the catalog file from every configured store and prepares
        each entry on the fan-out pool.

        include_images picks WHICH VIEW of an entry is built, not just
        whether a thumbnail is attached:

          * True -- the store window's view: every entry is described in
            full (manifest, attribution, thumbnail), because every entry is
            about to be shown.
          * False -- the update check's view: only what decides "is this
            installed, and is it out of date". Entries that are not
            installed cost no request of their own, and no image is fetched
            or decoded at all. The objects it returns are NOT complete store
            entries -- their display fields are unset.

        base_dir names the asset directory this catalog installs into. The
        update-check view needs it to identify installs made before the
        origin stamp existed; the store window's view does not use it.
        """
        n_stores_with_errors = 0
        data_list = []

        if not include_images:
            # Opens an update-check pass: the fan-out below then scans each
            # asset directory once for the whole pass instead of once per
            # catalog entry.
            self._installed_index = {}
        try:
            stores = self.get_stores()

            for url, branch in stores:
                store_file_json, n_stores_with_errors = self.fetch_and_parse_store_json(url, filename, branch, n_stores_with_errors)
                if store_file_json is not None:
                    data_list.extend(store_file_json)

            if n_stores_with_errors >= len(stores):
                return NoConnectionError()

            custom_entries = [{"url": url, "branch": branch}
                              for url, branch in (get_custom_func() if get_custom_func is not None else [])]

            if not include_images and base_dir is not None:
                # Before the fan-out, so every entry below decides locally.
                self.resolve_unstamped_installs(base_dir, data_list + custom_entries)

            futures = [self._prepare_pool.submit(process_func, data, include_images, True) for data in data_list]
            futures += [self._prepare_pool.submit(process_func, asset, include_images, False)
                        for asset in custom_entries]

            # Collect per-future: one misbehaving store entry must not raise out
            # of the fan-out and blank the whole page (the page's @log.catch
            # load() would swallow it and leave the spinner up forever).
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as e:
                    log.error(f"Store item preparation failed: {e!r}")
            results = [result for result in results if isinstance(result, data_class)]

            return results
        finally:
            if not include_images:
                # Dropped with the pass that took it: a later prepare_* must
                # never decide against a snapshot from an earlier one.
                self._installed_index = None

    def get_all_plugins(self, include_images: bool = True) -> list[PluginData]:
        return self.process_store_data(self.PLUGIN_FILE, self.prepare_plugin, self.get_custom_plugins, PluginData, include_images, gl.PLUGIN_DIR)

    def get_all_icons(self, include_images: bool = True) -> list[IconData]:
        return self.process_store_data(self.ICON_FILE, self.prepare_icon, None, IconData, include_images, self.icons_dir())

    def get_all_wallpapers(self, include_images: bool = True) -> list[WallpaperData]:
        return self.process_store_data(self.WALLPAPERS_FILE, self.prepare_wallpaper, None, WallpaperData, include_images, self.wallpapers_dir())

    def get_all_sd_plus_bar_wallpapers(self, include_images: bool = True) -> list[SDPlusBarWallpaperData]:
        return self.process_store_data(self.SDPLUSWALLPAPERS_FILE, self.prepare_sd_plus_bar_wallpaper, None, SDPlusBarWallpaperData, include_images, self.sd_plus_bar_wallpapers_dir())
    
    def get_manifest(self, url:str, commit:str) -> "dict | NoConnectionError | None":
        # url = self.build_url(url, "manifest.json", commit)
        manifest = self.get_remote_file(url, "manifest.json", commit)
        if isinstance(manifest, NoConnectionError):
            return manifest
        if manifest is None:
            return None
        return json.loads(manifest)

    def get_attribution(self, url:str, commit:str) -> dict:
        result = self.get_remote_file(url, "attribution.json", commit)
        if isinstance(result, NoConnectionError):
            return {}
        
        try:
            return json.loads(result)
        except (json.decoder.JSONDecodeError, TypeError) as e:
            return {}

    def _resolve_asset_version(self, entry: dict, desc: AssetTypeDescriptor, url: str):
        """Decide the commit an entry should be fetched at.

        Non-plugin entries always pin a version map; a plugin entry may omit
        it (a branch-pinned custom plugin), and only a plugin entry resolves a
        branch tip. Returns a _ResolvedVersion, or one of the two early-out
        values its callers already understand: the NoCompatibleVersion class
        when no version resolves at all, or a NoConnectionError when a branch
        tip cannot be reached.
        """
        compatible = True
        commit: str | None = None
        if not desc.is_plugin or "commits" in entry:
            newest = self.get_newest_compatible_version(entry["commits"])
            if newest is None:
                compatible = False
                newest = self.get_newest_version(list(entry["commits"].keys()))
                if newest is None:
                    return NoCompatibleVersion
            commit = entry["commits"][newest]

        branch: str | None = None
        if desc.is_plugin:
            branch = entry.get("branch")
            if branch is not None:
                commit = self.get_last_commit(url, branch)
                if isinstance(commit, NoConnectionError):
                    # NoConnectionError is falsy: letting it fall through would
                    # fetch the manifest at `branch` but store the error object
                    # as the entry's commit_sha (poisoning sha comparisons).
                    log.error(f"Could not resolve the last commit of {url}@{branch} due to NoConnectionError")
                    return commit

        return _ResolvedVersion(compatible, commit, branch)

    def _fetch_thumbnail(self, url: str, thumbnail_path: Any, ref: Any) -> "Image.Image | None":
        fetched = self.get_web_image(url, thumbnail_path, ref)
        # A missing/rate-limited thumbnail must not drop the asset from the
        # catalog -- list it without an image.
        return None if isinstance(fetched, NoConnectionError) else fetched

    def _translate_descriptions(self, manifest: dict) -> "tuple[Any, Any]":
        return (
            gl.lm.get_custom_translation(manifest.get("descriptions", {})),
            gl.lm.get_custom_translation(manifest.get("short-descriptions", {})),
        )

    def _prepare_asset(self, entry, desc: AssetTypeDescriptor, include_image: bool = True, verified: bool = False):
        """Turn one catalog entry into the descriptor's dataclass -- the single
        implementation of what prepare_plugin/icon/wallpaper/sd_plus each used
        to do line for line.

        include_image picks the view: False builds only what the update check
        reads (see check_entry_for_update) and fetches nothing to display; True
        builds the full store-window row (manifest, then thumbnail, then
        attribution). Everything type-specific -- the install dir, the
        dataclass, its id/name/version field names, whether a branch applies --
        is read off `desc`.
        """
        base_dir = getattr(self, desc.base_dir_attr)()
        if not include_image:
            # The update-check view: only the fields get_*_to_update reads and
            # install_* needs. Every field left unset here (name, descriptions,
            # tags, licence, thumbnail) would cost a request and is only ever
            # displayed.
            checked = self.check_entry_for_update(entry, base_dir)
            if not isinstance(checked, UpdateCheck):
                return checked
            fields: dict[str, Any] = {
                "github": checked.url,
                "author": checked.ref.user,
                "repository_name": checked.ref.repo,
                "commit_sha": checked.commit_sha,
                "local_sha": checked.local_sha,
                desc.id_field: checked.asset_id,
                "is_compatible": checked.compatible,
                "verified": verified,
            }
            if desc.is_plugin:
                fields["branch"] = checked.branch
            return desc.data_cls(**fields)

        if "url" not in entry:
            # A uniform diagnostic for a url-less entry of any type: the three
            # non-plugin types already dropped one here silently, and the
            # plugin path reached this point as an opaque KeyError instead.
            log.error(f"Skipping store entry without a url: {entry!r}")
            return None
        url = entry["url"]
        ref = self.repo_ref_for_entry(url)
        if ref is None:
            return None

        resolved = self._resolve_asset_version(entry, desc, url)
        if not isinstance(resolved, _ResolvedVersion):
            # NoCompatibleVersion (dropped by process_store_data's isinstance
            # filter) or a NoConnectionError from an unreachable branch tip.
            return resolved
        compatible, commit, branch = resolved
        # Any because a plugin entry with neither a version map nor a branch
        # leaves this None, yet get_manifest/get_attribution/get_web_image all
        # declare `commit: str`; they genuinely receive None here and cope. The
        # fetch layer's honest retype is a later change's job.
        ref_for_fetch: Any = commit or branch

        manifest = self.get_manifest(url, ref_for_fetch)
        if isinstance(manifest, NoConnectionError):
            return manifest
        if not manifest:
            log.error(f"manifest failed to load for repository {url}")
            return None

        thumbnail_path: Any = manifest.get("thumbnail")
        image = self._fetch_thumbnail(url, thumbnail_path, ref_for_fetch)
        attribution = self.get_attribution(url, ref_for_fetch).get("generic", {})  # TODO: Choose correct attribution

        translated_description, translated_short_description = self._translate_descriptions(manifest)

        author = ref.user

        # JSON-derived values: the manifest/attribution documents are untyped,
        # and each "missing -> None" below feeds a StoreData field that is
        # declared non-Optional in src/windows/Store/StoreData.py. Bound through
        # Any locals rather than restating a type they do not have.
        descriptions: Any = manifest.get("descriptions") or None
        short_descriptions: Any = manifest.get("short-descriptions") or None
        tags: Any = manifest.get("tags") or None
        license_descriptions: Any = attribution.get("licence-descriptions", attribution.get("descriptions")) or None

        # This entry was identified the expensive way -- its remote manifest --
        # so record the link while it is known: the update check then
        # identifies the same install without any fetch.
        self.note_installed_origin(base_dir, manifest.get("id"), url)

        fields = {
            "descriptions": descriptions,
            "short_descriptions": short_descriptions,
            "description": translated_description or manifest.get("description"),
            "short_description": translated_short_description or manifest.get("short-description"),

            "github": url or None,
            "author": author or None,  # Formerly: user_name
            "official": author in self.official_authors or False,
            "commit_sha": commit,
            "local_sha": self.get_local_sha_for_id(base_dir, manifest.get("id")),
            "minimum_app_version": manifest.get("minimum-app-version") or None,
            "app_version": manifest.get("app-version") or None,
            "repository_name": ref.repo,
            "tags": tags,

            "thumbnail": thumbnail_path or None,
            # _fetch_thumbnail already collapses a failed fetch to None, so it
            # is the single guard here -- no redundant second `or None`.
            "image": image,

            "copyright": attribution.get("copyright") or None,
            "original_url": attribution.get("original-url") or None,
            "license": attribution.get("licence") or None,
            "license_descriptions": license_descriptions,

            desc.name_field: manifest.get("name") or None,
            desc.version_field: manifest.get("version") or None,
            desc.id_field: manifest.get("id") or None,

            "is_compatible": compatible,
            "verified": verified,
        }
        if desc.is_plugin:
            fields["branch"] = branch
        return desc.data_cls(**fields)

    def prepare_plugin(self, plugin, include_image: bool = True, verified: bool = False):
        return self._prepare_asset(plugin, PLUGIN, include_image, verified)

    def get_current_git_commit_hash_without_git(self, repo_path: str) -> str:
        try:
            # Construct the path to the FETCH_HEAD file
            fetch_head_path = os.path.join(repo_path, '.git', 'FETCH_HEAD')
            
            # Read the contents of the FETCH_HEAD file
            with open(fetch_head_path, 'r') as file:
                lines = file.readlines()
                
                # The first line contains the latest commit hash
                if lines:
                    latest_commit_hash = lines[0].split()[0]
                    return latest_commit_hash
                else:
                    raise ValueError("FETCH_HEAD file is empty")
                    
        except Exception as e:
            raise RuntimeError(f"Unable to retrieve git commit hash: {e}")
    
    def plugins_dir(self) -> str:
        return gl.PLUGIN_DIR

    def icons_dir(self) -> str:
        return os.path.join(gl.DATA_PATH, "icons")

    def wallpapers_dir(self) -> str:
        return os.path.join(gl.DATA_PATH, "wallpapers")

    def sd_plus_bar_wallpapers_dir(self) -> str:
        return os.path.join(gl.DATA_PATH, "sd_plus_bar_wallpapers")

    def scan_installed_assets(self, base_dir: str) -> dict[str, InstalledAsset]:
        """{install directory name: InstalledAsset} for everything under
        base_dir -- the whole local half of the update check, read without a
        single request.

        Every field is what identity or the verdict needs: the ORIGIN stamp
        says which repository the tree came from, the local manifest id says
        whether the directory is the canonical install of that tree (a
        renamed copy answers with the id it was copied from), the sha says
        which commit it sits on, and islink marks a checkout the user
        manages rather than an install this app owns.

        Unsafe names are skipped, which also skips the dot-prefixed swap
        leftovers _swap_into_place may have left behind.
        """
        index: dict[str, InstalledAsset] = {}
        try:
            names = os.listdir(base_dir)
        except OSError:
            # A store asset class the user has never installed from has no
            # directory at all; that is "nothing installed", not an error.
            return index
        for asset_id in names:
            if not self.is_safe_asset_id(asset_id):
                continue
            asset_path = os.path.join(base_dir, asset_id)
            if not os.path.isdir(asset_path):
                continue
            index[asset_id] = InstalledAsset(
                asset_id=asset_id,
                path=asset_path,
                sha=self.get_local_sha(asset_path) or "",
                origin=self.read_origin(asset_path),
                manifest_id=self.read_local_manifest_id(asset_path),
                is_symlink=os.path.islink(asset_path),
            )
        return index

    def installed_assets(self, base_dir: str) -> dict[str, InstalledAsset]:
        """The current pass's snapshot for base_dir, scanning once on first
        use; a fresh scan every time outside a pass (a prepare_* called on
        its own).

        Two workers reaching an unscanned directory together both scan and
        both store -- the results are equivalent snapshots taken moments
        apart, so the race costs one extra listing and nothing else. Two
        overlapping passes clearing each other's snapshot costs the same
        way: the snapshot is a cache, never the source of truth.
        """
        snapshot = self._installed_index
        if snapshot is None:
            return self.scan_installed_assets(base_dir)
        index = snapshot.get(base_dir)
        if index is None:
            index = self.scan_installed_assets(base_dir)
            snapshot[base_dir] = index
        return index

    def read_origin(self, asset_path: str) -> RepoRef | None:
        """The repository an installed tree was downloaded from, as stamped
        by download_repo/clone_repo. Reduced to a RepoRef so a url written
        in one spelling still matches a catalog entry written in another."""
        try:
            with open(os.path.join(asset_path, self.ORIGIN_FILE)) as f:
                return parse_repo_url(f.readline().strip())
        except OSError:
            return None

    @staticmethod
    def read_local_manifest_id(asset_path: str) -> str | None:
        """The id an installed tree claims for itself. Equal to the
        directory name for a canonical install -- that is the invariant
        install_* creates and _staged_tree_id_matches enforces -- and
        different for a renamed or copied-aside directory."""
        try:
            with open(os.path.join(asset_path, "manifest.json")) as f:
                asset_id = json.load(f).get("id")
        except (OSError, ValueError):
            return None
        return asset_id if isinstance(asset_id, str) else None

    def stamp_origin(self, asset_path: str, repo_url: str) -> None:
        """Record which repository an installed tree came from.

        This is the link the catalog cannot supply: a catalog entry names a
        repository, an install directory is named after a manifest id, and
        nothing on disk used to connect the two without fetching the remote
        manifest. Written for every install, and backfilled the first time
        an existing install is identified, so the update check can answer
        "is this entry installed" from local state alone.

        Never written into a symlinked directory: that is a checkout the
        user manages, and this app does not write into it.
        """
        if os.path.islink(asset_path):
            return
        try:
            with open(os.path.join(asset_path, self.ORIGIN_FILE), "w") as f:
                f.write(f"{repo_url}\n")
        except OSError as e:
            # Nothing breaks without the stamp: identity falls back to the
            # manifest lookup that wrote it in the first place.
            log.warning(f"Could not stamp the origin of {asset_path}: {e}")

    def note_installed_origin(self, base_dir: str, asset_id, repo_url: str) -> None:
        """Backfill the origin stamp of an install that was identified some
        other way -- a full prepare, which fetches the manifest anyway -- so
        the identification happens once rather than once per launch.

        A stamp that DISAGREES with the url that just identified the tree is
        overwritten: a repository that was renamed or transferred otherwise
        keeps a stamp no catalog entry claims, and the install silently
        stops being updated. Where a broken catalog lists one asset id under
        two urls, the last prepare to identify the tree wins; the sweep
        resolves the same collision in catalog order.
        """
        if not self.is_safe_asset_id(asset_id) or not isinstance(repo_url, str):
            return
        ref = parse_repo_url(repo_url)
        if ref is None:
            return
        asset_path = os.path.join(base_dir, asset_id)
        if not os.path.isdir(asset_path) or os.path.islink(asset_path):
            return
        if same_repository(self.read_origin(asset_path), ref):
            return
        self.stamp_origin(asset_path, repo_url)

    def match_installed_asset(self, ref: RepoRef, installed: dict) -> "InstalledAsset | None":
        """The install a catalog entry refers to, out of everything stamped
        with its repository.

        Only a CANONICAL directory can be the answer -- one whose name is
        the id its own manifest claims. A copy kept aside
        (com_x_Alpha_backup) carries the same origin stamp and would
        otherwise claim the entry, and installing over it is doomed by
        construction: download_repo refuses a staged tree whose manifest id
        is not the directory name, so it would download an archive on every
        launch only to throw it away. An install with no readable manifest
        is still eligible -- it is broken, not misnamed, and reinstalling it
        is the repair.
        """
        candidates = [asset for asset in installed.values() if same_repository(asset.origin, ref)]
        if not candidates:
            return None
        canonical = [asset for asset in candidates
                     if asset.manifest_id is None or asset.manifest_id == asset.asset_id]
        if len(canonical) == 1:
            return canonical[0]
        if not canonical:
            log.warning(
                f"Not updating {ref.user}/{ref.repo}: the only directories stamped with it "
                f"({[asset.asset_id for asset in candidates]}) are not named after the id "
                f"their manifest claims"
            )
            return None
        log.warning(
            f"Not updating {ref.user}/{ref.repo}: more than one install claims it "
            f"({[asset.asset_id for asset in canonical]})"
        )
        return None

    def resolve_unstamped_installs(self, base_dir: str, entries: list) -> None:
        """Identity for installs the origin stamp cannot answer for: fetch a
        candidate entry's manifest and match its id against the directory
        names, which is how identity worked before the stamp existed, then
        stamp what it identifies so it is never looked up again.

        A directory is pending when it carries no stamp (installed before
        the stamp existed) OR when its stamp names a repository no entry in
        this catalog claims -- a repository that was renamed or transferred
        otherwise keeps a stamp nothing matches, and the install silently
        stops being updated, which is the very failure the stamp exists to
        prevent.

        Runs once per update-check pass, before the fan-out, and only while
        something is actually pending. Entries whose repository name appears
        in a pending directory's id are tried first -- an asset id
        conventionally ends in its repository name -- and the walk stops as
        soon as every pending directory is claimed, so the usual cost is one
        fetch per unresolved install rather than one per catalog entry.
        Where a broken catalog lists one asset id under two urls, the first
        entry in catalog order wins (the sort is stable and a claimed
        directory is removed from pending).

        A directory nothing claims costs one full walk -- one small fetch
        per entry, no image work -- and is then remembered as unresolvable
        for the rest of the session, so the walk is not repeated on every
        later pass. That memory is deliberately not persisted: a store that
        was merely unreachable gets another chance at the next launch.

        Symlinked directories are never pending: they are never
        auto-updated, so there is nothing to identify them for, and this app
        does not write into a tree it does not own.
        """
        installed = self.installed_assets(base_dir)
        claimed = set()
        for entry in entries:
            entry_ref = parse_repo_url(entry.get("url"))
            if entry_ref is not None:
                claimed.add(repository_key(entry_ref))

        pending = {
            asset.asset_id: asset for asset in installed.values()
            if not asset.is_symlink
            and asset.path not in self._unresolvable_installs
            and (asset.origin is None or repository_key(asset.origin) not in claimed)
        }
        if not pending:
            return

        def plausible_first(entry) -> int:
            entry_ref = parse_repo_url(entry.get("url"))
            if entry_ref is None:
                return 2
            return 0 if any(entry_ref.repo.lower() in asset_id.lower() for asset_id in pending) else 1

        for entry in sorted(entries, key=plausible_first):
            if not pending:
                return
            try:
                self._claim_pending_install(entry, pending, installed)
            except Exception as e:
                # Same contract as the prepare fan-out's collect loop: one
                # misbehaving entry must not raise out of the pass. Remote
                # data reaches both a json parse (a truncated manifest, an
                # error page) and a version parse (a catalog key like
                # "latest"), and this pre-pass runs before every leg of
                # update_everything -- a raise here left ALL FOUR legs
                # silently doing nothing.
                log.error(f"Could not identify installs from store entry {entry.get('url')!r}: {e!r}")

        if pending:
            # Walked the whole catalog and nothing claimed these.
            self._unresolvable_installs = frozenset(
                self._unresolvable_installs | {asset.path for asset in pending.values()}
            )

    def _claim_pending_install(self, entry: dict, pending: dict, installed: dict) -> None:
        """One entry's turn at the pending directories: fetch its manifest
        and, if the id names a pending directory, stamp it. Raises whatever
        the remote data raises -- the caller owns the per-entry catch."""
        ref = parse_repo_url(entry.get("url"))
        if ref is None:
            return
        url = entry["url"]
        # The manifest is read at the revision the entry points at; for a
        # branch-pinned entry the branch NAME is revision enough, so
        # identifying it costs no tip lookup.
        revision = entry.get("branch")
        if revision is None:
            commits = entry.get("commits")
            if not isinstance(commits, dict) or not commits:
                return
            newest = self.get_newest_compatible_version(commits) or self.get_newest_version(list(commits.keys()))
            revision = commits[newest]
        manifest = self.get_manifest(url, revision)
        if isinstance(manifest, NoConnectionError) or not manifest:
            return
        asset_id = manifest.get("id")
        if not self.is_safe_asset_id(asset_id):
            return
        asset = pending.pop(asset_id, None)
        if asset is None:
            return
        if asset.manifest_id is not None and asset.manifest_id != asset.asset_id:
            # A directory whose name is not the id it claims cannot be
            # installed over anyway -- the staged-id check refuses it.
            return
        self.stamp_origin(asset.path, url)
        # Keep the pass snapshot honest: the entries checked after this one
        # must see the directory as stamped.
        installed[asset_id] = asset._replace(origin=ref)

    def check_entry_for_update(self, entry: dict, base_dir: str) -> "UpdateCheck | NoConnectionError | None":
        """Resolves one catalog entry against what is installed under
        base_dir, fetching nothing the update decision does not need.

        Identity comes from the ORIGIN stamp every install carries: the
        entry names a repository, the stamp says which directory came from
        that repository, and the directory's VERSION says which commit it
        sits on. All local, so an entry the user never installed costs
        nothing beyond the catalog that named it -- its manifest,
        attribution and thumbnail only ever mattered for displaying it.

        Identity deliberately does NOT come from matching the catalog's
        commit shas against what is installed: the store rewrites an entry's
        sha in place under the same version key, so the sha an install sits
        on stops being listed at exactly the moment an update exists.

        What still costs a request: a branch-pinned entry (custom plugins
        name a branch, not a version map) must resolve its tip.

        This answers from stamps ALONE, so it is only complete inside a pass
        that ran resolve_unstamped_installs first (process_store_data does,
        for the update-check view). Called directly, an install whose stamp
        is missing or stale reads as not installed.
        """
        ref = self.repo_ref_for_entry(entry.get("url"))
        if ref is None:
            return None
        url = entry["url"]
        installed = self.installed_assets(base_dir)

        branch = entry.get("branch")
        compatible = True
        if branch is not None:
            commit = self.get_last_commit(url, branch)
            if isinstance(commit, NoConnectionError):
                # NoConnectionError is falsy: letting it fall through would
                # store the error object as the entry's commit_sha and
                # poison the sha comparison.
                log.error(f"Could not resolve the last commit of {url}@{branch} due to NoConnectionError")
                return commit
            target = commit
        else:
            commits = entry.get("commits")
            if not isinstance(commits, dict) or not commits:
                log.error(f"Skipping store entry {url!r}: it pins no version")
                return None
            newest = self.get_newest_compatible_version(commits)
            if newest is None:
                # No version for this app major: pin the newest one anyway,
                # the way prepare_* does, and let the caller refuse to
                # install it.
                compatible = False
                newest = self.get_newest_version(list(commits.keys()))
            target = commits[newest]

        asset = self.match_installed_asset(ref, installed)
        if asset is None:
            return UpdateCheck(url, ref, None, None, target, branch, compatible)

        if asset.is_symlink:
            # A symlinked install is a checkout the user manages -- a dev
            # workflow points one at a working tree. Installing over it
            # replaces the link with a downloaded copy and takes the working
            # tree out of the plugin directory, so auto-update leaves it be;
            # the store window still offers the update explicitly.
            log.info(f"Skipping auto-update of {asset.asset_id}: it is a symlink to a tree this app does not own")
            return UpdateCheck(url, ref, None, None, target, branch, compatible)

        # An empty sha means neither .git nor VERSION could be read -- a
        # half-written or hand-copied tree. It compares unequal to every
        # commit, so the entry reads as outdated and the install is
        # repaired by reinstalling it.
        return UpdateCheck(url, ref, asset.asset_id, asset.sha, target, branch, compatible)

    def get_local_sha_for_id(self, base_dir: str, asset_id) -> str | None:
        """get_local_sha guarded by the asset-id whitelist: an unsafe or
        missing manifest id never probes the filesystem and simply reads as
        'not installed' (None)."""
        if not self.is_safe_asset_id(asset_id):
            return None
        return self.get_local_sha(os.path.join(base_dir, asset_id))

    def get_local_sha(self, git_dir: str):
        if not os.path.exists(git_dir):
            return
        
        if os.path.exists(os.path.join(git_dir, ".git")):
            try:
                sha = self.get_current_git_commit_hash_without_git(git_dir)
                if sha is not None:
                    return sha
            except (ValueError, RuntimeError) as e:
                log.error(e)

        version_file_path = os.path.join(git_dir, "VERSION")
        if not os.path.exists(version_file_path):
            return ""
        
        with open(version_file_path, "r") as f:
            return f.read().strip()
    
    def prepare_icon(self, icon, include_image: bool = True, verified: bool = False):
        return self._prepare_asset(icon, ICON, include_image, verified)

    def prepare_wallpaper(self, wallpaper, include_image: bool = True, verified: bool = False):
        return self._prepare_asset(wallpaper, WALLPAPER, include_image, verified)

    def prepare_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper, include_image: bool = True, verified: bool = False):
        return self._prepare_asset(sd_plus_bar_wallpaper, SD_PLUS_BAR, include_image, verified)

    def get_web_image(self, url: str, path: str, branch: str = "main") -> "Image.Image | NoConnectionError | None":
        # `except Exception` so a pool worker still honours SystemExit and
        # KeyboardInterrupt.
        try:
            result = self.get_remote_file(url, path, branch, data_type="content")
        except Exception as e:
            log.error(f"Failed to fetch image {path} from {url}: {e}")
            return None
        if isinstance(result, NoConnectionError):
            return result
        try:
            return Image.open(BytesIO(result))
        except Exception as e:
            log.warning(f"Could not decode image {path} from {url}: {e}")
            return None
    
    def get_user_name(self, repo_url:str) -> str:
        ref = parse_repo_url(repo_url)
        if ref is None:
            raise ValueError(f"Not a store repository url: {repo_url!r}")
        return ref.user

    def get_repo_name(self, repo_url:str) -> str | None:
        ref = parse_repo_url(repo_url)
        return None if ref is None else ref.repo

    def repo_ref_for_entry(self, url: object) -> RepoRef | None:
        """Parses a catalog/settings entry's url, reporting the skip.

        Store entries carry whatever the catalog json or the user's settings
        hold, so an entry that names no usable repository is dropped here
        rather than raising through the prepare fan-out as an opaque
        "x not in list".
        """
        ref = parse_repo_url(url)
        if ref is None:
            log.error(f"Skipping store entry {url!r}: not a store repository url")
        return ref

    def get_newest_compatible_version(self, available_versions: Collection[str]) -> str | None:
        if gl.exact_app_version_check:
            if gl.app_version in available_versions:
                return gl.app_version
            else:
                return None
            
        current_major = version.parse(gl.app_version).major

        compatible_versions = [v for v in available_versions if version.parse(v).major == current_major]
        parsed_compatible_versions = [version.parse(v) for v in compatible_versions]

        if compatible_versions:
            max_index = parsed_compatible_versions.index(max(parsed_compatible_versions))
            return compatible_versions[max_index]
        else:
            return None
        
    def get_newest_version(self, available_versions: list[str]) -> str:
        parsed_versions = [version.parse(v) for v in available_versions]
        
        max_index = parsed_versions.index(max(parsed_versions))
        return available_versions[max_index]

    ## Install
    def subp_call(self, args):
        return subprocess.call(args)

    def get_main_folder_of_zip(self, zip_path: str) -> str | int:
        extracted_folder_name = None
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_contents = zip_ref.namelist()
            for item in zip_contents:
                if not item.endswith("/"): # Directories end with /
                    continue
                if item.count("/") > 1:
                    continue
                
                if extracted_folder_name is not None:
                    log.error("Multiple folders in zip")
                    return 400
                extracted_folder_name = item.split("/")[0]


        if extracted_folder_name is None:
            log.error("Could not find extracted folder name")
            return 400

        return extracted_folder_name

    def zip_has_unsafe_members(self, zip_path: str) -> bool:
        """Defense-in-depth Zip-Slip check on a downloaded archive.

        We only ever download GitHub-generated .zip archives, and CPython's
        zipfile already strips leading "/" and ".." when extracting -- so this
        is belt-and-suspenders, not the primary guard. It exists so that a
        future change (a different archive source, or swapping in tar/other
        formats that CPython does NOT sanitize the same way) can't silently
        reintroduce a path-traversal write. Any member whose normalized path
        is absolute or escapes the extraction root fails the whole archive.
        """
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            for name in zip_ref.namelist():
                # Reject absolute paths and drive-style/backslash members.
                if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
                    log.error(f"Archive member has absolute path, refusing: {name!r}")
                    return True
                normalized = os.path.normpath(name.replace("\\", "/"))
                # normpath collapses "a/../b"; a leading ".." (or a bare "..")
                # means the member resolves outside the extraction root.
                if normalized == ".." or normalized.startswith(".." + os.sep) or normalized.startswith("../"):
                    log.error(f"Archive member escapes extraction root, refusing: {name!r}")
                    return True
        return False

    @staticmethod
    def _remove_leftover(path: str) -> None:
        """Remove a transient swap tree (or stray file/symlink) if present."""
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _staged_tree_id_matches(self, staging_tree: str, expected_id: str | None) -> bool:
        """Single choke point for the staged-manifest identity check: when
        the caller knows which asset id it is installing (the id also names
        the install dir), the downloaded tree's manifest must agree -- a
        catalog/repo drift or hostile manifest must never be swapped over
        the installed pack."""
        if expected_id is None:
            return True
        try:
            with open(os.path.join(staging_tree, "manifest.json")) as f:
                staged_id = json.load(f).get("id")
        except (OSError, ValueError) as e:
            log.error(f"Staged download has no readable manifest.json ({e}) -- refusing to install as {expected_id!r}")
            return False
        if staged_id != expected_id:
            log.error(f"Staged download identifies as {staged_id!r}, expected {expected_id!r} -- refusing to install")
            return False
        return True

    def _swap_into_place(self, staging_tree: str, directory: str) -> None:
        """Replace `directory` with the fully staged tree, deleting the old
        install only after the new one is in place.

        The staged tree is first moved next to the destination -- the only
        possibly cross-filesystem step (PLUGIN_DIR can be env-overridden
        onto another device), and it runs while the old install is still
        fully intact. The two renames that follow are same-parent and
        therefore atomic. The transient siblings are dot-prefixed so the
        plugin/pack directory scanners never pick a crash leftover up as a
        real install; leftovers are swept on the next install of the same
        asset."""
        parent = os.path.dirname(os.path.abspath(directory))
        name = os.path.basename(os.path.normpath(directory))
        os.makedirs(parent, exist_ok=True)
        new_tree = os.path.join(parent, f".{name}.deckard-new")
        old_tree = os.path.join(parent, f".{name}.deckard-old")
        self._remove_leftover(new_tree)
        self._remove_leftover(old_tree)

        shutil.move(staging_tree, new_tree)
        moved_old_aside = False
        try:
            if os.path.lexists(directory):
                os.replace(directory, old_tree)
                moved_old_aside = True
            os.replace(new_tree, directory)
        except Exception:
            # Put the old install back before surfacing the failure.
            if moved_old_aside and not os.path.lexists(directory):
                os.replace(old_tree, directory)
            shutil.rmtree(new_tree, ignore_errors=True)
            raise
        self._remove_leftover(old_tree)

    def download_repo(self, repo_url:str, directory:str, commit_sha:str = None, branch_name:str = None, expected_id:str = None):
        """Returns 200 on success, 404 for a hard failure (e.g. git missing
        on the devel clone path), 400 for a staged tree that fails the
        expected_id manifest check, or NoConnectionError.

        The install is transactional: the new tree is downloaded, extracted,
        validated and VERSION-stamped in a staging area first, then swapped
        into `directory` via _swap_into_place -- the previously installed
        tree is deleted only after the new one is in place, so a failure
        anywhere leaves the old install untouched."""
        if not is_flatpak() and gl.argparser.parse_args().devel:
            return self.clone_repo(repo_url, directory, commit_sha, branch_name, expected_id)


        ref = parse_repo_url(repo_url)
        if ref is None:
            log.error(f"Could not derive a repository from {repo_url!r}")
            return 404
        username = ref.user
        projectname = ref.repo.lower()
        sha = commit_sha
        if commit_sha is None and branch_name is not None:
            # Used to write the version
            sha = self.get_last_commit(repo_url, branch_name)
            if isinstance(sha, NoConnectionError):
                return sha
            if sha is None:
                # Fail up front rather than building a ".../None.zip" URL
                # that 404s later with a misleading log.
                log.error(f"Could not resolve branch {branch_name!r} of {repo_url}")
                return 404
        if sha is None:
            # Neither a commit sha nor a branch was given: there is nothing to
            # download (this used to build a ".../None.zip" url and stamp
            # VERSION with None).
            log.error(f"Refusing to download {repo_url}: no commit sha and no branch")
            return 404

        zip_url = f"https://github.com/{username}/{projectname}/archive/{sha}.zip"

        zip_path = os.path.join(gl.DATA_PATH, "cache", f"{projectname}-{sha}.zip")

        # Download. The helper creates the cache dir, raises on an HTTP error
        # status, and reaps a partial/zero-byte archive itself, so a failed
        # download can never leave something behind to poison the next run.
        try:
            http_client.download_to_file(zip_url, zip_path, timeout=30)
        except Exception as e:
            log.error(e)
            return NoConnectionError()
        
        ## Extract
        extracted_folder = None
        try:
            # Resolve the folder name from the zip listing (github urls aren't
            # case-sensitive, so it may not match projectname) BEFORE unpacking,
            # so the finally-cleanup also covers a mid-extraction failure.
            extracted_folder_name = self.get_main_folder_of_zip(zip_path)
            if not isinstance(extracted_folder_name, str):
                # 400 from the helper: no single root folder in the archive.
                raise ValueError("could not determine the archive's root folder")
            # Defense-in-depth: refuse a traversal/absolute member before we
            # let shutil.unpack_archive write anything to disk.
            if self.zip_has_unsafe_members(zip_path):
                log.error(f"Refusing to extract {projectname}: archive contains unsafe member paths")
                return NoConnectionError()
            extracted_folder = os.path.join(gl.DATA_PATH, "cache", extracted_folder_name)
            if os.path.exists(extracted_folder):
                shutil.rmtree(extracted_folder)
            shutil.unpack_archive(zip_path, os.path.join(gl.DATA_PATH, "cache"))

            # Validate and complete the STAGED tree before it can reach the
            # install location. VERSION must exist before the swap: a tree
            # without it reads as local_sha None ("not installed"), so a
            # crash after the swap but before a late VERSION write would
            # leave an install that is never retried.
            if not self._staged_tree_id_matches(extracted_folder, expected_id):
                return 400
            with open(os.path.join(extracted_folder, "VERSION"), "w") as f:
                f.write(sha)
            # Stamped in the staging tree like VERSION, so the swap
            # publishes the install and its origin together.
            self.stamp_origin(extracted_folder, repo_url)

            self._swap_into_place(extracted_folder, directory)
        except Exception as e:
            log.error(f"Failed to extract/install {projectname}: {e}")
            return NoConnectionError()
        finally:
            # Best-effort: never leave the archive or extracted temp folder behind,
            # and never let cleanup replace the try-block's outcome.
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except OSError:
                pass
            if extracted_folder is not None and os.path.isdir(extracted_folder):
                shutil.rmtree(extracted_folder, ignore_errors=True)

        return 200
    
    def clone_repo(self, repo_url:str, local_path:str, commit_sha:str = None, branch_name:str = None, expected_id:str = None):
        if commit_sha is not None:
            # Use the main branch for the initial clone
            branch_name = None

        # commit_sha and branch_name originate from the REMOTE store catalog
        # (plugin["commits"][version] / plugin["branch"]). They used to be
        # f-string-interpolated into os.system() -- a catalog branch of
        # "main; <cmd>" injected a shell. Validate them here and pass them to
        # git as argv tokens with no shell (git -C, see below), the same way
        # the install-script runners were de-shelled.
        if commit_sha is not None and not self.is_safe_commit_sha(commit_sha):
            log.error(f"Refusing to clone {repo_url}: malformed commit sha {commit_sha!r}")
            return 400
        if branch_name is not None and not self.is_safe_ref_name(branch_name):
            log.error(f"Refusing to clone {repo_url}: unsafe branch/ref name {branch_name!r}")
            return 400

        # Check if git is installed on the system - should be the case for most linux systems
        if shutil.which("git") is None:
            log.error("Git is not installed on this system. Please install it.")
            return 404

        # Same transactional contract as download_repo: clone and prepare in
        # a staging dir under cache/, then swap -- the old rmtree-first flow
        # destroyed the existing install before the clone could fail.
        staging = os.path.join(gl.DATA_PATH, "cache", f".clone-staging.{os.path.basename(os.path.normpath(local_path))}")
        os.makedirs(os.path.join(gl.DATA_PATH, "cache"), exist_ok=True)
        self._remove_leftover(staging)

        try:
            # Clone the repository at the newest stage on the default branch
            rc = self.subp_call(["git", "clone", repo_url, staging])
            if rc != 0 or not os.path.isdir(staging):
                log.error(f"git clone of {repo_url} failed with exit code {rc}")
                return 404

            # Add repository to the safe directory list to avoid dubious ownership warnings
            # -- both the final home and the staging clone, since the
            # pull/reset/checkout below now run in staging.
            # FIXME: Check if not already added
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(local_path)])
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(staging)])

            # Run git pull to create .git/FETCH_HEAD. This allows us to check for available updates.
            # `git -C <dir>` (argv, no shell) instead of the old
            # `os.system("cd '<dir>' && git pull")` which built a shell command line.
            self.subp_call(["git", "-C", staging, "pull"])

            # Set repository to the given commit_sha. The rc is checked for
            # the same reason as the checkout below: an unreachable
            # catalog sha (upstream force-push, GC'd commit) otherwise leaves
            # staging on the default-branch tip, which then passes the tree
            # validation, gets VERSION-stamped with the sha it is NOT, and
            # installs as a success -- a silently wrong tree.
            #
            # Fail hard rather than fall back to the default tip: that is
            # what every other git failure in this function does (clone rc,
            # checkout rc, missing git -> 404), and, decisively, it is
            # already what the NON-devel path does for this exact failure --
            # download_repo builds ".../<sha>.zip", which 404s on an
            # unreachable sha and returns NoConnectionError. A fallback here
            # would make the devel clone path the only place in the store
            # where an unreachable sha still installs something.
            if commit_sha is not None:
                rc = self.subp_call(["git", "-C", staging, "reset", "--hard", commit_sha])
                if rc != 0:
                    log.error(f"git reset --hard {commit_sha!r} failed with exit code {rc} for {repo_url} "
                              f"(commit unreachable?) -- refusing to install the default-branch tip")
                    return 404
            elif branch_name is not None:
                # checkout, not switch: custom plugins may pin a TAG (or any
                # detachable ref), which `git switch` refuses without
                # --detach. The rc must be checked -- ignoring it
                # shipped the default-branch tip stamped as the ref whenever
                # the (user-typed) ref didn't exist.
                rc = self.subp_call(["git", "-C", staging, "checkout", branch_name])
                if rc != 0:
                    log.error(f"git checkout {branch_name!r} failed with exit code {rc} for {repo_url}")
                    return 404

            # Same order as download_repo: validate the staged tree first,
            # then stamp VERSION, then swap.
            if not self._staged_tree_id_matches(staging, expected_id):
                return 400

            ## Write version
            version_stamp = commit_sha or branch_name
            if version_stamp is None:
                log.error(f"Refusing to stamp VERSION for {repo_url}: no commit sha and no branch")
                return 400
            with open(os.path.join(staging, "VERSION"), "w") as f:
                f.write(version_stamp)
            self.stamp_origin(staging, repo_url)

            self._swap_into_place(staging, local_path)
        except Exception as e:
            log.error(f"Failed to stage devel clone of {repo_url}: {e}")
            return NoConnectionError()
        finally:
            self._remove_leftover(staging)

        return 200

    def install_plugin(self, plugin_data:PluginData, auto_update: bool = False):
        url = plugin_data.github
        plugin_id = plugin_data.plugin_id

        if not self.is_safe_asset_id(plugin_id):
            # The id names the install dir (which download_repo swap-replaces)
            # -- a traversal id like "../../.." must never reach that join.
            log.error(f"Refusing to install plugin with unsafe id {plugin_id!r} from {url}")
            return 400

        if url is None:
            log.error(f"Refusing to install plugin {plugin_id!r}: no repository url")
            return 400

        local_path = os.path.join(gl.PLUGIN_DIR, plugin_id)

        response = self.download_repo(repo_url=url, directory=local_path, commit_sha=plugin_data.commit_sha, branch_name=plugin_data.branch, expected_id=plugin_id)

        # Bail before running install scripts or reloading plugins over a
        # missing or partial tree.
        if isinstance(response, NoConnectionError):
            return response
        if response != 200:
            return 404

        # UPDATE case: the new tree is already swapped in; deregister the
        # old version now (sys.modules purge included) so load_plugins
        # below imports the new code. Deregistering only AFTER a successful
        # download means a failed update leaves the old version on disk AND
        # registered -- the old deregister-first flow needed a recovery
        # reload to undo its own damage.
        plugin_manager = gl.plugin_manager
        if plugin_manager is not None and plugin_manager.get_plugin_by_id(plugin_id) is not None:
            try:
                self.uninstall_plugin(plugin_id, remove_from_pages=False, remove_files=False)
            except Exception as e:
                log.error(f"Deregistering the old version of {plugin_id} failed: {e}")

        # Run install script if present. Make sure to use python binary used to run this process to not break venv dependency installations.
        # List form without a shell: an f-string command both broke on spaces
        # in the data path and let crafted path components inject shell syntax.
        if os.path.isfile(os.path.join(local_path, "__install__.py")):
            subprocess.run([sys.executable, os.path.join(local_path, "__install__.py")], start_new_session=True)

        # Install requirements from requirements.txt
        if os.path.isfile(os.path.join(local_path, "requirements.txt")):
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", os.path.join(local_path, "requirements.txt")], start_new_session=True)

        # Update plugin manager
        if plugin_manager is not None:
            plugin_manager.load_plugins()
            plugin_manager.init_plugins()
            plugin_manager.generate_action_index()

        # A version-gated plugin "installs" fine (files on disk, True
        # returned, store button flips to installed) but lands in
        # disabled_plugins during the reload above -- and the only feedback
        # used to be the NEXT launch's disabled-plugins toast: in the install
        # session it silently never appeared, reading later as "my config
        # reset after restart" (the custom-repo case). Say so now.
        self.notify_if_installed_disabled(plugin_id)

        # Update ui
        if gl.app is not None and recursive_hasattr(gl, "app.main_win.sidebar.action_chooser"):
            GLib.idle_add(gl.app.main_win.sidebar.action_chooser.plugin_group.update)

        ## Update page
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            ## Checks required to prevent errors after auto-update
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    # Load action objects
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

        # Notify plugin actions
        gl.signal_manager.trigger_signal(Signals.PluginInstall, plugin_data.plugin_id)

        log.success(f"Plugin {plugin_id} installed successfully under: {local_path} with sha: {plugin_data.commit_sha}")
        return True

    @staticmethod
    def notify_if_installed_disabled(plugin_id: str) -> bool:
        """If the plugin that was just installed got version-disabled by the
        register() gate (in disabled_plugins, not in plugins), tell the user
        immediately instead of leaving the first feedback to the next
        launch's startup toast. Returns whether it notified."""
        if plugin_id in PluginBase.plugins:
            return False
        entry = PluginBase.disabled_plugins.get(plugin_id)
        if entry is None:
            return False

        reason = entry.get("reason")
        name = getattr(entry.get("object"), "plugin_name", None) or plugin_id
        if reason == "app-out-of-date":
            detail = "it requires a newer version of StreamController"
        elif reason == "plugin-out-of-date":
            detail = "it was built for an older version of StreamController"
        else:
            detail = "its version metadata is invalid"
        body = f"{name} was installed but is disabled: {detail}"
        log.warning(f"Install of {plugin_id}: {body}")

        gl.notify.info(body, title="Plugin disabled")
        return True

    def uninstall_plugin(self, plugin_id:str, remove_from_pages:bool = False, remove_files:bool = True) -> None:
        ## 1. Remove all action objects in every cached page of every
        ## controller -- not just each controller's currently active page.
        ## A page that was previously visited and is still sitting in the
        ## page cache would otherwise keep dead plugin action objects alive
        ## with no teardown. Snapshot via the _pages_lock accessor: iterating
        ## gl.page_manager.pages directly raced load_page /
        ## discard_controller / clear_old_cached_pages mutating the dict
        ## from other threads.
        for page in (gl.page_manager.all_cached_pages() if gl.page_manager is not None else []):
            page.remove_plugin_action_objects(plugin_id=plugin_id)
            if remove_from_pages:
                page.remove_plugin_actions_from_json(plugin_id=plugin_id)

        ## 2. Inform plugin base
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            return None
        plugins = plugin_manager.get_plugins()
        plugin = plugin_manager.get_plugin_by_id(plugin_id)
        if plugin is None:
            return None
        # Capture the actual import folder now, before on_uninstall()/rmtree
        # below can remove the directory or rewrite plugin.PATH through the
        # symlink-resolution branch -- the sys.modules purge below needs the
        # real "plugins.<folder>" prefix, which may differ from plugin_id
        # (the manifest id) when the folder was renamed (bug 7).
        plugin_folder = os.path.basename(os.path.normpath(plugin.PATH))
        if remove_files:
            plugin.on_uninstall()
            
            ## 3. Remove plugin folder
            if os.path.islink(plugin.PATH):
                symlink_target = os.readlink(plugin.PATH)
                log.warning(f"Plugin {plugin.plugin_name} is inside a Symlink!")
                plugin.PATH = symlink_target

            shutil.rmtree(plugin.PATH)

        ## 4. Delete plugin base object
        # plugin_obj = gl.plugin_manager.get_plugin_by_id(plugin_id)
        plugin_manager.remove_plugin_from_list(plugin)

        plugin_manager.generate_action_index()


        del plugin

        if gl.app is not None:
            GLib.idle_add(gl.app.main_win.sidebar.action_chooser.plugin_group.update)
            GLib.idle_add(gl.app.main_win.sidebar.page_selector.update)


        base_module = f"plugins.{plugin_folder}"
        for module in sys.modules.copy():
            if module.startswith(base_module):
                del sys.modules[module]

        # for controller in gl.deck_manager.deck_controller:
            # controller.active_page.update_inputs_with_actions_from_plugin(plugin_id)

        ## Update page
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            ## Checks required to prevent errors after auto-update
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    # Load action objects
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

    # The three data-only pack types (icon / wallpaper / SD+ bar wallpaper)
    # share one install/uninstall pair, selected by descriptor. Each joins a
    # manifest-supplied id under a data dir and rmtree/replaces the result, so
    # every one must reject unsafe ids (a traversal id would hand a data-only
    # pack filesystem-wide delete with no code execution involved). The plugin
    # (un)install pair stays bespoke above -- it runs pip, __install__.py, and
    # the plugin-manager deregister/reload wiring the packs have no equivalent
    # of.

    def _install_asset(self, data, desc: AssetTypeDescriptor):
        """Download one data-only asset into its per-type directory. Returns
        download_repo's result (200 success / 404 hard failure / 400 bad-tree /
        NoConnectionError), or 400 for an unsafe id or a missing url -- the same
        contract each per-type installer carried.

        No pre-delete (B-06): download_repo stages and validates the new tree
        and only swaps it over the installed one at the end, so a failed
        download leaves the installed pack untouched (the old uninstall-first
        flow permanently lost the pack when the download failed mid-update)."""
        asset_id = data.asset_id
        if not self.is_safe_asset_id(asset_id):
            log.error(f"Refusing to install {desc.display_name} with unsafe id {asset_id!r} from {data.github}")
            return 400

        github = data.github
        if github is None:
            log.error(f"Refusing to install {desc.display_name} {asset_id!r}: no repository url")
            return 400

        asset_path = os.path.join(getattr(self, desc.base_dir_attr)(), asset_id)
        return self.download_repo(repo_url=github, directory=asset_path, commit_sha=data.commit_sha, expected_id=asset_id)

    def _uninstall_asset(self, data, desc: AssetTypeDescriptor):
        """Delete one data-only asset's installed directory. Returns 400 for an
        unsafe id, otherwise None -- byte-identical to the per-type
        uninstallers it replaced."""
        asset_id = data.asset_id
        if not self.is_safe_asset_id(asset_id):
            log.error(f"Refusing to uninstall {desc.display_name} with unsafe id {asset_id!r}")
            return 400
        asset_path = os.path.join(getattr(self, desc.base_dir_attr)(), asset_id)
        if os.path.exists(asset_path):
            shutil.rmtree(asset_path)

    def install_icon(self, icon_data:IconData):
        return self._install_asset(icon_data, ICON)

    def uninstall_icon(self, icon_data:IconData):
        return self._uninstall_asset(icon_data, ICON)

    def install_wallpaper(self, wallpaper_data:WallpaperData):
        return self._install_asset(wallpaper_data, WALLPAPER)

    def uninstall_wallpaper(self, wallpaper_data:WallpaperData):
        return self._uninstall_asset(wallpaper_data, WALLPAPER)

    def install_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData):
        return self._install_asset(sd_plus_bar_wallpaper_data, SD_PLUS_BAR)

    def uninstall_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData):
        return self._uninstall_asset(sd_plus_bar_wallpaper_data, SD_PLUS_BAR)

    def get_plugin_for_id(self, plugin_id):
        plugins = self.get_all_plugins()
        for plugin in plugins:
            if plugin.plugin_id == plugin_id:
                return plugin
            
    ## Updates
    def _get_assets_to_update(self, desc: AssetTypeDescriptor):
        """The installed assets of one class that have a newer, compatible,
        known-target version -- the shared update-check decision. The
        update-check view fetches no thumbnails and makes no request for a
        catalog entry that was never installed. Dispatches through the public
        ``get_all_*`` name so a test stub of it is honoured."""
        assets = getattr(self, desc.get_all_attr)(include_images=False)
        if isinstance(assets, NoConnectionError):
            return assets

        to_update: list = []
        for asset in assets:
            if asset.local_sha is None:
                # Not installed.
                continue
            if asset.local_sha == asset.commit_sha:
                # Already up to date.
                continue
            if asset.commit_sha is None:
                # No known target: a branch-pinned plugin whose tip did not
                # resolve, or -- any type -- an entry with a "branch" key or null
                # version map (check_entry_for_update reads a branch for every
                # type). Non-observable: a None target can never install, so the
                # skip only avoids a doomed download; count and disk are unchanged.
                continue
            if asset.is_compatible is False:
                # prepare pins the newest INCOMPATIBLE commit when no compatible
                # version exists (so the store can still list the entry).
                # Auto-updating onto it would replace a working asset with a
                # build for a different app major -- skip and report instead.
                # The store UI's update button reads the same verdict.
                log.warning(
                    f"Skipping update of {desc.display_name} {asset.asset_id}: pinned version "
                    f"{asset.commit_sha} is not compatible with app version {gl.app_version}"
                )
                continue
            to_update.append(asset)

        return to_update

    def _update_all(self, desc: AssetTypeDescriptor) -> "int | NoConnectionError":
        """Reinstall every out-of-date asset of one class; return how many
        reinstalls actually succeeded, or NoConnectionError if the catalog was
        unreachable. Reinstalling goes entirely through the install method, so
        this never deregisters anything itself -- for plugins the bespoke
        installer deregisters the old version only AFTER a good download, so a
        failed update leaves the old version on disk AND registered."""
        to_update = getattr(self, desc.get_to_update_attr)()
        if isinstance(to_update, NoConnectionError):
            return to_update

        n_updated = 0
        install = getattr(self, desc.install_attr)
        for asset in to_update:
            result = install(asset)
            # desc.install_ok reads the install call's raw success value. A
            # plugin install answers True; the three data-only installers
            # answer the HTTP-style 200. That split is the one success dialect
            # the collapse cannot yet erase -- it lives as descriptor data and
            # is removed once the install boundary returns a single success
            # value.
            if desc.install_ok(result):
                n_updated += 1
            else:
                log.error(f"Failed to update {desc.display_name} {asset.asset_id}: {result!r}")

        return n_updated

    def get_plugins_to_update(self):
        return self._get_assets_to_update(PLUGIN)

    def update_all_plugins(self) -> "int | NoConnectionError":
        """Returns number of SUCCESSFULLY updated plugins"""
        return self._update_all(PLUGIN)

    def get_icons_to_update(self):
        return self._get_assets_to_update(ICON)

    def update_all_icons(self) -> "int | NoConnectionError":
        """Returns number of SUCCESSFULLY updated icon packs"""
        return self._update_all(ICON)

    def get_wallpapers_to_update(self):
        return self._get_assets_to_update(WALLPAPER)

    def update_all_wallpapers(self) -> "int | NoConnectionError":
        """Returns number of SUCCESSFULLY updated wallpapers"""
        return self._update_all(WALLPAPER)

    def get_sd_plus_bar_wallpapers_to_update(self):
        return self._get_assets_to_update(SD_PLUS_BAR)

    def update_all_sd_plus_bar_wallpapers(self) -> "int | NoConnectionError":
        """Returns number of SUCCESSFULLY updated SD+ bar wallpapers"""
        return self._update_all(SD_PLUS_BAR)

    def update_everything(self) -> "int | NoConnectionError":
        """
        Returns number of SUCCESSFULLY updated assets, or NoConnectionError
        """
        # Run every class's update leg first -- dispatched through the public
        # update_all_* names so a test stub of any of them is honoured -- THEN
        # check. A NoConnectionError leaking into the sum used to raise
        # TypeError, and one leg used to go unchecked entirely. (SD+ bar
        # wallpaper packs used to have no update leg at all -- installed once
        # and never auto-updated.)
        results = [getattr(self, desc.update_all_attr)() for desc in ASSET_TYPES]
        if any(isinstance(result, NoConnectionError) for result in results):
            return NoConnectionError()

        return sum(results)

class NoCompatibleVersion:
    pass
