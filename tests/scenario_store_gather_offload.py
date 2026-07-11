"""
Regression test for gl#21 -- get_last_commit blocked the asyncio event loop
inside the catalog gather, exercised WITHOUT network:

get_last_commit issued a synchronous requests.get() on the loop thread;
prepare_plugin awaits it for every branch-pinned custom plugin inside
process_store_data's gather, so each call serialized the WHOLE page load
behind that request's latency (up to timeout=30 apiece) and evaded the
fetch-limiter semaphore that 782a1dac routed every other fetch through.
Its requests exceptions also raised straight through instead of returning
NoConnectionError, bypassing the error contract every sibling fetch obeys.

The contract is now: the blocking round-trip runs via asyncio.to_thread
under _fetch_limiter with timeout=30 (request_from_url's exact shape);
network failures return NoConnectionError; prepare_plugin and
download_repo isinstance-check it instead of interpolating an error
object (or None) into further URLs.
"""
import asyncio
import time

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

import requests

import src.backend.Store.StoreBackend as sb_module
from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError


class FakeResponse:
    status_code = 200

    def json(self):
        return [{"sha": "abc123"}]


def _make_backend() -> StoreBackend:
    import threading
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    sb.official_authors = []
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    return sb


def test_gathered_get_last_commit_calls_overlap() -> None:
    """5 x 0.3s commit lookups gathered must run in ~one round-trip, not
    serialize on the loop thread (fully serial would be >= 1.5s)."""
    def slow_get(url, timeout=30):
        time.sleep(0.3)
        return FakeResponse()

    real_get = sb_module.requests.get
    sb_module.requests.get = slow_get
    try:
        sb = _make_backend()

        async def burst():
            return await asyncio.gather(
                *(sb.get_last_commit(f"https://github.com/Example/Repo{i}", "main")
                  for i in range(5))
            )

        start = time.monotonic()
        results = asyncio.run(burst())
        elapsed = time.monotonic() - start
    finally:
        sb_module.requests.get = real_get

    assert results == ["abc123"] * 5, f"expected 5 resolved shas, got {results!r}"
    assert elapsed < 1.0, (
        f"5 x 0.3s commit lookups took {elapsed:.2f}s -- the blocking fetch "
        f"is serializing the event loop (fully serial would be >=1.5s)"
    )


def test_loop_stays_responsive_during_lookup() -> None:
    """While one commit lookup blocks in its round-trip, other coroutines on
    the same loop must keep running -- this is exactly what the catalog
    gather needs from every prepare_* sibling."""
    def slow_get(url, timeout=30):
        time.sleep(0.4)
        return FakeResponse()

    real_get = sb_module.requests.get
    sb_module.requests.get = slow_get
    try:
        sb = _make_backend()

        async def run() -> int:
            fetch = asyncio.ensure_future(
                sb.get_last_commit("https://github.com/Example/Repo", "main")
            )
            ticks = 0
            while not fetch.done():
                await asyncio.sleep(0.02)
                ticks += 1
            await fetch
            return ticks

        ticks = asyncio.run(run())
    finally:
        sb_module.requests.get = real_get

    assert ticks >= 5, (
        f"the event loop only ticked {ticks} times during a 0.4s lookup -- "
        f"the blocking fetch is stalling every sibling coroutine"
    )


def test_network_failure_returns_no_connection_error() -> None:
    """A requests exception must come back as NoConnectionError (the
    contract every sibling fetch obeys), and prepare_plugin must propagate
    it instead of raising out of the gather or fetching a manifest for an
    unresolved commit."""
    def failing_get(url, timeout=30):
        raise requests.exceptions.ConnectionError("boom: no route to host")

    real_get = sb_module.requests.get
    sb_module.requests.get = failing_get
    try:
        sb = _make_backend()

        result = asyncio.run(sb.get_last_commit("https://github.com/Example/Repo", "main"))
        assert isinstance(result, NoConnectionError), (
            f"a network failure must return NoConnectionError, got {result!r}"
        )

        async def manifest_must_not_be_called(url, commit):
            raise AssertionError(
                "prepare_plugin must not fetch a manifest when the branch's "
                "commit could not be resolved"
            )

        sb.get_manifest = manifest_must_not_be_called
        plugin = {"url": "https://github.com/Example/TestPlugin", "branch": "main"}
        result = asyncio.run(sb.prepare_plugin(plugin))
        assert isinstance(result, NoConnectionError), (
            f"prepare_plugin must propagate the NoConnectionError, got {result!r}"
        )
    finally:
        sb_module.requests.get = real_get


def test_download_repo_guards_unresolved_sha() -> None:
    """download_repo's branch path resolves the sha through get_last_commit;
    a NoConnectionError or None sha must fail the download up front instead
    of interpolating the object into the archive URL."""
    def get_must_not_be_called(*args, **kwargs):
        raise AssertionError("no archive fetch may be attempted for an unresolved sha")

    real_get = sb_module.requests.get
    real_is_flatpak = sb_module.is_flatpak
    sb_module.requests.get = get_must_not_be_called
    sb_module.is_flatpak = lambda: True  # force the zip path (argv has --devel)
    try:
        sb = _make_backend()

        async def last_commit_nce(repo_url, branch_name="main"):
            return NoConnectionError()

        async def last_commit_none(repo_url, branch_name="main"):
            return None

        sb.get_last_commit = last_commit_nce
        result = asyncio.run(sb.download_repo(
            "https://github.com/Example/Repo", "/nonexistent/target", branch_name="main"
        ))
        assert isinstance(result, NoConnectionError), (
            f"an unresolvable sha (offline) must propagate, got {result!r}"
        )

        sb.get_last_commit = last_commit_none
        result = asyncio.run(sb.download_repo(
            "https://github.com/Example/Repo", "/nonexistent/target", branch_name="main"
        ))
        assert result == 404, (
            f"a branch with no commits must be a hard 404, got {result!r}"
        )
    finally:
        sb_module.requests.get = real_get
        sb_module.is_flatpak = real_is_flatpak


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_gather_offload")
    test_gathered_get_last_commit_calls_overlap()
    test_loop_stays_responsive_during_lookup()
    test_network_failure_returns_no_connection_error()
    test_download_repo_guards_unresolved_sha()
    print("scenario_store_gather_offload: PASS")


if __name__ == "__main__":
    main()
