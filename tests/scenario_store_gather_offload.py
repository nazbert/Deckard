"""
Regression test for get_last_commit on the shared store fetch path.

Catalog prepare tasks fan out on StoreBackend._prepare_pool, so per-entry
lookups overlap. get_last_commit runs under _fetch_limiter like every
sibling fetch, a network failure raises StoreFetchError, and download_repo
fails up front on an unresolved sha. No network is involved.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

import requests

import src.backend.Store.StoreBackend as sb_module
from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Err, ErrReason, StoreFetchError


class FakeResponse:
    status_code = 200

    def json(self):
        return [{"sha": "abc123"}]


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    sb.official_authors = []
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb._prepare_pool = ThreadPoolExecutor(
        max_workers=StoreBackend.MAX_CONCURRENT_REQUESTS,
        thread_name_prefix="store-prepare-test",
    )
    return sb


def test_catalog_fanout_overlaps() -> None:
    """Five 0.3s commit lookups through process_store_data's pool must run
    in about one round-trip. A serial run would take 1.5s or more."""
    def slow_get(url, timeout=30):
        time.sleep(0.3)
        return FakeResponse()

    real_get = sb_module.http_client.get
    sb_module.http_client.get = slow_get
    try:
        sb = _make_backend()
        sb.get_stores = lambda: [("https://github.com/Example/Store", "main")]
        sb.fetch_and_parse_store_json = (
            lambda url, filename, branch, n_errors=0:
            ([{"name": f"Repo{i}"} for i in range(5)], n_errors)
        )

        def prepare(entry, include_images, verified):
            return sb.get_last_commit(f"https://github.com/Example/{entry['name']}", "main")

        start = time.monotonic()
        results = sb.process_store_data("Plugins.json", prepare, None, str)
        elapsed = time.monotonic() - start
    finally:
        sb_module.http_client.get = real_get

    assert results == ["abc123"] * 5, f"expected 5 resolved shas, got {results!r}"
    assert elapsed < 1.0, (
        f"5 x 0.3s commit lookups took {elapsed:.2f}s -- the catalog fan-out "
        f"is serializing (fully serial would be >=1.5s)"
    )


def test_lookup_respects_fetch_limiter() -> None:
    """get_last_commit must take the shared fetch limiter, like every
    sibling fetch."""
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def tracking_get(url, timeout=30):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.1)
        with lock:
            in_flight -= 1
        return FakeResponse()

    real_get = sb_module.http_client.get
    sb_module.http_client.get = tracking_get
    try:
        sb = _make_backend()
        sb._fetch_limiter = threading.Semaphore(2)
        threads = [
            threading.Thread(
                target=sb.get_last_commit,
                args=(f"https://github.com/Example/Repo{i}", "main"),
            )
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sb_module.http_client.get = real_get

    assert peak <= 2, (
        f"{peak} lookups ran concurrently inside a limiter of 2 -- "
        f"get_last_commit is evading _fetch_limiter"
    )


def test_network_failure_raises_store_fetch_error() -> None:
    """A requests exception must come back as a StoreFetchError, and
    prepare_plugin must let it propagate without fetching a manifest for an
    unresolved commit, so the fan-out drops just that entry."""
    def failing_get(url, timeout=30):
        raise requests.exceptions.ConnectionError("boom: no route to host")

    real_get = sb_module.http_client.get
    sb_module.http_client.get = failing_get
    try:
        sb = _make_backend()

        try:
            sb.get_last_commit("https://github.com/Example/Repo", "main")
            raise AssertionError("a network failure must raise StoreFetchError")
        except StoreFetchError:
            pass

        def forbidden_manifest(url, commit):
            raise AssertionError(
                "prepare_plugin must not fetch a manifest when the branch's "
                "commit could not be resolved"
            )

        sb.get_manifest = forbidden_manifest
        plugin = {"url": "https://github.com/Example/TestPlugin", "branch": "main"}
        try:
            sb.prepare_plugin(plugin)
            raise AssertionError("prepare_plugin must propagate the StoreFetchError")
        except StoreFetchError:
            pass
    finally:
        sb_module.http_client.get = real_get


def test_download_repo_guards_unresolved_sha() -> None:
    """download_repo's branch path resolves the sha through get_last_commit.
    A raised StoreFetchError or a None sha must fail the download up front,
    rather than interpolate the object into the archive URL."""
    def forbidden_get(*args, **kwargs):
        raise AssertionError("no archive fetch may be attempted for an unresolved sha")

    real_get = sb_module.http_client.get
    real_is_flatpak = sb_module.is_flatpak
    sb_module.http_client.get = forbidden_get
    sb_module.is_flatpak = lambda: True  # force the zip path, since argv has --devel
    try:
        sb = _make_backend()

        def _raise_offline(repo_url, branch_name="main"):
            raise StoreFetchError(repo_url, "offline")
        sb.get_last_commit = _raise_offline
        result = sb.download_repo(
            "https://github.com/Example/Repo", "/nonexistent/target", branch_name="main"
        )
        assert isinstance(result, Err), (
            f"an unresolvable sha (offline) must propagate, got {result!r}"
        )

        sb.get_last_commit = lambda repo_url, branch_name="main": None
        result = sb.download_repo(
            "https://github.com/Example/Repo", "/nonexistent/target", branch_name="main"
        )
        assert isinstance(result, Err) and result.reason is ErrReason.INSTALL_FAILED, (
            f"a branch with no commits must be a hard install failure, got {result!r}"
        )
    finally:
        sb_module.http_client.get = real_get
        sb_module.is_flatpak = real_is_flatpak


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_gather_offload")
    test_catalog_fanout_overlaps()
    test_lookup_respects_fetch_limiter()
    test_network_failure_raises_store_fetch_error()
    test_download_repo_guards_unresolved_sha()
    print("scenario_store_gather_offload: PASS")


if __name__ == "__main__":
    main()
