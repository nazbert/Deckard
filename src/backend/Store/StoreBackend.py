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

from gi.repository import GLib

from autostart import is_flatpak
from src.backend.Store.StoreCache import StoreCache
from src.backend.Store.StoreURL import RepoRef, parse_repo_url
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.backend import http_client

from src.Signals import Signals

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
from src.backend.Store.store_result import Err, ErrReason, Ok, StoreFetchError, StoreResult


class _ResolvedVersion(NamedTuple):
    """What version resolution decides before a fetch of an entry. It names
    whether a compatible release exists, which commit to fetch, and, for a
    plugin only, the branch a custom entry pins. Version resolution returns
    None when no compatible or newest release exists, and the catalog then
    drops the entry. _prepare_asset tells the two apart by type."""
    compatible: bool
    commit: str | None
    branch: str | None


def same_repository(a: RepoRef | None, b: RepoRef | None) -> bool:
    """Whether two parsed store urls name the same GitHub repository.

    The comparison sites call this, and parse_repo_url never does. GitHub
    treats an owner and a repository name case-insensitively, so a tree
    stamped from acme/Widget must match a catalog entry spelled Acme/Widget.
    The cache keys built from the same helpers stay byte-compatible with what
    is already on disk.
    """
    if a is None or b is None:
        return False
    return (a.user.casefold(), a.repo.casefold()) == (b.user.casefold(), b.repo.casefold())


def repository_key(ref: RepoRef) -> tuple[str, str]:
    """The case-insensitive identity of a repository, for set membership."""
    return (ref.user.casefold(), ref.repo.casefold())


class InstalledAsset(NamedTuple):
    """One directory under an asset directory, read locally.

    asset_id is the directory name. install_* installs over that name, and
    download_repo validates the staged tree against it. manifest_id is what
    the tree on disk claims to be. The two agree for a canonical install, and
    differ for a copy kept aside under another name.
    """
    asset_id: str
    path: str
    sha: str                  # "" when neither .git nor VERSION can be read
    origin: RepoRef | None    # from the ORIGIN stamp, None for an old install
    manifest_id: str | None   # None when the tree has no readable manifest
    is_symlink: bool


class UpdateCheck(NamedTuple):
    """Everything the question "does this catalog entry need an update" needs.
    That is the installed asset the entry names, the sha that asset sits at,
    and the sha it should sit at.

    Every other field a store entry carries (name, descriptions, tags,
    licence, thumbnail) only displays the entry, and each one costs a
    request. This narrow tuple keeps a launch off the network for an entry
    the user never installed.
    """
    url: str
    ref: RepoRef
    asset_id: str | None      # None when the entry is not installed
    local_sha: str | None     # the installed asset's sha, None when not installed
    commit_sha: str | None    # the sha the entry should be installed at
    branch: str | None
    compatible: bool


class StoreBackend:
    STORE_REPO_URL = "https://github.com/StreamController/StreamController-Store" #"https://github.com/StreamController/StreamController-Store"
    STORE_CACHE_PATH = "Store/cache"
    # STORE_CACHE_PATH = os.path.join(gl.DATA_PATH, STORE_CACHE_PATH)
    STORE_BRANCH = "1.5.0"

    # Names the repository that a tree came from. Every install writes it
    # next to VERSION. The catalog names repositories, and an install
    # directory takes its name from a manifest id, so without this stamp only
    # a fetch of every entry's remote manifest connects the two.
    ORIGIN_FILE = "ORIGIN"

    WALLPAPERS_FILE = "Wallpapers.json"
    PLUGIN_FILE = "Plugins.json"
    ICON_FILE = "Icons.json"
    SDPLUSWALLPAPERS_FILE = "SDPlusBarWallpapers.json"


    # Caps the concurrent GitHub fetches. It is high enough to overlap the
    # catalog's 150 or so small requests, which dominate store load time, and
    # low enough to look unlike a scrape burst to raw.githubusercontent. It
    # aliases the shared session's pool size, so the semaphore cap and the
    # connection pool cannot drift apart. A cap above the pool would make the
    # surplus threads open throwaway connections.
    MAX_CONCURRENT_REQUESTS = http_client.POOL_MAXSIZE

    # Whitelist for a manifest-supplied asset id, which is the "id" field of
    # a plugin, an icon or a wallpaper. Such an id comes from a remote
    # manifest.json and becomes one path component under the app's data
    # directories, including an rmtree target and an install target. An id
    # must start alphanumeric, which rejects ".", ".." and a hidden
    # directory, and may continue with [A-Za-z0-9._-] only, which rejects
    # "/", "\\", whitespace and an absolute path. The length cap keeps it a
    # sane directory name.
    ASSET_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    @classmethod
    def is_safe_asset_id(cls, asset_id) -> TypeGuard[str]:
        """Whether a manifest-supplied id is safe as a single path component.
        Reject a bad id and never normalize it. An id that fails this check
        comes from a hostile or broken manifest, and a quiet repair would
        install into, or delete, a path the author never named."""
        return isinstance(asset_id, str) and bool(cls.ASSET_ID_PATTERN.fullmatch(asset_id))

    # A git commit sha holds exactly 40 hex characters. commit_sha reaches
    # git as an argv token and no shell reads it, so this gate checks the
    # shape only. A malformed value must fail loudly rather than reach
    # "git reset --hard".
    COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

    # A branch or ref name from a remote store catalog (plugin["branch"]).
    # Even as an argv token it must carry no shell metacharacter, newline,
    # NUL, or leading "-", which git reads as an option. The pattern stays
    # wide enough for a real ref name, with slashes, dots and inner dashes,
    # and rejects whitespace and every shell-significant character.
    SAFE_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

    # What sits installed on disk, per asset directory. One update-check pass
    # (process_store_data with include_images False) scans it once, rather
    # than once per catalog entry. It stays None outside such a pass, and
    # prepare_* then scans for itself, so nothing depends on the snapshot. A
    # whole-dict assignment is atomic, so a worker reads this pass's snapshot
    # or no snapshot. It sits on the class, so an instance built without
    # __init__, which the test harness does, reads the same default.
    _installed_index: "dict[str, dict[str, InstalledAsset]] | None" = None

    # The install directories that no catalog entry claimed this session, so
    # the fallback walk stops visiting them again. Each change rebinds this
    # set rather than mutates it, so two instances never share the
    # class-level default.
    _unresolvable_installs: "frozenset[str]" = frozenset()

    @classmethod
    def is_safe_commit_sha(cls, commit_sha) -> TypeGuard[str]:
        return isinstance(commit_sha, str) and bool(cls.COMMIT_SHA_PATTERN.fullmatch(commit_sha))

    @classmethod
    def is_safe_ref_name(cls, ref_name) -> TypeGuard[str]:
        """Whether a remote-catalog branch or ref name is safe to pass to
        git. It rejects a shell metacharacter, whitespace, a newline and a
        leading dash, so a catalog branch of "main; rm -rf ~" injects no
        shell and git reads no option."""
        return isinstance(ref_name, str) and bool(cls.SAFE_REF_PATTERN.fullmatch(ref_name))

    def __init__(self):
        self.store_cache = StoreCache()

        # Every fetch path shares this. The catalog prepare_* tasks on
        # _prepare_pool take it, and so do the direct calls from UI worker
        # threads for an install or an update. It caps the process-wide store
        # HTTP concurrency.
        self._fetch_limiter = threading.Semaphore(self.MAX_CONCURRENT_REQUESTS)

        # Fan-out pool for the catalog prepare_* tasks of
        # process_store_data. Its size matches the fetch cap, because further
        # workers would only queue on _fetch_limiter. Nothing that runs on
        # the pool submits to it, because prepare_* never re-enters, so the
        # pool cannot starve itself.
        self._prepare_pool = ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT_REQUESTS, thread_name_prefix="store-prepare")

        self.official_store_branch_cache: str = None

        # Seed the fallback list of official authors.
        self.official_authors = ["Core447", "StreamController"]

        # Fetch the real official authors on a background thread.
        threading.Thread(target=self._fetch_official_authors_background, daemon=True).start()

    def _fetch_official_authors_background(self):
        """Fetches official authors in a background thread and updates self.official_authors."""
        try:
            self.official_authors = self.get_official_authors()
            log.info(f"Official authors updated: {self.official_authors}")
        except Exception as e:
            log.warning(f"Failed to fetch official authors, using fallback: {e}")

    def get_stores(self) -> list[tuple[str, str]]:
        settings = gl.settings_manager.app()

        stores = []
        branch = self.get_official_store_branch()
        if not isinstance(branch, str) or not branch:
            # get_official_store_branch returns a str. Enforce that here too,
            # because a non-str branch reaches build_url urls and cache keys.
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
                    # Skip an unparseable store url here. It otherwise
                    # raises an opaque "x not in list" out of the cache-key
                    # helper, through fetch_and_parse_store_json, which
                    # catches json errors only, and out of the catalog load.
                    # One bad settings entry then blanks every store page.
                    log.error(f"Skipping custom store {url!r}: not a store repository url")
                    continue
                custom_branch = store.get("branch")
                if not isinstance(custom_branch, str) or not custom_branch:
                    # A third-party store follows the "main" convention. The
                    # official store instead falls back to STORE_BRANCH,
                    # currently "1.5.0", a version-pinned tag of this app's
                    # own store repository, which a custom repository does
                    # not share. The two values differ for that reason.
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
                    # An empty row, added in the settings window and never
                    # filled in. Skip it silently, like get_stores does,
                    # because it is no error.
                    continue
                if parse_repo_url(url) is None:
                    log.error(f"Skipping custom plugin {url!r}: not a store repository url")
                    continue
                plugins.append((url, plugin.get("branch")))

        return plugins
    
    def get_official_store_branch(self) -> str:
        """Always returns a str branch name. Every failure falls back to
        STORE_BRANCH, whether the fetch failed with a cache too stale, or
        versions.json is truncated or corrupt. An error object here would
        reach the (url, branch) tuples of get_stores, and build_url would
        interpolate it into urls and cache keys. The fallback never enters
        official_store_branch_cache, so a later successful fetch corrects it.
        """
        if self.official_store_branch_cache is not None:
            return self.official_store_branch_cache
        try:
            versions_file = self.get_remote_file(self.STORE_REPO_URL, "versions.json", branch_name="versions", force_refetch=True)
        except StoreFetchError:
            log.warning(f"Could not fetch versions.json; falling back to store branch {self.STORE_BRANCH}")
            return self.STORE_BRANCH
        try:
            versions = json.loads(versions_file)
        except (json.decoder.JSONDecodeError, TypeError) as e:
            # The stale-cache fallback can serve a truncated versions.json.
            # A raise here would freeze the store tab's spinner and leave the
            # page marked as loaded.
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

    def request_from_url(self, url: str) -> "requests.Response":
        # Callers run on worker threads, the prepare pool and the UI install
        # threads. The connection and the body read both stay inside the
        # limiter, which keeps a catalog load from looking like a scrape
        # burst. The shared session retries a 429 or a 5xx inside the
        # adapter, so a retry holds the same slot and cannot widen the burst.
        # Returns the read Response, or raises StoreFetchError.
        try:
            with self._fetch_limiter:
                req = http_client.get(url, stream=True, timeout=30)
                try:
                    if req.status_code == 200:
                        req.content  # read the body while the connection is open
                        return req
                    log.error(f"Request to {url} failed with status code {req.status_code}")
                    # Read the error body too, and then discard it. A close
                    # of a streamed response whose body nothing read closes
                    # the socket instead of returning it to the shared
                    # session's pool. A catalog carries many valid 404s,
                    # because attribution.json is optional and most entries
                    # miss it, and each one would then cost the next fetch a
                    # fresh TCP and TLS handshake, which the pooled session
                    # exists to avoid. A GitHub error body is a few bytes,
                    # and the 200 body above already reads without a bound.
                    req.content
                    raise StoreFetchError(url, f"status code {req.status_code}")
                finally:
                    req.close()  # content stays cached on the Response
        except requests.exceptions.RequestException as e:
            log.error(e)
            raise StoreFetchError(url, str(e)) from e
    
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
        Retrieves the content of a remote file from a GitHub repository.

        Parameters:
            repo_url (str): The URL of the GitHub repository.
            file_path (str): The path to the file within the repository.
            branch_name (str, optional): The name of the branch to read the file from. Defaults to "main".
                                         A commit hash also works here.

        Returns:
            str: The content of the remote file.

        Note:
            - The store cache holds each fetched file, so a repeated read costs no request.
            - A url on another domain than github.com becomes a
              raw.githubusercontent.com url.
        """
        byte_suffix = ""
        if data_type == "content":
            byte_suffix = "b"

        # data_type belongs to the cache key. Without it a binary fetch
        # (data_type="content") of a repo path lands under the same index
        # entry as a text fetch of that path, and one cache file then opens
        # in conflicting modes, by the order of the callers.
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

        answer: "requests.Response | None"
        try:
            answer = self.request_from_url(url)
        except StoreFetchError:
            answer = None  # offline or 429, so run the fallback path below
        if answer is None:
            # Fall back to the cached copy, even after the caller forced a
            # refetch, because a slightly stale catalog beats an empty or
            # errored store page. The entry's fetched age bounds this. Its
            # "date" field is a last-use clock that every read renews, so
            # that field cannot bound staleness.
            if self.store_cache.is_cached(url=repo_url, branch=branch_name, path=file_path, data_type=data_type):
                fetched = self.store_cache.get_fetched_date(url=repo_url, branch=branch_name, path=file_path, data_type=data_type)
                if fetched is not None and time.time() - fetched <= StoreCache.DAYS_TO_KEEP * 24 * 60 * 60:
                    log.warning(f"Serving cached copy of {file_path} from {repo_url} after failed fetch")
                    with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=f"r{byte_suffix}") as f:
                        return f.read()
            raise StoreFetchError(url, f"could not fetch {file_path} and no fresh cache")

        with self.store_cache.open_cache_file(url=repo_url, branch=branch_name, path=file_path, data_type=data_type, mode=f"w{byte_suffix}") as f:
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

        Runs under the fetch semaphore, like every sibling fetch.
        prepare_plugin calls this for every branch-pinned custom plugin during
        the catalog fan-out, and an uncapped requests.get() would evade the
        cap.

        Returns the sha str, or None when no sha resolves, for a url that
        names no repository, a non-200 answer, or an empty or unparseable
        commit list. A network failure raises StoreFetchError, like
        request_from_url does.
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
            raise StoreFetchError(repo_url, f"could not resolve {branch_name}: {e}") from e

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
    
    def get_official_authors(self) -> list:
        authors_json = self.get_remote_file(self.STORE_REPO_URL, "OfficialAuthors.json", self.STORE_BRANCH, force_refetch=True)
        return json.loads(authors_json)

    def fetch_and_parse_store_json(self, url: str, filename: str, branch: str, n_stores_with_errors: int = 0):
        try:
            store_file_json = self.get_remote_file(url, filename, branch, force_refetch=True)
            store_file_json = json.loads(store_file_json)
            return store_file_json, n_stores_with_errors
        except StoreFetchError:
            # The catalog is unreachable and no fresh cache exists. Count
            # this store as failed. The fetch layer logged the reason.
            n_stores_with_errors += 1
            return None, n_stores_with_errors
        except (json.decoder.JSONDecodeError, TypeError) as e:
            n_stores_with_errors += 1
            log.error(e)
            return None, n_stores_with_errors

    def process_store_data(self, filename: str, process_func: Callable[..., Any], get_custom_func: Callable[..., Any] | None, data_class, include_images=True, base_dir: str | None = None):
        """Fetches the catalog file from every configured store and prepares
        each entry on the fan-out pool.

        include_images picks which view of an entry this builds, and does
        more than attach a thumbnail.

        True builds the store window's view. It describes every entry in full,
        with its manifest, attribution and thumbnail, because the window shows
        every entry.

        False builds the update check's view. It reads only what decides
        whether an entry is installed and out of date. An entry that is not
        installed costs no request of its own, and nothing fetches or decodes
        an image. The objects it returns are incomplete store entries, with
        their display fields unset.

        base_dir names the asset directory this catalog installs into. The
        update-check view needs it to identify an install made before the
        origin stamp existed. The store window's view ignores it.
        """
        n_stores_with_errors = 0
        data_list = []

        if not include_images:
            # This opens an update-check pass. The fan-out below then scans
            # each asset directory once for the whole pass, rather than once
            # per catalog entry.
            self._installed_index = {}
        try:
            stores = self.get_stores()

            for url, branch in stores:
                store_file_json, n_stores_with_errors = self.fetch_and_parse_store_json(url, filename, branch, n_stores_with_errors)
                if store_file_json is not None:
                    data_list.extend(store_file_json)

            if n_stores_with_errors >= len(stores):
                # Every configured store's catalog fetch failed. None means
                # "no catalog at all", which _as_store_result turns into an
                # Err. An empty list means the stores answered and listed
                # nothing.
                return None

            custom_entries = [{"url": url, "branch": branch}
                              for url, branch in (get_custom_func() if get_custom_func is not None else [])]

            if not include_images and base_dir is not None:
                # Before the fan-out, so every entry below decides locally.
                self.resolve_unstamped_installs(base_dir, data_list + custom_entries)

            futures = [self._prepare_pool.submit(process_func, data, include_images, True) for data in data_list]
            futures += [self._prepare_pool.submit(process_func, asset, include_images, False)
                        for asset in custom_entries]

            # Collect each future on its own. One bad store entry must not
            # raise out of the fan-out and blank the page, because the page's
            # @log.catch load() would swallow it and leave the spinner up.
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as e:
                    # Drop this entry alone, for a StoreFetchError from its
                    # fetch and for any other fault. Only a failure of every
                    # store, below, counts as an error.
                    log.error(f"Store item preparation failed: {e!r}")
            results = [result for result in results if isinstance(result, data_class)]

            return results
        finally:
            if not include_images:
                # Drop the snapshot with the pass that took it. A later
                # prepare_* must never decide against an earlier snapshot.
                self._installed_index = None

    def _as_store_result(self, data) -> StoreResult[list]:
        # Turn the None that process_store_data returns into the typed
        # channel. Err means that every configured store's fetch failed.
        if data is None:
            return Err(ErrReason.NO_CONNECTION, "no store catalog could be fetched")
        return Ok(data)

    def get_all_plugins(self, include_images: bool = True) -> StoreResult[list[PluginData]]:
        return self._as_store_result(self.process_store_data(self.PLUGIN_FILE, self.prepare_plugin, self.get_custom_plugins, PluginData, include_images, gl.PLUGIN_DIR))

    def get_all_icons(self, include_images: bool = True) -> StoreResult[list[IconData]]:
        return self._as_store_result(self.process_store_data(self.ICON_FILE, self.prepare_icon, None, IconData, include_images, self.icons_dir()))

    def get_all_wallpapers(self, include_images: bool = True) -> StoreResult[list[WallpaperData]]:
        return self._as_store_result(self.process_store_data(self.WALLPAPERS_FILE, self.prepare_wallpaper, None, WallpaperData, include_images, self.wallpapers_dir()))

    def get_all_sd_plus_bar_wallpapers(self, include_images: bool = True) -> StoreResult[list[SDPlusBarWallpaperData]]:
        return self._as_store_result(self.process_store_data(self.SDPLUSWALLPAPERS_FILE, self.prepare_sd_plus_bar_wallpaper, None, SDPlusBarWallpaperData, include_images, self.sd_plus_bar_wallpapers_dir()))
    
    def get_manifest(self, url:str, commit:str) -> "dict | None":
        manifest = self.get_remote_file(url, "manifest.json", commit)  # raises on a failed fetch
        return json.loads(manifest)

    def get_attribution(self, url:str, commit:str) -> dict:
        try:
            result = self.get_remote_file(url, "attribution.json", commit)
        except StoreFetchError:
            return {}  # An optional file, so a failed fetch reads as empty
        try:
            return json.loads(result)
        except (json.decoder.JSONDecodeError, TypeError):
            return {}

    def _resolve_asset_version(self, entry: dict, desc: AssetTypeDescriptor, url: str):
        """Decide the commit an entry should be fetched at.

        A non-plugin entry always pins a version map. A plugin entry can
        omit one, which a branch-pinned custom plugin does, and a plugin entry
        alone resolves a branch tip. Returns a _ResolvedVersion, or None when
        no version resolves and the catalog drops the entry. An unreachable
        branch tip raises out of get_last_commit, and the fan-out's collect
        loop drops that entry.
        """
        compatible = True
        commit: str | None = None
        if not desc.is_plugin or "commits" in entry:
            newest = self.get_newest_compatible_version(entry["commits"])
            if newest is None:
                compatible = False
                newest = self.get_newest_version(list(entry["commits"].keys()))
                if newest is None:
                    return None
            commit = entry["commits"][newest]

        branch: str | None = None
        if desc.is_plugin:
            branch = entry.get("branch")
            if branch is not None:
                commit = self.get_last_commit(url, branch)

        return _ResolvedVersion(compatible, commit, branch)

    def _fetch_thumbnail(self, url: str, thumbnail_path: Any, ref: Any) -> "Image.Image | None":
        # List the entry without an image rather than drop it, because
        # get_web_image turns a failed fetch into None. This is the one image
        # guard, and it keeps _prepare_asset to a single line.
        return self.get_web_image(url, thumbnail_path, ref)

    def _translate_descriptions(self, manifest: dict) -> "tuple[Any, Any]":
        return (
            gl.lm.get_custom_translation(manifest.get("descriptions", {})),
            gl.lm.get_custom_translation(manifest.get("short-descriptions", {})),
        )

    def _prepare_asset(self, entry, desc: AssetTypeDescriptor, include_image: bool = True, verified: bool = False):
        """Turn one catalog entry into the descriptor's dataclass. This is
        the one implementation behind prepare_plugin, prepare_icon,
        prepare_wallpaper and prepare_sd_plus_bar_wallpaper.

        include_image picks the view. False builds what the update check reads
        (see check_entry_for_update) and fetches nothing for display. True
        builds the full store-window row, with the manifest, then the
        thumbnail, then the attribution. desc carries everything
        type-specific, which is the install directory, the dataclass, its id,
        name and version field names, and whether a branch applies.
        """
        base_dir = getattr(self, desc.base_dir_attr)()
        if not include_image:
            # The update-check view holds the fields that get_*_to_update
            # reads and install_* needs. Every field left unset here (name,
            # descriptions, tags, licence, thumbnail) costs a request and only
            # ever gets displayed.
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
            # One diagnostic for a url-less entry of any type. The three
            # non-plugin types dropped such an entry silently, and the plugin
            # path reached this point as an opaque KeyError.
            log.error(f"Skipping store entry without a url: {entry!r}")
            return None
        url = entry["url"]
        ref = self.repo_ref_for_entry(url)
        if ref is None:
            return None

        resolved = self._resolve_asset_version(entry, desc, url)
        if not isinstance(resolved, _ResolvedVersion):
            # None means no version resolved, and process_store_data drops
            # the entry.
            return resolved
        compatible, commit, branch = resolved
        # Typed Any because a plugin entry with no version map and no branch
        # leaves this None, while get_manifest, get_attribution and
        # get_web_image each declare commit as str. All three receive None
        # here and handle it. A later change retypes the fetch layer.
        ref_for_fetch: Any = commit or branch

        manifest = self.get_manifest(url, ref_for_fetch)  # raises on a failed fetch
        if not manifest:
            log.error(f"manifest failed to load for repository {url}")
            return None

        thumbnail_path: Any = manifest.get("thumbnail")
        image = self._fetch_thumbnail(url, thumbnail_path, ref_for_fetch)
        attribution = self.get_attribution(url, ref_for_fetch).get("generic", {})  # TODO: Choose correct attribution

        translated_description, translated_short_description = self._translate_descriptions(manifest)

        author = ref.user

        # These values come from JSON. The manifest and attribution
        # documents carry no types, and each missing field below becomes None
        # and feeds a StoreData field that src/windows/Store/StoreData.py
        # declares non-optional. Bind them through Any locals rather than
        # restate a type they do not have.
        descriptions: Any = manifest.get("descriptions") or None
        short_descriptions: Any = manifest.get("short-descriptions") or None
        tags: Any = manifest.get("tags") or None
        license_descriptions: Any = attribution.get("licence-descriptions", attribution.get("descriptions")) or None

        # A fetch of the remote manifest identified this entry, which costs
        # a request. Record the link while it is known, so the update check
        # identifies the same install with no fetch.
        self.note_installed_origin(base_dir, manifest.get("id"), url)

        fields = {
            "descriptions": descriptions,
            "short_descriptions": short_descriptions,
            "description": translated_description or manifest.get("description"),
            "short_description": translated_short_description or manifest.get("short-description"),

            "github": url or None,
            "author": author or None,
            "official": author in self.official_authors or False,
            "commit_sha": commit,
            "local_sha": self.get_local_sha_for_id(base_dir, manifest.get("id")),
            "minimum_app_version": manifest.get("minimum-app-version") or None,
            "app_version": manifest.get("app-version") or None,
            "repository_name": ref.repo,
            "tags": tags,

            "thumbnail": thumbnail_path or None,
            # _fetch_thumbnail turns a failed fetch into None, so it is the
            # one guard here and a second "or None" adds nothing.
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
            fetch_head_path = os.path.join(repo_path, '.git', 'FETCH_HEAD')

            with open(fetch_head_path, 'r') as file:
                lines = file.readlines()

                # The first line holds the latest commit hash.
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
        """Maps an install directory name to an InstalledAsset for
        everything under base_dir. This is the whole local half of the update
        check, and it makes no request.

        Every field serves identity or the verdict. The ORIGIN stamp names
        the repository the tree came from. The local manifest id says whether
        the directory holds the canonical install of that tree, and a renamed
        copy answers with the id it was copied from. The sha names the commit
        the tree sits on. is_symlink marks a checkout the user manages rather
        than an install this app owns.

        This skips an unsafe name, which also skips a dot-prefixed leftover
        from _swap_into_place.
        """
        index: dict[str, InstalledAsset] = {}
        try:
            names = os.listdir(base_dir)
        except OSError:
            # An asset class the user never installed from has no directory.
            # That means nothing is installed, and is no error.
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
        """The current pass's snapshot for base_dir. It scans once on first
        use. Outside a pass, which a lone prepare_* call is, it scans afresh
        every time.

        Two workers that reach an unscanned directory together both scan and
        both store. The results are equivalent snapshots taken moments apart,
        so that race costs one extra listing. Two overlapping passes that
        clear each other's snapshot cost the same, because the snapshot is a
        cache and never the authority.
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
        """The repository an installed tree came from, as download_repo and
        clone_repo stamped it. This reduces the url to a RepoRef, so a url in
        one spelling still matches a catalog entry in another."""
        try:
            with open(os.path.join(asset_path, self.ORIGIN_FILE)) as f:
                return parse_repo_url(f.readline().strip())
        except OSError:
            return None

    @staticmethod
    def read_local_manifest_id(asset_path: str) -> str | None:
        """The id an installed tree claims for itself. It equals the
        directory name for a canonical install, which install_* creates and
        _staged_tree_id_matches enforces. It differs for a renamed directory
        and for a copy kept aside."""
        try:
            with open(os.path.join(asset_path, "manifest.json")) as f:
                asset_id = json.load(f).get("id")
        except (OSError, ValueError):
            return None
        return asset_id if isinstance(asset_id, str) else None

    def stamp_origin(self, asset_path: str, repo_url: str) -> None:
        """Record which repository an installed tree came from.

        The catalog cannot supply this link. A catalog entry names a
        repository, an install directory takes its name from a manifest id,
        and nothing else on disk connects the two without a fetch of the
        remote manifest. Every install writes this stamp, and the first
        identification of an older install backfills it, so the update check
        answers "is this entry installed" from local state alone.

        Never written into a symlinked directory, which is a checkout the
        user manages, and this app does not write into it.
        """
        if os.path.islink(asset_path):
            return
        try:
            with open(os.path.join(asset_path, self.ORIGIN_FILE), "w") as f:
                f.write(f"{repo_url}\n")
        except OSError as e:
            # The app works without the stamp. Identity falls back to the
            # manifest lookup that wrote the stamp.
            log.warning(f"Could not stamp the origin of {asset_path}: {e}")

    def note_installed_origin(self, base_dir: str, asset_id, repo_url: str) -> None:
        """Backfill the origin stamp of an install that something else
        identified. A full prepare fetches the manifest anyway, so the
        identification happens once rather than once per launch.

        A stamp that disagrees with the url that just identified the tree
        gets overwritten. A renamed or transferred repository otherwise keeps
        a stamp that no catalog entry claims, and the install then stops
        receiving updates. Where a broken catalog lists one asset id under two
        urls, the last prepare to identify the tree wins, and the sweep
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

        Only a canonical directory can answer, which is one whose name
        equals the id its own manifest claims. A copy kept aside, such as
        com_x_Alpha_backup, carries the same origin stamp and otherwise
        claims the entry. An install over it cannot work, because download_repo
        refuses a staged tree whose manifest id differs from the directory
        name, so every launch would download an archive and throw it away. An
        install with no readable manifest stays eligible. It is broken rather
        than misnamed, and a reinstall repairs it.
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
        """Identifies an install that the origin stamp cannot answer for.
        This fetches a candidate entry's manifest, matches its id against the
        directory names, and stamps what it identifies, so no later pass
        looks it up again.

        A directory stays pending while it carries no stamp, which an install
        made before the stamp existed does, or while its stamp names a
        repository that no entry in this catalog claims. A renamed or
        transferred repository otherwise keeps a stamp that nothing matches,
        and the install then stops receiving updates, which is the failure the
        stamp prevents.

        This runs once per update-check pass, before the fan-out, and only
        while something is pending. An entry whose repository name appears in
        a pending directory's id goes first, because an asset id usually ends
        in its repository name. The walk stops once every pending directory is
        claimed, so the usual cost is one fetch per unresolved install rather
        than one per catalog entry. Where a broken catalog lists one asset id
        under two urls, the first entry in catalog order wins, because the
        sort is stable and a claimed directory leaves pending.

        A directory that nothing claims costs one full walk, which is one
        small fetch per entry and no image work. It then counts as
        unresolvable for the rest of the session, so no later pass repeats
        the walk. That memory never persists, so a store that was merely
        unreachable gets another chance at the next launch.

        A symlinked directory never goes pending. Nothing auto-updates it, so
        nothing needs to identify it, and this app does not write into a tree
        it does not own.
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
                # The same contract as the prepare fan-out's collect loop.
                # One bad entry must not raise out of the pass. Remote data
                # reaches a json parse, where a truncated manifest or an error
                # page fails, and a version parse, where a catalog key like
                # "latest" fails. This pre-pass runs before every leg of
                # update_everything, so a raise here stops all four legs.
                log.error(f"Could not identify installs from store entry {entry.get('url')!r}: {e!r}")

        if pending:
            # The walk covered the whole catalog and nothing claimed these.
            self._unresolvable_installs = frozenset(
                self._unresolvable_installs | {asset.path for asset in pending.values()}
            )

    def _claim_pending_install(self, entry: dict, pending: dict, installed: dict) -> None:
        """One entry's turn at the pending directories. This fetches its
        manifest and stamps a pending directory that the id names. It raises
        whatever the remote data raises, and the caller catches per entry."""
        ref = parse_repo_url(entry.get("url"))
        if ref is None:
            return
        url = entry["url"]
        # Read the manifest at the revision the entry points at. For a
        # branch-pinned entry the branch name is revision enough, so this
        # identification costs no tip lookup.
        revision = entry.get("branch")
        if revision is None:
            commits = entry.get("commits")
            if not isinstance(commits, dict) or not commits:
                return
            newest = self.get_newest_compatible_version(commits) or self.get_newest_version(list(commits.keys()))
            revision = commits[newest]
        manifest = self.get_manifest(url, revision)  # raises into the per-entry catch
        if not manifest:
            return
        asset_id = manifest.get("id")
        if not self.is_safe_asset_id(asset_id):
            return
        asset = pending.pop(asset_id, None)
        if asset is None:
            return
        if asset.manifest_id is not None and asset.manifest_id != asset.asset_id:
            # No install can land on a directory whose name differs from
            # the id it claims, because the staged-id check refuses it.
            return
        self.stamp_origin(asset.path, url)
        # Keep the pass snapshot current. Every entry checked after this one
        # must see the directory as stamped.
        installed[asset_id] = asset._replace(origin=ref)

    def check_entry_for_update(self, entry: dict, base_dir: str) -> "UpdateCheck | None":
        """Resolves one catalog entry against what is installed under
        base_dir, fetching nothing the update decision does not need.

        Identity comes from the ORIGIN stamp that every install carries. The
        entry names a repository, the stamp names the directory that came
        from that repository, and the directory's VERSION names the commit it
        sits on. All of that is local, so an entry the user never installed
        costs nothing beyond the catalog that named it. Its manifest,
        attribution and thumbnail only ever served the display.

        Identity must not come from a match of the catalog's commit shas
        against what is installed. The store rewrites an entry's sha in place
        under the same version key, so the sha an install sits on leaves the
        listing at the moment an update exists.

        One case still costs a request. A branch-pinned entry, which a custom
        plugin is, names a branch rather than a version map, and its tip must
        resolve.

        This answers from stamps alone, so it is complete only inside a pass
        that ran resolve_unstamped_installs first, which process_store_data
        does for the update-check view. A direct call reads an install with a
        missing or stale stamp as not installed.
        """
        ref = self.repo_ref_for_entry(entry.get("url"))
        if ref is None:
            return None
        url = entry["url"]
        installed = self.installed_assets(base_dir)

        branch = entry.get("branch")
        compatible = True
        if branch is not None:
            # An unreachable tip raises out of get_last_commit and the
            # fan-out drops the entry, rather than write a failure into
            # commit_sha.
            target = self.get_last_commit(url, branch)
        else:
            commits = entry.get("commits")
            if not isinstance(commits, dict) or not commits:
                log.error(f"Skipping store entry {url!r}: it pins no version")
                return None
            newest = self.get_newest_compatible_version(commits)
            if newest is None:
                # No version matches this app major. Pin the newest one, the
                # way prepare_* does, and let the caller refuse to install
                # it.
                compatible = False
                newest = self.get_newest_version(list(commits.keys()))
            target = commits[newest]

        asset = self.match_installed_asset(ref, installed)
        if asset is None:
            return UpdateCheck(url, ref, None, None, target, branch, compatible)

        if asset.is_symlink:
            # A symlinked install is a checkout the user manages, which a
            # dev workflow points at a working tree. An install over it
            # replaces the link with a downloaded copy and takes the working
            # tree out of the plugin directory, so auto-update skips it. The
            # store window still offers the update.
            log.info(f"Skipping auto-update of {asset.asset_id}: it is a symlink to a tree this app does not own")
            return UpdateCheck(url, ref, None, None, target, branch, compatible)

        # An empty sha means that neither .git nor VERSION could be read,
        # from a half-written or hand-copied tree. It matches no commit, so
        # the entry reads as outdated and a reinstall repairs it.
        return UpdateCheck(url, ref, asset.asset_id, asset.sha, target, branch, compatible)

    def get_local_sha_for_id(self, base_dir: str, asset_id) -> str | None:
        """get_local_sha behind the asset-id whitelist. An unsafe or missing
        manifest id never probes the filesystem, and reads as not installed,
        which is None."""
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

    def get_web_image(self, url: str, path: str, branch: str = "main") -> "Image.Image | None":
        try:
            result = self.get_remote_file(url, path, branch, data_type="content")
        except StoreFetchError:
            return None  # Offline or rate-limited, and logged. List with no image.
        except Exception as e:
            # Catch Exception, so a pool worker still honours SystemExit and
            # KeyboardInterrupt.
            log.error(f"Failed to fetch image {path} from {url}: {e}")
            return None
        try:
            return Image.open(BytesIO(result))
        except Exception as e:
            log.warning(f"Could not decode image {path} from {url}: {e}")
            return None
    
    def repo_ref_for_entry(self, url: object) -> RepoRef | None:
        """Parses a catalog/settings entry's url, reporting the skip.

        A store entry carries whatever the catalog json or the user's
        settings hold. An entry that names no usable repository drops here,
        rather than raise through the prepare fan-out as an opaque
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

    def get_main_folder_of_zip(self, zip_path: str) -> str | None:
        extracted_folder_name = None
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_contents = zip_ref.namelist()
            for item in zip_contents:
                if not item.endswith("/"): # A directory name ends with /
                    continue
                if item.count("/") > 1:
                    continue

                if extracted_folder_name is not None:
                    log.error("Multiple folders in zip")
                    return None
                extracted_folder_name = item.split("/")[0]


        if extracted_folder_name is None:
            log.error("Could not find extracted folder name")
            return None

        return extracted_folder_name

    def zip_has_unsafe_members(self, zip_path: str) -> bool:
        """A second Zip-Slip check on a downloaded archive.

        This app downloads a GitHub-generated .zip archive only, and CPython's
        zipfile strips a leading "/" and ".." during extraction, so that
        library is the first guard. This check keeps a later change safe, such
        as another archive source, or a tar or other format that CPython does
        not sanitize the same way. A member whose normalized path is absolute,
        or which escapes the extraction root, fails the whole archive.
        """
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            for name in zip_ref.namelist():
                # Reject absolute paths and drive-style/backslash members.
                if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
                    log.error(f"Archive member has absolute path, refusing: {name!r}")
                    return True
                normalized = os.path.normpath(name.replace("\\", "/"))
                # normpath collapses "a/../b". A leading "..", or a bare
                # "..", resolves outside the extraction root.
                if normalized == ".." or normalized.startswith(".." + os.sep) or normalized.startswith("../"):
                    log.error(f"Archive member escapes extraction root, refusing: {name!r}")
                    return True
        return False

    @staticmethod
    def _remove_leftover(path: str) -> None:
        """Remove a transient swap tree, or a stray file or symlink."""
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _staged_tree_id_matches(self, staging_tree: str, expected_id: str | None) -> bool:
        """The one staged-manifest identity check. When the caller knows the
        asset id it installs, and that id also names the install directory,
        the downloaded tree's manifest must agree. A drift between catalog and
        repository, or a hostile manifest, must never swap over the installed
        pack."""
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
        """Replace directory with the fully staged tree, and delete the old
        install only after the new one is in place.

        This first moves the staged tree next to the destination. That move is
        the one step that can cross a filesystem, because an environment
        variable can put PLUGIN_DIR on another device, and it runs while the
        old install stays intact. The two renames that follow share a parent
        and are atomic. The transient siblings carry a dot prefix, so the
        plugin and pack directory scanners never read a crash leftover as a
        real install. The next install of the same asset sweeps a leftover."""
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
            # Put the old install back, then report the failure.
            if moved_old_aside and not os.path.lexists(directory):
                os.replace(old_tree, directory)
            shutil.rmtree(new_tree, ignore_errors=True)
            raise
        self._remove_leftover(old_tree)

    def download_repo(self, repo_url:str, directory:str, commit_sha:str = None, branch_name:str = None, expected_id:str = None) -> StoreResult[None]:
        """Returns Ok(None) on success, or an Err that names the failure.
        INSTALL_FAILED covers a hard failure, such as a missing git on the
        devel clone path or an unresolvable branch. INVALID_ASSET covers a
        staged tree that fails the expected_id manifest check. NO_CONNECTION
        covers a network or archive fault. This stays inside the backend. The
        install_* methods alone call it, and they hand the Err to the UI.

        The install is transactional. It downloads, extracts, validates and
        VERSION-stamps the new tree in a staging area, and then _swap_into_place
        moves it into directory. The delete of the previous tree happens only
        after the new one lands, so any failure leaves the old install
        untouched."""
        if not is_flatpak() and gl.argparser.parse_args().devel:
            return self.clone_repo(repo_url, directory, commit_sha, branch_name, expected_id)


        ref = parse_repo_url(repo_url)
        if ref is None:
            log.error(f"Could not derive a repository from {repo_url!r}")
            return Err(ErrReason.INSTALL_FAILED, f"could not derive a repository from {repo_url!r}")
        username = ref.user
        projectname = ref.repo.lower()
        sha = commit_sha
        if commit_sha is None and branch_name is not None:
            # Resolve the sha that gets written into VERSION.
            try:
                sha = self.get_last_commit(repo_url, branch_name)
            except StoreFetchError:
                return Err(ErrReason.NO_CONNECTION, f"could not resolve branch {branch_name!r} of {repo_url}")
            if sha is None:
                # Fail here rather than build a ".../None.zip" url, which
                # 404s later and logs a misleading reason.
                log.error(f"Could not resolve branch {branch_name!r} of {repo_url}")
                return Err(ErrReason.INSTALL_FAILED, f"branch {branch_name!r} of {repo_url} has no commits")
        if sha is None:
            # The caller gave neither a commit sha nor a branch, so nothing
            # exists to download.
            log.error(f"Refusing to download {repo_url}: no commit sha and no branch")
            return Err(ErrReason.INSTALL_FAILED, f"no commit sha and no branch for {repo_url}")

        zip_url = f"https://github.com/{username}/{projectname}/archive/{sha}.zip"

        zip_path = os.path.join(gl.DATA_PATH, "cache", f"{projectname}-{sha}.zip")

        # The helper creates the cache directory, raises on an HTTP error
        # status, and removes a partial or zero-byte archive itself, so a
        # failed download leaves nothing behind to poison the next run.
        try:
            http_client.download_to_file(zip_url, zip_path, timeout=30)
        except Exception as e:
            log.error(e)
            return Err(ErrReason.NO_CONNECTION, f"download of {projectname} failed: {e}")

        ## Extract
        extracted_folder = None
        try:
            # Resolve the folder name from the zip listing before the
            # unpack, so the cleanup below also covers a failure mid-extract.
            # A github url ignores case, so the name can differ from
            # projectname.
            extracted_folder_name = self.get_main_folder_of_zip(zip_path)
            if extracted_folder_name is None:
                # The helper found no single root folder in the archive.
                raise ValueError("could not determine the archive's root folder")
            # Refuse a traversal or absolute member before
            # shutil.unpack_archive writes to disk.
            if self.zip_has_unsafe_members(zip_path):
                log.error(f"Refusing to extract {projectname}: archive contains unsafe member paths")
                return Err(ErrReason.NO_CONNECTION, f"{projectname} archive contains unsafe member paths")
            extracted_folder = os.path.join(gl.DATA_PATH, "cache", extracted_folder_name)
            if os.path.exists(extracted_folder):
                shutil.rmtree(extracted_folder)
            shutil.unpack_archive(zip_path, os.path.join(gl.DATA_PATH, "cache"))

            # Validate and complete the staged tree before it reaches the
            # install location. VERSION must exist before the swap. A tree
            # without VERSION reads as local_sha None, which means not
            # installed, so a crash after the swap and before a late VERSION
            # write leaves an install that nothing retries.
            if not self._staged_tree_id_matches(extracted_folder, expected_id):
                return Err(ErrReason.INVALID_ASSET, f"staged {projectname} tree does not match expected id {expected_id!r}")
            with open(os.path.join(extracted_folder, "VERSION"), "w") as f:
                f.write(sha)
            # Stamp the staging tree, like VERSION above, so the swap
            # publishes the install and its origin together.
            self.stamp_origin(extracted_folder, repo_url)

            self._swap_into_place(extracted_folder, directory)
        except Exception as e:
            log.error(f"Failed to extract/install {projectname}: {e}")
            return Err(ErrReason.NO_CONNECTION, f"failed to extract/install {projectname}: {e}")
        finally:
            # Leave no archive and no extracted temp folder behind, and let
            # no cleanup failure replace the outcome of the try block.
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except OSError:
                pass
            if extracted_folder is not None and os.path.isdir(extracted_folder):
                shutil.rmtree(extracted_folder, ignore_errors=True)

        return Ok(None)

    def clone_repo(self, repo_url:str, local_path:str, commit_sha:str = None, branch_name:str = None, expected_id:str = None) -> StoreResult[None]:
        if commit_sha is not None:
            # Clone the main branch first.
            branch_name = None

        # commit_sha and branch_name come from the remote store catalog, as
        # plugin["commits"][version] and plugin["branch"]. A catalog branch of
        # "main; <cmd>" injects a shell wherever a command line reads it.
        # Validate both here and pass them to git as argv tokens, with no
        # shell, through git -C below.
        if commit_sha is not None and not self.is_safe_commit_sha(commit_sha):
            log.error(f"Refusing to clone {repo_url}: malformed commit sha {commit_sha!r}")
            return Err(ErrReason.INVALID_ASSET, f"malformed commit sha {commit_sha!r}")
        if branch_name is not None and not self.is_safe_ref_name(branch_name):
            log.error(f"Refusing to clone {repo_url}: unsafe branch/ref name {branch_name!r}")
            return Err(ErrReason.INVALID_ASSET, f"unsafe branch/ref name {branch_name!r}")

        # Check for git, which most Linux systems have.
        if shutil.which("git") is None:
            log.error("Git is not installed on this system. Please install it.")
            return Err(ErrReason.INSTALL_FAILED, "git is not installed on this system")

        # The same transactional contract as download_repo. Clone and
        # prepare in a staging directory under cache/, then swap. A removal
        # first destroys the existing install before the clone can fail.
        staging = os.path.join(gl.DATA_PATH, "cache", f".clone-staging.{os.path.basename(os.path.normpath(local_path))}")
        os.makedirs(os.path.join(gl.DATA_PATH, "cache"), exist_ok=True)
        self._remove_leftover(staging)

        try:
            # Clone the newest commit of the default branch.
            rc = self.subp_call(["git", "clone", repo_url, staging])
            if rc != 0 or not os.path.isdir(staging):
                log.error(f"git clone of {repo_url} failed with exit code {rc}")
                return Err(ErrReason.INSTALL_FAILED, f"git clone of {repo_url} failed (exit {rc})")

            # Add both the final home and the staging clone to the safe
            # directory list, so git logs no dubious-ownership warning. The
            # pull, reset and checkout below all run in staging.
            # FIXME: Check if not already added
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(local_path)])
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(staging)])

            # Run git pull to create .git/FETCH_HEAD, which the update check
            # reads. git -C takes argv tokens and runs no shell.
            self.subp_call(["git", "-C", staging, "pull"])

            # Set the repository to the given commit_sha, and check the rc
            # for the reason the checkout below does. An unreachable catalog
            # sha, after an upstream force-push or a collected commit, leaves
            # staging on the default-branch tip. That tree then passes
            # validation, takes a VERSION stamp of a sha it does not hold, and
            # installs as a success, which ships a wrong tree.
            #
            # Fail hard rather than fall back to the default tip. Every other
            # git failure in this function does the same, for the clone rc,
            # the checkout rc and a missing git, and each returns
            # INSTALL_FAILED. The non-devel path already answers this exact
            # failure the same way, because download_repo builds
            # ".../<sha>.zip", which 404s on an unreachable sha and fails the
            # install. A fallback here would leave the devel clone path as the
            # one place in the store where an unreachable sha still installs
            # something.
            if commit_sha is not None:
                rc = self.subp_call(["git", "-C", staging, "reset", "--hard", commit_sha])
                if rc != 0:
                    log.error(f"git reset --hard {commit_sha!r} failed with exit code {rc} for {repo_url} "
                              f"(commit unreachable?) -- refusing to install the default-branch tip")
                    return Err(ErrReason.INSTALL_FAILED, f"git reset --hard {commit_sha!r} failed (exit {rc})")
            elif branch_name is not None:
                # Use checkout rather than switch. A custom plugin can pin a
                # tag, or any detachable ref, which git switch refuses without
                # --detach. Check the rc. An ignored rc ships the
                # default-branch tip stamped as the ref whenever the ref the
                # user typed does not exist.
                rc = self.subp_call(["git", "-C", staging, "checkout", branch_name])
                if rc != 0:
                    log.error(f"git checkout {branch_name!r} failed with exit code {rc} for {repo_url}")
                    return Err(ErrReason.INSTALL_FAILED, f"git checkout {branch_name!r} failed (exit {rc})")

            # Keep the order of download_repo. Validate the staged tree,
            # then stamp VERSION, then swap.
            if not self._staged_tree_id_matches(staging, expected_id):
                return Err(ErrReason.INVALID_ASSET, f"staged tree does not match expected id {expected_id!r}")

            # Write the version stamp.
            version_stamp = commit_sha or branch_name
            if version_stamp is None:
                log.error(f"Refusing to stamp VERSION for {repo_url}: no commit sha and no branch")
                return Err(ErrReason.INVALID_ASSET, f"no commit sha and no branch for {repo_url}")
            with open(os.path.join(staging, "VERSION"), "w") as f:
                f.write(version_stamp)
            self.stamp_origin(staging, repo_url)

            self._swap_into_place(staging, local_path)
        except Exception as e:
            log.error(f"Failed to stage devel clone of {repo_url}: {e}")
            return Err(ErrReason.NO_CONNECTION, f"failed to stage devel clone of {repo_url}: {e}")
        finally:
            self._remove_leftover(staging)

        return Ok(None)

    def install_plugin(self, plugin_data:PluginData, auto_update: bool = False) -> StoreResult[None]:
        url = plugin_data.github
        plugin_id = plugin_data.plugin_id

        if not self.is_safe_asset_id(plugin_id):
            # The id names the install directory, which download_repo
            # replaces by a swap. A traversal id such as "../../.." must never
            # reach that join.
            log.error(f"Refusing to install plugin with unsafe id {plugin_id!r} from {url}")
            return Err(ErrReason.INVALID_ASSET, f"unsafe plugin id {plugin_id!r}")

        if url is None:
            log.error(f"Refusing to install plugin {plugin_id!r}: no repository url")
            return Err(ErrReason.INVALID_ASSET, f"no repository url for plugin {plugin_id!r}")

        local_path = os.path.join(gl.PLUGIN_DIR, plugin_id)

        response = self.download_repo(repo_url=url, directory=local_path, commit_sha=plugin_data.commit_sha, branch_name=plugin_data.branch, expected_id=plugin_id)

        # Stop before an install script runs, or a plugin reload lands, on a
        # missing or partial tree.
        if isinstance(response, Err):
            return response

        # On an update the new tree already sits in place. Deregister the
        # old version now, which also purges sys.modules, so load_plugins
        # below imports the new code. A deregister after a successful download
        # leaves a failed update with the old version on disk and registered,
        # while a deregister first would need a recovery reload.
        plugin_manager = gl.plugin_manager
        if plugin_manager is not None and plugin_manager.get_plugin_by_id(plugin_id) is not None:
            try:
                self.uninstall_plugin(plugin_id, remove_from_pages=False, remove_files=False)
            except Exception as e:
                log.error(f"Deregistering the old version of {plugin_id} failed: {e}")

        # Run the install script when one exists. Use the python binary that
        # runs this process, so a venv keeps its dependencies. Pass a list and
        # run no shell. An f-string command breaks on a space in the data
        # path, and lets a crafted path component inject shell syntax.
        if os.path.isfile(os.path.join(local_path, "__install__.py")):
            subprocess.run([sys.executable, os.path.join(local_path, "__install__.py")], start_new_session=True)

        # Install the dependencies from requirements.txt.
        if os.path.isfile(os.path.join(local_path, "requirements.txt")):
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", os.path.join(local_path, "requirements.txt")], start_new_session=True)

        # Update the plugin manager.
        if plugin_manager is not None:
            plugin_manager.load_plugins()
            plugin_manager.init_plugins()
            plugin_manager.generate_action_index()

        # A version-gated plugin installs without an error. Its files land
        # on disk and the store button flips to installed, but the reload
        # above puts it in disabled_plugins. Tell the user now. Without this
        # call the first feedback is the disabled-plugins toast of the next
        # launch, which a user reads as a config reset after a restart.
        self.notify_if_installed_disabled(plugin_id)

        # Update the UI.
        if gl.app is not None and recursive_hasattr(gl, "app.main_win.sidebar.action_chooser"):
            GLib.idle_add(gl.app.main_win.sidebar.action_chooser.plugin_group.update)

        # Update the page on every deck.
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # Check both, so an auto-update raises no error here.
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    # Load the action objects.
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

        # Tell the plugin actions.
        gl.signal_manager.trigger_signal(Signals.PluginInstall, plugin_data.plugin_id)

        log.success(f"Plugin {plugin_id} installed successfully under: {local_path} with sha: {plugin_data.commit_sha}")
        return Ok(None)

    @staticmethod
    def notify_if_installed_disabled(plugin_id: str) -> bool:
        """Tells the user at once when the register() gate disabled the
        plugin that just installed, which puts it in disabled_plugins rather
        than plugins. Without this call the next launch's startup toast gives
        the first feedback. Returns whether it told the user."""
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
        # 1. Remove every action object from every cached page of every
        # controller, and not from the active page alone. A visited page that
        # still sits in the page cache otherwise holds dead plugin action
        # objects alive with no teardown. Take the snapshot through the
        # _pages_lock accessor. A loop over gl.page_manager.pages races
        # load_page, discard_controller and clear_old_cached_pages, which
        # mutate the dict from other threads.
        for page in (gl.page_manager.all_cached_pages() if gl.page_manager is not None else []):
            page.remove_plugin_action_objects(plugin_id=plugin_id)
            if remove_from_pages:
                page.remove_plugin_actions_from_json(plugin_id=plugin_id)

        # 2. Inform the plugin base.
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            return None
        plugins = plugin_manager.get_plugins()
        plugin = plugin_manager.get_plugin_by_id(plugin_id)
        if plugin is None:
            return None
        # Capture the real import folder now. on_uninstall() and the rmtree
        # below can remove the directory, and the symlink branch can rewrite
        # plugin.PATH. The sys.modules purge below needs the real
        # "plugins.<folder>" prefix, which differs from plugin_id, the
        # manifest id, once someone renames the folder.
        plugin_folder = os.path.basename(os.path.normpath(plugin.PATH))
        if remove_files:
            plugin.on_uninstall()
            
            # 3. Remove the plugin folder.
            if os.path.islink(plugin.PATH):
                symlink_target = os.readlink(plugin.PATH)
                log.warning(f"Plugin {plugin.plugin_name} is inside a Symlink!")
                plugin.PATH = symlink_target

            shutil.rmtree(plugin.PATH)

        # 4. Delete the plugin base object.
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

        # Update the page on every deck.
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # Check both, so an auto-update raises no error here.
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    # Load the action objects.
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

    # The three data-only pack types, which are the icon pack, the wallpaper
    # pack and the SD+ bar wallpaper pack, share one install and uninstall
    # pair, and the descriptor selects the type. Each one joins a
    # manifest-supplied id under a data directory, and then removes or
    # replaces the result, so each one must reject an unsafe id. A traversal
    # id would give a data-only pack a filesystem-wide delete, with no code
    # execution. The plugin install and uninstall pair above stays separate,
    # because it runs pip and __install__.py, and drives the plugin-manager
    # deregister and reload, which a pack does not need.

    def _install_asset(self, data, desc: AssetTypeDescriptor) -> StoreResult[None]:
        """Download one data-only asset into its per-type directory. Returns
        the StoreResult of download_repo, which is Ok(None) or an Err, or
        Err(INVALID_ASSET) for an unsafe id or a missing url. The plugin
        installer reports the same failure for those two conditions.

        Nothing deletes the installed pack first. download_repo stages and
        validates the new tree, and swaps it over the installed one at the
        end, so a failed download leaves the installed pack in place. An
        uninstall first would lose the pack when a download failed
        mid-update."""
        asset_id = data.asset_id
        if not self.is_safe_asset_id(asset_id):
            log.error(f"Refusing to install {desc.display_name} with unsafe id {asset_id!r} from {data.github}")
            return Err(ErrReason.INVALID_ASSET, f"unsafe {desc.display_name} id {asset_id!r}")

        github = data.github
        if github is None:
            log.error(f"Refusing to install {desc.display_name} {asset_id!r}: no repository url")
            return Err(ErrReason.INVALID_ASSET, f"no repository url for {desc.display_name} {asset_id!r}")

        asset_path = os.path.join(getattr(self, desc.base_dir_attr)(), asset_id)
        return self.download_repo(repo_url=github, directory=asset_path, commit_sha=data.commit_sha, expected_id=asset_id)

    def _uninstall_asset(self, data, desc: AssetTypeDescriptor):
        """Delete one data-only asset's installed directory. Returns 400 for
        an unsafe id, and None otherwise."""
        asset_id = data.asset_id
        if not self.is_safe_asset_id(asset_id):
            log.error(f"Refusing to uninstall {desc.display_name} with unsafe id {asset_id!r}")
            return 400
        asset_path = os.path.join(getattr(self, desc.base_dir_attr)(), asset_id)
        if os.path.exists(asset_path):
            shutil.rmtree(asset_path)

    def install_icon(self, icon_data:IconData) -> StoreResult[None]:
        return self._install_asset(icon_data, ICON)

    def uninstall_icon(self, icon_data:IconData):
        return self._uninstall_asset(icon_data, ICON)

    def install_wallpaper(self, wallpaper_data:WallpaperData) -> StoreResult[None]:
        return self._install_asset(wallpaper_data, WALLPAPER)

    def uninstall_wallpaper(self, wallpaper_data:WallpaperData):
        return self._uninstall_asset(wallpaper_data, WALLPAPER)

    def install_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData) -> StoreResult[None]:
        return self._install_asset(sd_plus_bar_wallpaper_data, SD_PLUS_BAR)

    def uninstall_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData):
        return self._uninstall_asset(sd_plus_bar_wallpaper_data, SD_PLUS_BAR)

    def get_plugin_for_id(self, plugin_id) -> "PluginData | None":
        """The catalog plugin with this id, or None, which an unreachable
        store also gives. get_all_plugins returns a StoreResult, so this
        narrows an Err rather than iterates it. A loop over an Err raises
        TypeError under an @log.catch, which swallows it and strands the
        caller with a stuck install spinner or an onboarding page that never
        loads."""
        result = self.get_all_plugins()
        if isinstance(result, Err):
            log.error(f"Cannot resolve plugin {plugin_id!r}: {result.detail or result.reason.value}")
            return None
        for plugin in result.value:
            if plugin.plugin_id == plugin_id:
                return plugin
        return None

    ## Updates
    def _get_assets_to_update(self, desc: AssetTypeDescriptor) -> StoreResult[list]:
        """The installed assets of one class that have a newer, compatible
        and known target version. This is the shared update-check decision.
        The update-check view fetches no thumbnail, and makes no request for a
        catalog entry that was never installed. It dispatches through the
        public get_all_* name, so a test stub of that name applies."""
        result = getattr(self, desc.get_all_attr)(include_images=False)
        if isinstance(result, Err):
            return result
        assets = result.value

        to_update: list = []
        for asset in assets:
            if asset.local_sha is None:
                # The asset is not installed.
                continue
            if asset.local_sha == asset.commit_sha:
                # The asset is up to date.
                continue
            if asset.commit_sha is None:
                # No known target. That happens for a branch-pinned plugin
                # whose tip did not resolve, and for an entry of any type with
                # a "branch" key or a null version map, because
                # check_entry_for_update reads a branch for every type. A None
                # target cannot install, so this skip avoids a doomed download
                # and changes neither the count nor the disk.
                continue
            if asset.is_compatible is False:
                # prepare pins the newest incompatible commit when no
                # compatible version exists, so the store still lists the
                # entry. An auto-update onto it replaces a working asset with
                # a build for another app major. Skip it and report it. The
                # store UI's update button reads the same verdict.
                log.warning(
                    f"Skipping update of {desc.display_name} {asset.asset_id}: pinned version "
                    f"{asset.commit_sha} is not compatible with app version {gl.app_version}"
                )
                continue
            to_update.append(asset)

        return Ok(to_update)

    def _update_all(self, desc: AssetTypeDescriptor) -> StoreResult[int]:
        """Reinstall every out-of-date asset of one class. Returns Ok(n)
        with the number of reinstalls that succeeded, or the catalog's Err
        when the catalog was unreachable. Every reinstall goes through the
        install method, so this deregisters nothing itself. For a plugin the
        installer deregisters the old version only after a good download, so a
        failed update leaves the old version on disk and registered."""
        to_update = getattr(self, desc.get_to_update_attr)()
        if isinstance(to_update, Err):
            return to_update

        n_updated = 0
        install = getattr(self, desc.install_attr)
        for asset in to_update.value:
            result = install(asset)
            # install_* answers one StoreResult, and a success is an Ok.
            # Narrow the result rather than test its truth. An Err is truthy,
            # so a truth test would count a failure as a success.
            if isinstance(result, Err):
                log.error(f"Failed to update {desc.display_name} {asset.asset_id}: {result!r}")
                continue
            n_updated += 1

        return Ok(n_updated)

    def get_plugins_to_update(self) -> StoreResult[list]:
        return self._get_assets_to_update(PLUGIN)

    def update_all_plugins(self) -> StoreResult[int]:
        """Returns Ok with the number of plugins updated, or an Err."""
        return self._update_all(PLUGIN)

    def get_icons_to_update(self) -> StoreResult[list]:
        return self._get_assets_to_update(ICON)

    def update_all_icons(self) -> StoreResult[int]:
        """Returns Ok with the number of icon packs updated, or an Err."""
        return self._update_all(ICON)

    def get_wallpapers_to_update(self) -> StoreResult[list]:
        return self._get_assets_to_update(WALLPAPER)

    def update_all_wallpapers(self) -> StoreResult[int]:
        """Returns Ok with the number of wallpapers updated, or an Err."""
        return self._update_all(WALLPAPER)

    def get_sd_plus_bar_wallpapers_to_update(self) -> StoreResult[list]:
        return self._get_assets_to_update(SD_PLUS_BAR)

    def update_all_sd_plus_bar_wallpapers(self) -> StoreResult[int]:
        """Returns Ok with the number of SD+ bar wallpapers updated, or an
        Err."""
        return self._update_all(SD_PLUS_BAR)

    def update_everything(self) -> StoreResult[int]:
        """Returns Ok with the number of assets updated, or the first
        Err."""
        # Run every class's update leg, and aggregate afterwards. Each leg
        # dispatches through the public update_all_* name, so a test stub of
        # any of them applies. They run in ASSET_TYPES order, which puts
        # plugins first, the one leg that reloads the plugin manager. An Err
        # from any leg becomes the overall Err, and otherwise the successful
        # counts sum.
        results = [getattr(self, desc.update_all_attr)() for desc in ASSET_TYPES]
        for result in results:
            if isinstance(result, Err):
                return result

        return Ok(sum(result.value for result in results if isinstance(result, Ok)))
