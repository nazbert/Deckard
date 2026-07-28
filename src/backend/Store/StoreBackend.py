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
from concurrent.futures import ThreadPoolExecutor
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
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.backend import http_client

# Import signals
from src.Signals import Signals

# Import globals
import globals as gl
from src.windows.Store.StoreData import PluginData, IconData, SDPlusBarWallpaperData, WallpaperData


class NoConnectionError:
    # Falsy so callers can treat any error result as a failed operation.
    def __bool__(self) -> bool:
        return False

class StoreBackend:
    STORE_REPO_URL = "https://github.com/StreamController/StreamController-Store" #"https://github.com/StreamController/StreamController-Store"
    STORE_CACHE_PATH = "Store/cache"
    # STORE_CACHE_PATH = os.path.join(gl.DATA_PATH, STORE_CACHE_PATH)
    STORE_BRANCH = "1.5.0"

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
    def is_safe_asset_id(cls, asset_id) -> bool:
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

    @classmethod
    def is_safe_commit_sha(cls, commit_sha) -> bool:
        return isinstance(commit_sha, str) and bool(cls.COMMIT_SHA_PATTERN.fullmatch(commit_sha))

    @classmethod
    def is_safe_ref_name(cls, ref_name) -> bool:
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
                plugins.append((plugin.get("url"), plugin.get("branch")))

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

    def request_from_url(self, url: str) -> requests.Response:
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

        Returns the sha str, None when the branch resolves to no commits
        (non-200 answer, empty/unparseable commit list), or NoConnectionError
        on a network failure -- the same contract as request_from_url, so
        callers isinstance-check instead of catching requests exceptions
        out of a gather.
        """
        url = f"https://api.github.com/repos/{self.get_user_name(repo_url)}/{self.get_repo_name(repo_url)}/commits?sha={branch_name}&per_page=1"

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
    
    def get_official_authors(self) -> list:
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

    def process_store_data(self, filename: str, process_func: callable, get_custom_func: callable, data_class, include_images=True):
        n_stores_with_errors = 0
        data_list = []

        stores = self.get_stores()

        for url, branch in stores:
            store_file_json, n_stores_with_errors = self.fetch_and_parse_store_json(url, filename, branch, n_stores_with_errors)
            if store_file_json is not None:
                data_list.extend(store_file_json)

        if n_stores_with_errors >= len(stores):
            return NoConnectionError()

        futures = [self._prepare_pool.submit(process_func, data, include_images, True) for data in data_list]

        if get_custom_func is not None:
            for url, branch in get_custom_func():
                asset = {
                    "url": url,
                    "branch": branch
                }
                futures.append(self._prepare_pool.submit(process_func, asset, include_images, False))

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

    def get_all_plugins(self, include_images: bool = True) -> list[PluginData]:
        return self.process_store_data(self.PLUGIN_FILE, self.prepare_plugin, self.get_custom_plugins, PluginData, include_images)

    def get_all_icons(self) -> int:
        return self.process_store_data(self.ICON_FILE, self.prepare_icon, None, IconData)

    def get_all_wallpapers(self) -> int:
        return self.process_store_data(self.WALLPAPERS_FILE, self.prepare_wallpaper, None, WallpaperData)
    
    def get_all_sd_plus_bar_wallpapers(self) -> int:
        return self.process_store_data(self.SDPLUSWALLPAPERS_FILE, self.prepare_sd_plus_bar_wallpaper, None, SDPlusBarWallpaperData)
    
    def get_manifest(self, url:str, commit:str) -> dict:
        # url = self.build_url(url, "manifest.json", commit)
        manifest = self.get_remote_file(url, "manifest.json", commit)
        if isinstance(manifest, NoConnectionError):
            return manifest
        if manifest is None:
            return
        return json.loads(manifest)

    def get_attribution(self, url:str, commit:str) -> dict:
        result = self.get_remote_file(url, "attribution.json", commit)
        if isinstance(result, NoConnectionError):
            return {}
        
        try:
            return json.loads(result)
        except (json.decoder.JSONDecodeError, TypeError) as e:
            return {}

    def prepare_plugin(self, plugin, include_image: bool = True, verified: bool = False):
        url = plugin["url"]

        # Check if suitable version is available
        compatible = True
        commit: str = None
        if "commits" in plugin:
            version = self.get_newest_compatible_version(plugin["commits"])
            if version is None:
                compatible = False
                version = self.get_newest_version(list(plugin["commits"].keys()))
                if version is None:
                    return NoCompatibleVersion #TODO
            commit = plugin["commits"][version]

        branch = plugin.get("branch")
        if branch is not None:
            commit = self.get_last_commit(url, branch)
            if isinstance(commit, NoConnectionError):
                # NoConnectionError is falsy: letting it fall through would
                # fetch the manifest at `branch` but store the error object
                # as the entry's commit_sha (poisoning sha comparisons).
                log.error(f"Could not resolve the last commit of {url}@{branch} due to NoConnectionError")
                return commit

        manifest = self.get_manifest(url, commit or branch)
        if isinstance(manifest, NoConnectionError):
            log.error(f"manifest failed to load due to NoConnectionError for repository {url}")
            return manifest
        if not manifest:
            log.error(f"manifest failed to load for repository {url}")
            return

        image = None
        thumbnail_path = manifest.get("thumbnail")
        if include_image:
            image = self.get_web_image(url, thumbnail_path, commit or branch)
            if isinstance(image, NoConnectionError):
                # A missing/rate-limited thumbnail must not drop the plugin --
                # list it without an image.
                image = None
        
        attribution = self.get_attribution(url, commit or branch)
        if isinstance(attribution, NoConnectionError):
            return attribution
        attribution = attribution.get("generic", {}) #TODO: Choose correct attribution

        stargazers = self.get_stargazers(url)

        author = self.get_user_name(url)

        translated_description = gl.lm.get_custom_translation(manifest.get("descriptions", {}))
        translated_short_description = gl.lm.get_custom_translation(manifest.get("short-descriptions", {}))

        return PluginData(
            descriptions=manifest.get("descriptions") or None,
            short_descriptions=manifest.get("short-descriptions") or None,
            description=translated_description or manifest.get("description"),
            short_description=translated_short_description or manifest.get("short-description"),

            github=url or None,
            author=author or None, # Formerly: user_name
            official=author in self.official_authors or False,
            commit_sha=commit,
            branch=branch,
            local_sha=self.get_local_sha_for_id(gl.PLUGIN_DIR, manifest.get("id")),
            minimum_app_version=manifest.get("minimum-app-version") or None,
            app_version=manifest.get("app-version") or None,
            repository_name=self.get_repo_name(url),
            tags=manifest.get("tags") or None,

            thumbnail=thumbnail_path or None,
            image=image or None,

            copyright=attribution.get("copyright") or None,
            original_url=attribution.get("original-url") or None,
            license=attribution.get("licence") or None,
            license_descriptions=attribution.get("licence-descriptions", attribution.get("descriptions")) or None,

            plugin_name=manifest.get("name") or None,
            plugin_version=manifest.get("version") or None,
            plugin_id=manifest.get("id") or None,

            is_compatible=compatible,
            verified=verified
        )
    
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
    
    def get_local_sha_for_id(self, base_dir: str, asset_id) -> str:
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
        if not include_image:
            raise NotImplementedError("Not yet implemented") #TODO
        if "url" not in icon:
            return None

        url = icon["url"]

        # Check if suitable version is available
        compatible = True
        version = self.get_newest_compatible_version(icon["commits"])
        if version is None:
            compatible = False
            version = self.get_newest_version(list(icon["commits"].keys()))
            if version is None:
                return NoCompatibleVersion
        commit = icon["commits"][version]

        manifest = self.get_manifest(url, commit)
        if isinstance(manifest, NoConnectionError):
            return manifest
        attribution = self.get_attribution(url, commit)
        if isinstance(attribution, NoConnectionError):
            return attribution
        attribution = attribution.get("generic", {}) #TODO: Choose correct attribution

        thumbnail_path = manifest.get("thumbnail")
        image = self.get_web_image(url, thumbnail_path, commit)
        if isinstance(image, NoConnectionError):
            # A missing/rate-limited thumbnail must not drop the pack from
            # the catalog -- list it without an image, like prepare_plugin.
            image = None

        author = self.get_user_name(url)

        stargazers = self.get_stargazers(url)
        if isinstance(stargazers, NoConnectionError):
            return stargazers
        
        translated_description = gl.lm.get_custom_translation(manifest.get("descriptions", {}))
        translated_short_description = gl.lm.get_custom_translation(manifest.get("short-descriptions", {}))

        return IconData(
            description=translated_description or manifest.get("description"),
            short_description=translated_short_description or manifest.get("short-description"),

            github=url or None,
            descriptions=manifest.get("descriptions") or None,
            short_descriptions=manifest.get("short-descriptions") or None,
            author=author or None,  # Formerly: user_name
            official=author in self.official_authors or False,
            commit_sha=commit,
            local_sha=self.get_local_sha_for_id(os.path.join(gl.DATA_PATH, "icons"), manifest.get("id")),
            minimum_app_version=manifest.get("minimum-app-version") or None,
            app_version=manifest.get("app-version") or None,
            repository_name=self.get_repo_name(url),
            tags=manifest.get("tags") or None,

            thumbnail=thumbnail_path or None,
            image=image or None,

            copyright=attribution.get("copyright") or None,
            original_url=attribution.get("original-url") or None,
            license=attribution.get("licence") or None,
            license_descriptions=attribution.get("licence-descriptions", attribution.get("descriptions")) or None,

            icon_name=manifest.get("name") or None,
            icon_version=manifest.get("version") or None,
            icon_id=manifest.get("id") or None,

            is_compatible=compatible,
            verified=verified
        )

    
    def prepare_wallpaper(self, wallpaper, include_image: bool = True, verified: bool = False):
        if not include_image:
            raise NotImplementedError("Not yet implemented") #TODO
        if "url" not in wallpaper:
            return None

        url = wallpaper["url"]

        # Check if suitable version is available
        compatible = True
        version = self.get_newest_compatible_version(wallpaper["commits"])
        if version is None:
            compatible = False
            version = self.get_newest_version(list(wallpaper["commits"].keys()))
            if version is None:
                return NoCompatibleVersion
        commit = wallpaper["commits"][version]

        manifest = self.get_manifest(url, commit)
        if isinstance(manifest, NoConnectionError):
            return manifest

        thumbnail_path = manifest.get("thumbnail")
        image = self.get_web_image(url, thumbnail_path, commit)
        if isinstance(image, NoConnectionError):
            # A missing/rate-limited thumbnail must not drop the wallpaper
            # from the catalog -- list it without an image, like
            # prepare_plugin.
            image = None
        attribution = self.get_attribution(url, commit)
        if isinstance(attribution, NoConnectionError):
            return attribution
        attribution = attribution.get("generic", {}) #TODO: Choose correct attribution

        author = self.get_user_name(url)

        translated_description = gl.lm.get_custom_translation(manifest.get("descriptions", {}))
        translated_short_description = gl.lm.get_custom_translation(manifest.get("short-descriptions", {}))

        return WallpaperData(
            description=translated_description or manifest.get("description"),
            short_description=translated_short_description or manifest.get("short-description"),

            github=url or None,
            descriptions=manifest.get("descriptions") or None,
            short_descriptions=manifest.get("short-descriptions") or None,
            author=author or None,  # Formerly: user_name
            official=author in self.official_authors or False,
            commit_sha=commit,
            local_sha=self.get_local_sha_for_id(os.path.join(gl.DATA_PATH, "wallpapers"), manifest.get("id")),
            minimum_app_version=manifest.get("minimum-app-version") or None,
            app_version=manifest.get("app-version") or None,
            repository_name=self.get_repo_name(url),
            tags=manifest.get("tags") or None,

            thumbnail=thumbnail_path or None,
            image=image or None,

            copyright=attribution.get("copyright") or None,
            original_url=attribution.get("original-url") or None,
            license=attribution.get("licence") or None,
            license_descriptions=attribution.get("licence-descriptions", attribution.get("descriptions")) or None,

            wallpaper_name=manifest.get("name") or None,
            wallpaper_version=manifest.get("version") or None,
            wallpaper_id=manifest.get("id") or None,

            is_compatible=compatible,
            verified=verified
        )

    def prepare_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper, include_image: bool = True, verified: bool = False):
        if not include_image:
            raise NotImplementedError("Not yet implemented") #TODO
        if "url" not in sd_plus_bar_wallpaper:
            return None

        url = sd_plus_bar_wallpaper["url"]
        
        compatible = True
        version = self.get_newest_compatible_version(sd_plus_bar_wallpaper["commits"])
        if version is None:
            compatible = False
            version = self.get_newest_version(list(sd_plus_bar_wallpaper["commits"].keys()))
            if version is None:
                return NoCompatibleVersion
        commit = sd_plus_bar_wallpaper["commits"][version]
        
        manifest = self.get_manifest(url, commit)
        if isinstance(manifest, NoConnectionError):
            return manifest
        
        thumbnail_path = manifest.get("thumbnail")
        image = self.get_web_image(url, thumbnail_path, commit)
        if isinstance(image, NoConnectionError):
            # A missing/rate-limited thumbnail must not drop the SD+ bar
            # wallpaper from the catalog -- list it without an image, like
            # prepare_plugin.
            image = None
        attribution = self.get_attribution(url, commit)
        if isinstance(attribution, NoConnectionError):
            return attribution
        attribution = attribution.get("generic", {}) #TODO: Choose correct attribution

        author = self.get_user_name(url)

        translated_description = gl.lm.get_custom_translation(manifest.get("descriptions", {}))
        translated_short_description = gl.lm.get_custom_translation(manifest.get("short-descriptions", {}))

        return SDPlusBarWallpaperData(
            description=translated_description or manifest.get("description"),
            short_description=translated_short_description or manifest.get("short-description"),

            github=url or None,
            descriptions=manifest.get("descriptions") or None,
            short_descriptions=manifest.get("short-descriptions") or None,
            author=author or None,  # Formerly: user_name
            official=author in self.official_authors or False,
            commit_sha=commit,
            local_sha=self.get_local_sha_for_id(os.path.join(gl.DATA_PATH, "sd_plus_bar_wallpapers"), manifest.get("id")),
            minimum_app_version=manifest.get("minimum-app-version") or None,
            app_version=manifest.get("app-version") or None,
            repository_name=self.get_repo_name(url),
            tags=manifest.get("tags") or None,

            thumbnail=thumbnail_path or None,
            image=image or None,

            copyright=attribution.get("copyright") or None,
            original_url=attribution.get("original-url") or None,
            license=attribution.get("licence") or None,
            license_descriptions=attribution.get("licence-descriptions", attribution.get("descriptions")) or None,

            name=manifest.get("name") or None,
            version=manifest.get("version") or None,
            id=manifest.get("id") or None,    

            is_compatible=compatible,
            verified=verified
        )

    def get_web_image(self, url: str, path: str, branch: str = "main") -> Image:
        # `except Exception` so a pool worker still honours SystemExit and
        # KeyboardInterrupt.
        try:
            result = self.get_remote_file(url, path, branch, data_type="content")
        except Exception as e:
            log.error(f"Failed to fetch image {path} from {url}: {e}")
            return
        if isinstance(result, NoConnectionError):
            return result
        try:
            return Image.open(BytesIO(result))
        except Exception as e:
            log.warning(f"Could not decode image {path} from {url}: {e}")
            return
    
    def get_stargazers(self, repo_url: str) -> int:
        "Deactivated for now because of rate limits"
        return 0

    def get_user_name(self, repo_url:str) -> str:
        splitted =  repo_url.split("/")
        return splitted[splitted.index("github.com")+1]
    
    def get_repo_name(self, repo_url:str) -> str:
        github_split = repo_url.split("github")
        if len(github_split) < 2:
            return
        split = github_split[1].split("/")
        if len(split) < 3:
            return
        return split[2]
    
    def get_newest_compatible_version(self, available_versions: list[str]) -> str:
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

    def get_main_folder_of_zip(self, zip_path: str) -> str:
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

    def _staged_tree_id_matches(self, staging_tree: str, expected_id: str) -> bool:
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


        username = self.get_user_name(repo_url)
        projectname = self.get_repo_name(repo_url).lower()
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
            # pull/reset/switch below now run in staging.
            # FIXME: Check if not already added
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(local_path)])
            self.subp_call(["git", "config", "--global", "--add", "safe.directory", os.path.abspath(staging)])

            # Run git pull to create .git/FETCH_HEAD. This allows us to check for available updates.
            # `git -C <dir>` (argv, no shell) instead of the old
            # `os.system("cd '<dir>' && git pull")` which built a shell command line.
            self.subp_call(["git", "-C", staging, "pull"])

            # Set repository to the given commit_sha
            if commit_sha is not None:
                self.subp_call(["git", "-C", staging, "reset", "--hard", commit_sha])
            elif branch_name is not None:
                self.subp_call(["git", "-C", staging, "switch", branch_name])

            # Same order as download_repo: validate the staged tree first,
            # then stamp VERSION, then swap.
            if not self._staged_tree_id_matches(staging, expected_id):
                return 400

            ## Write version
            with open(os.path.join(staging, "VERSION"), "w") as f:
                f.write(commit_sha or branch_name)

            self._swap_into_place(staging, local_path)
        except Exception as e:
            log.error(f"Failed to stage devel clone of {repo_url}: {e}")
            return NoConnectionError()
        finally:
            self._remove_leftover(staging)

        return 200

    def install_plugin(self, plugin_data:PluginData, auto_update: bool = False):
        url = plugin_data.github

        if not self.is_safe_asset_id(plugin_data.plugin_id):
            # The id names the install dir (which download_repo swap-replaces)
            # -- a traversal id like "../../.." must never reach that join.
            log.error(f"Refusing to install plugin with unsafe id {plugin_data.plugin_id!r} from {url}")
            return 400

        local_path = os.path.join(gl.PLUGIN_DIR, plugin_data.plugin_id)

        response = self.download_repo(repo_url=url, directory=local_path, commit_sha=plugin_data.commit_sha, branch_name=plugin_data.branch, expected_id=plugin_data.plugin_id)

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
        if gl.plugin_manager.get_plugin_by_id(plugin_data.plugin_id) is not None:
            try:
                self.uninstall_plugin(plugin_data.plugin_id, remove_from_pages=False, remove_files=False)
            except Exception as e:
                log.error(f"Deregistering the old version of {plugin_data.plugin_id} failed: {e}")

        # Run install script if present. Make sure to use python binary used to run this process to not break venv dependency installations.
        # List form without a shell: an f-string command both broke on spaces
        # in the data path and let crafted path components inject shell syntax.
        if os.path.isfile(os.path.join(local_path, "__install__.py")):
            subprocess.run([sys.executable, os.path.join(local_path, "__install__.py")], start_new_session=True)

        # Install requirements from requirements.txt
        if os.path.isfile(os.path.join(local_path, "requirements.txt")):
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", os.path.join(local_path, "requirements.txt")], start_new_session=True)

        # Update plugin manager
        gl.plugin_manager.load_plugins()
        gl.plugin_manager.init_plugins()
        gl.plugin_manager.generate_action_index()
        plugins = gl.plugin_manager.get_plugins()

        # A version-gated plugin "installs" fine (files on disk, True
        # returned, store button flips to installed) but lands in
        # disabled_plugins during the reload above -- and the only feedback
        # used to be the NEXT launch's disabled-plugins toast: in the install
        # session it silently never appeared, reading later as "my config
        # reset after restart" (the custom-repo half of #102). Say so now.
        self.notify_if_installed_disabled(plugin_data.plugin_id)

        # Update ui
        if recursive_hasattr(gl, "app.main_win.sidebar.action_chooser"):
            GLib.idle_add(gl.app.main_win.sidebar.action_chooser.plugin_group.update)

        ## Update page
        for controller in gl.deck_manager.deck_controller:
            ## Checks required to prevent errors after auto-update
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    # Load action objects
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

        # Notify plugin actions
        gl.signal_manager.trigger_signal(Signals.PluginInstall, plugin_data.plugin_id)

        log.success(f"Plugin {plugin_data.plugin_id} installed successfully under: {local_path} with sha: {plugin_data.commit_sha}")
        return True

    @staticmethod
    def notify_if_installed_disabled(plugin_id: str) -> bool:
        """If the plugin that was just installed got version-disabled by the
        register() gate (in disabled_plugins, not in plugins), tell the user
        immediately instead of leaving the first feedback to the next
        launch's startup toast (#102). Returns whether it notified."""
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

    def uninstall_plugin(self, plugin_id:str, remove_from_pages:bool = False, remove_files:bool = True) -> bool:
        ## 1. Remove all action objects in every cached page of every
        ## controller -- not just each controller's currently active page.
        ## A page that was previously visited and is still sitting in the
        ## page cache would otherwise keep dead plugin action objects alive
        ## with no teardown. Snapshot via the _pages_lock accessor: iterating
        ## gl.page_manager.pages directly raced load_page /
        ## discard_controller / clear_old_cached_pages mutating the dict
        ## from other threads.
        for page in gl.page_manager.all_cached_pages():
            page.remove_plugin_action_objects(plugin_id=plugin_id)
            if remove_from_pages:
                page.remove_plugin_actions_from_json(plugin_id=plugin_id)

        ## 2. Inform plugin base
        plugins = gl.plugin_manager.get_plugins()
        plugin = gl.plugin_manager.get_plugin_by_id(plugin_id)
        if plugin is None:
            return
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
        gl.plugin_manager.remove_plugin_from_list(plugin)

        gl.plugin_manager.generate_action_index()


        del plugin

        GLib.idle_add(gl.app.main_win.sidebar.action_chooser.plugin_group.update)
        GLib.idle_add(gl.app.main_win.sidebar.page_selector.update)


        base_module = f"plugins.{plugin_folder}"
        for module in sys.modules.copy():
            if module.startswith(base_module):
                del sys.modules[module]

        # for controller in gl.deck_manager.deck_controller:
            # controller.active_page.update_inputs_with_actions_from_plugin(plugin_id)

        ## Update page
        for controller in gl.deck_manager.deck_controller:
            ## Checks required to prevent errors after auto-update
            if hasattr(controller, "active_page"):
                if controller.active_page is not None:
                    # Load action objects
                    controller.active_page.load_action_objects()
                    controller.load_page(controller.active_page)

    # The (un)install pairs below all join a manifest-supplied id under a
    # data dir and rmtree/replace the result -- every one of them must reject
    # unsafe ids (icon/wallpaper packs are data-only; a traversal id would
    # hand them filesystem-wide delete with no code execution involved).

    def install_icon(self, icon_data:IconData):
        if not self.is_safe_asset_id(icon_data.icon_id):
            log.error(f"Refusing to install icon pack with unsafe id {icon_data.icon_id!r} from {icon_data.github}")
            return 400

        icon_path = os.path.join(gl.DATA_PATH, "icons", icon_data.icon_id)

        # No pre-delete (B-06): download_repo stages and validates the new
        # pack and only replaces the installed one at swap time -- the old
        # uninstall-first flow permanently lost the pack when the download
        # failed mid-update.
        return self.download_repo(repo_url=icon_data.github, directory=icon_path, commit_sha=icon_data.commit_sha, expected_id=icon_data.icon_id)

    def uninstall_icon(self, icon_data:IconData):
        folder_name = icon_data.icon_id
        if not self.is_safe_asset_id(folder_name):
            log.error(f"Refusing to uninstall icon pack with unsafe id {folder_name!r}")
            return 400
        if os.path.exists(os.path.join(gl.DATA_PATH, "icons", folder_name)):
            shutil.rmtree(os.path.join(gl.DATA_PATH, "icons", folder_name))

    def install_wallpaper(self, wallpaper_data:WallpaperData):
        if not self.is_safe_asset_id(wallpaper_data.wallpaper_id):
            log.error(f"Refusing to install wallpaper with unsafe id {wallpaper_data.wallpaper_id!r} from {wallpaper_data.github}")
            return 400

        wallpaper_path = os.path.join(gl.DATA_PATH, "wallpapers", wallpaper_data.wallpaper_id)

        # No pre-delete (B-06) -- see install_icon.
        return self.download_repo(repo_url=wallpaper_data.github, directory=wallpaper_path, commit_sha=wallpaper_data.commit_sha, expected_id=wallpaper_data.wallpaper_id)

    def uninstall_wallpaper(self, wallpaper_data:WallpaperData):
        folder_name = wallpaper_data.wallpaper_id
        if not self.is_safe_asset_id(folder_name):
            log.error(f"Refusing to uninstall wallpaper with unsafe id {folder_name!r}")
            return 400
        if os.path.exists(os.path.join(gl.DATA_PATH, "wallpapers", folder_name)):
            shutil.rmtree(os.path.join(gl.DATA_PATH, "wallpapers", folder_name))

    def install_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData):
        if not self.is_safe_asset_id(sd_plus_bar_wallpaper_data.id):
            log.error(f"Refusing to install SD+ bar wallpaper with unsafe id {sd_plus_bar_wallpaper_data.id!r} from {sd_plus_bar_wallpaper_data.github}")
            return 400

        wallpaper_path = os.path.join(gl.DATA_PATH, "sd_plus_bar_wallpapers", sd_plus_bar_wallpaper_data.id)

        # No pre-delete (B-06) -- see install_icon.
        return self.download_repo(repo_url=sd_plus_bar_wallpaper_data.github, directory=wallpaper_path, commit_sha=sd_plus_bar_wallpaper_data.commit_sha, expected_id=sd_plus_bar_wallpaper_data.id)

    def uninstall_sd_plus_bar_wallpaper(self, sd_plus_bar_wallpaper_data:SDPlusBarWallpaperData):
        folder_name = sd_plus_bar_wallpaper_data.id
        if not self.is_safe_asset_id(folder_name):
            log.error(f"Refusing to uninstall SD+ bar wallpaper with unsafe id {folder_name!r}")
            return 400
        if os.path.exists(os.path.join(gl.DATA_PATH, "sd_plus_bar_wallpapers", folder_name)):
            shutil.rmtree(os.path.join(gl.DATA_PATH, "sd_plus_bar_wallpapers", folder_name))

    def get_plugin_for_id(self, plugin_id):
        plugins = self.get_all_plugins()
        for plugin in plugins:
            if plugin.plugin_id == plugin_id:
                return plugin
            
    ## Updates
    def get_plugins_to_update(self):
        plugins =  self.get_all_plugins()
        if isinstance(plugins, NoConnectionError):
            return plugins

        plugins_to_update: list[PluginData] = []

        for plugin in plugins:
            if plugin.local_sha is None:
                # Plugin is not installed
                continue
            if plugin.local_sha == plugin.commit_sha:
                # Up to date
                continue
            if plugin.commit_sha is None:
                # Unresolved remote tip (branch-pinned plugin whose
                # get_last_commit returned None -- 429/empty). There is no
                # known sha to update to; auto-updating would only hard-404.
                continue
            if plugin.is_compatible is False:
                # When no compatible version exists, prepare_plugin pins the
                # newest INCOMPATIBLE commit (so the store can still list the
                # entry). Auto-updating onto it would replace a working
                # plugin with a build for a different app major -- skip and
                # report instead. PluginPreview.get_install_state_for makes
                # the same call for the store UI's update button.
                log.warning(
                    f"Skipping update of plugin {plugin.plugin_id}: pinned version "
                    f"{plugin.commit_sha} is not compatible with app version {gl.app_version}"
                )
                continue
            plugins_to_update.append(plugin)

        return plugins_to_update
    
    def update_all_plugins(self) -> int:
        """
        Returns number of SUCCESSFULLY updated plugins
        """
        plugins_to_update = self.get_plugins_to_update()
        if isinstance(plugins_to_update, NoConnectionError):
            return plugins_to_update
        n_updated = 0
        for plugin in plugins_to_update:
            # install_plugin deregisters the old version only after its
            # download succeeded -- a failed update leaves the old version
            # on disk AND registered, so no recovery pass is needed.
            result = self.install_plugin(plugin)
            if result is True:
                n_updated += 1
            else:
                log.error(f"Failed to update plugin {plugin.plugin_id}: {result!r}")

        return n_updated

    def get_icons_to_update(self):
        icons = self.get_all_icons()
        if isinstance(icons, NoConnectionError):
            return icons

        icons_to_update: list[IconData] = []

        for icon in icons:
            if icon.local_sha is None:
                # Icon pack is not installed
                continue
            if icon.local_sha == icon.commit_sha:
                # Up to date
                continue
            if icon.is_compatible is False:
                # prepare_icon pins the newest INCOMPATIBLE commit when no
                # compatible version exists (so the store can still list it).
                # Auto-updating onto it would replace a working pack with a
                # build for a different app major -- skip and report, exactly
                # like the plugin update path.
                log.warning(
                    f"Skipping update of icon pack {icon.icon_id}: pinned version "
                    f"{icon.commit_sha} is not compatible with app version {gl.app_version}"
                )
                continue
            icons_to_update.append(icon)

        return icons_to_update
    
    def update_all_icons(self) -> int:
        """
        Returns number of SUCCESSFULLY updated icons
        """
        icons_to_update = self.get_icons_to_update()
        if isinstance(icons_to_update, NoConnectionError):
            return icons_to_update
        n_updated = 0
        for icon in icons_to_update:
            result = self.install_icon(icon)
            if result == 200:
                n_updated += 1
            else:
                log.error(f"Failed to update icon pack {icon.icon_id}: {result!r}")

        return n_updated
    
    def get_wallpapers_to_update(self):
        wallpapers = self.get_all_wallpapers()
        if isinstance(wallpapers, NoConnectionError):
            return wallpapers

        wallpapers_to_update: list[WallpaperData] = []

        for wallpaper in wallpapers:
            if wallpaper.local_sha is None:
                # Wallpaper is not installed
                continue
            if wallpaper.local_sha == wallpaper.commit_sha:
                # Up to date
                continue
            if wallpaper.is_compatible is False:
                # prepare_wallpaper pins the newest INCOMPATIBLE commit when
                # no compatible version exists (so the store can still list
                # it). Auto-updating onto it would replace a working pack
                # with a build for a different app major -- skip and report,
                # exactly like the plugin update path.
                log.warning(
                    f"Skipping update of wallpaper {wallpaper.wallpaper_id}: pinned version "
                    f"{wallpaper.commit_sha} is not compatible with app version {gl.app_version}"
                )
                continue
            wallpapers_to_update.append(wallpaper)

        return wallpapers_to_update

    def update_all_wallpapers(self) -> int:
        """
        Returns number of SUCCESSFULLY updated wallpapers
        """
        wallpapers_to_update = self.get_wallpapers_to_update()
        if isinstance(wallpapers_to_update, NoConnectionError):
            return wallpapers_to_update
        n_updated = 0
        for wallpaper in wallpapers_to_update:
            result = self.install_wallpaper(wallpaper)
            if result == 200:
                n_updated += 1
            else:
                log.error(f"Failed to update wallpaper {wallpaper.wallpaper_id}: {result!r}")

        return n_updated

    def get_sd_plus_bar_wallpapers_to_update(self):
        wallpapers = self.get_all_sd_plus_bar_wallpapers()
        if isinstance(wallpapers, NoConnectionError):
            return wallpapers

        wallpapers_to_update: list[SDPlusBarWallpaperData] = []

        for wallpaper in wallpapers:
            if wallpaper.local_sha is None:
                # Wallpaper is not installed
                continue
            if wallpaper.local_sha == wallpaper.commit_sha:
                # Up to date
                continue
            if wallpaper.is_compatible is False:
                # prepare_sd_plus_bar_wallpaper pins the newest INCOMPATIBLE
                # commit when no compatible version exists (so the store can
                # still list it). Auto-updating onto it would replace a
                # working pack with a build for a different app major -- skip
                # and report, exactly like the plugin update path.
                log.warning(
                    f"Skipping update of SD+ bar wallpaper {wallpaper.id}: pinned version "
                    f"{wallpaper.commit_sha} is not compatible with app version {gl.app_version}"
                )
                continue
            wallpapers_to_update.append(wallpaper)

        return wallpapers_to_update

    def update_all_sd_plus_bar_wallpapers(self) -> int:
        """
        Returns number of SUCCESSFULLY updated SD+ bar wallpapers
        """
        wallpapers_to_update = self.get_sd_plus_bar_wallpapers_to_update()
        if isinstance(wallpapers_to_update, NoConnectionError):
            return wallpapers_to_update
        n_updated = 0
        for wallpaper in wallpapers_to_update:
            result = self.install_sd_plus_bar_wallpaper(wallpaper)
            if result == 200:
                n_updated += 1
            else:
                log.error(f"Failed to update SD+ bar wallpaper {wallpaper.id}: {result!r}")

        return n_updated

    def update_everything(self) -> int:
        """
        Returns number of SUCCESSFULLY updated assets, or NoConnectionError
        """
        n_plugins = self.update_all_plugins()
        n_icons = self.update_all_icons()
        n_wallpapers = self.update_all_wallpapers()
        # SD+ bar wallpaper packs used to have NO update leg at all -- they
        # were installed once and never auto-updated.
        n_sd_plus = self.update_all_sd_plus_bar_wallpapers()

        # Every leg must be checked -- a NoConnectionError leaking into the
        # sum below used to raise TypeError (and n_wallpapers wasn't checked
        # at all).
        if any(isinstance(n, NoConnectionError) for n in (n_plugins, n_icons, n_wallpapers, n_sd_plus)):
            return NoConnectionError()

        return n_plugins + n_icons + n_wallpapers + n_sd_plus

class NoCompatibleVersion:
    pass
