"""
Regression test for "the store fails to load/display" -- the two backend
failure modes that blanked the page, exercised WITHOUT network:

1. process_store_data used to let ONE raising store entry kill the whole
   catalog; the exception then died in the page's @log.catch load(),
   leaving the spinner up forever. Now each prepare_* future is collected
   individually and filtered -- the healthy entries survive.

2. get_remote_file(force_refetch=True) had no fallback: a failed fetch
   (offline, or raw.githubusercontent 429 rate limiting -- observed live on
   2026-07-08) returned NoConnectionError even when a perfectly good cached
   copy existed, turning a throttled catalog fetch into an error page /
   silently dropped items. Now it serves the cached copy, bounded by the
   entry's FETCHED age (the "date" field is a last-use clock that every read
   renews, so it cannot bound staleness).

Also pinned here:
3. request_from_url calls must overlap up to the fetch limiter's cap --
   otherwise the catalog's prepare_* tasks serialize behind each
   request's latency (measured as a 30s+ store spinner on a cold cache).
4. prepare_plugin must list a plugin WITHOUT an image when only its
   thumbnail fetch fails.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError
from src.backend.Store.StoreCache import StoreCache
from src.backend.Store.store_result import StoreFetchError


class Item:
    """Stands in for PluginData -- process_store_data filters by data_class."""
    def __init__(self, name):
        self.name = name


def test_fanout_survives_poison_entry() -> None:
    sb = StoreBackend()

    def fake_get_stores():
        return [("https://example.invalid/store", "main")]

    def fake_fetch_and_parse(url, filename, branch, n_errors=0):
        return [{"name": "good-1"}, {"name": "poison"}, {"name": "good-2"}], n_errors

    def fake_prepare(entry, include_images=True, verified=False):
        if entry["name"] == "poison":
            raise RuntimeError("boom: one misconfigured store entry")
        return Item(entry["name"])

    sb.get_stores = fake_get_stores
    sb.fetch_and_parse_store_json = fake_fetch_and_parse

    results = sb.process_store_data("Plugins.json", fake_prepare, None, Item)
    assert not isinstance(results, NoConnectionError), "healthy entries must survive"
    names = sorted(item.name for item in results)
    assert names == ["good-1", "good-2"], (
        f"expected the two healthy entries to survive the poison one, got {names}"
    )


def test_remote_file_falls_back_to_cache() -> None:
    sb = StoreBackend()
    repo = "https://github.com/StreamController/StreamController-Store"

    def fetch_ok(url):
        class Resp:
            text = '[{"cached": "catalog"}]'
            content = text.encode()
        return Resp()

    def fetch_fail(url):
        return NoConnectionError()

    # Seed the cache through a successful force_refetch...
    sb.request_from_url = fetch_ok
    first = sb.get_remote_file(repo, "Plugins.json", "main", force_refetch=True)
    assert first == '[{"cached": "catalog"}]'

    # ...then fail every subsequent fetch (e.g. 429 rate limit).
    sb.request_from_url = fetch_fail
    second = sb.get_remote_file(repo, "Plugins.json", "main", force_refetch=True)
    assert second == '[{"cached": "catalog"}]', (
        f"failed refetch must serve the cached copy, got {second!r}"
    )

    # With no cached copy at all, the failure now propagates as a raise -- the
    # fetch layer raises, and get_remote_file re-raises only a genuine
    # no-cache failure (the stale-cache path above still returns bytes).
    try:
        sb.get_remote_file(repo, "Missing.json", "main", force_refetch=True)
        raise AssertionError("uncached failure must raise StoreFetchError")
    except StoreFetchError:
        pass


def test_fallback_respects_content_age() -> None:
    sb = StoreBackend()
    repo = "https://github.com/StreamController/StreamController-Store"

    def fetch_ok(url):
        class Resp:
            text = "fresh-content"
            content = text.encode()
        return Resp()

    def fetch_fail(url):
        return NoConnectionError()

    sb.request_from_url = fetch_ok
    first = sb.get_remote_file(repo, "AgeBound.json", "main", force_refetch=True)
    assert first == "fresh-content"

    key = sb.store_cache.generate_cache_string(repo, "AgeBound.json", "main", "text")
    fetched0 = sb.store_cache.files[key]["fetched"]
    date0 = sb.store_cache.files[key]["date"]
    time.sleep(0.05)

    # A plain cached read renews only the last-use clock, never the fetched one.
    cached = sb.get_remote_file(repo, "AgeBound.json", "main")
    assert cached == "fresh-content"
    assert sb.store_cache.files[key]["fetched"] == fetched0, (
        "a cached READ must not advance the fetched clock -- that would let "
        "repeated fallback reads renew an arbitrarily stale catalog forever"
    )
    assert sb.store_cache.files[key]["date"] >= date0

    # Failed refetch with fresh content: served from cache.
    sb.request_from_url = fetch_fail
    assert sb.get_remote_file(repo, "AgeBound.json", "main", force_refetch=True) == "fresh-content"

    # Failed refetch with content older than the bound: refused -- and a refusal
    # with no fresh cache is now a raise, not a returned sentinel.
    sb.store_cache.files[key]["fetched"] = time.time() - (StoreCache.DAYS_TO_KEEP * 24 * 3600 + 60)
    sb.store_cache.set_files(sb.store_cache.files)
    try:
        sb.get_remote_file(repo, "AgeBound.json", "main", force_refetch=True)
        raise AssertionError(
            f"content older than {StoreCache.DAYS_TO_KEEP} days must not be served"
        )
    except StoreFetchError:
        pass


def test_prepare_plugin_survives_failed_thumbnail() -> None:
    from src.windows.Store.StoreData import PluginData

    sb = StoreBackend()

    def fake_manifest(url, commit):
        return {"id": "test_plugin", "name": "Test", "thumbnail": "store/thumb.png",
                "version": "1.0", "descriptions": {}, "short-descriptions": {}}

    def fake_image(url, path, branch="main"):
        return NoConnectionError()

    def fake_attribution(url, commit):
        return {}

    def fake_last_commit(url, branch):
        return "abc123"

    sb.get_manifest = fake_manifest
    sb.get_web_image = fake_image
    sb.get_attribution = fake_attribution
    sb.get_last_commit = fake_last_commit
    gl.lm = SimpleNamespace(get_custom_translation=lambda d: None)

    plugin = {"url": "https://github.com/Example/TestPlugin", "branch": "main"}
    result = sb.prepare_plugin(plugin, include_image=True, verified=True)
    assert isinstance(result, PluginData), (
        f"a failed thumbnail fetch must not drop the plugin, got {result!r}"
    )
    assert result.image is None


def test_fetches_run_concurrently() -> None:
    import src.backend.Store.StoreBackend as sb_module

    class FakeResponse:
        status_code = 200
        content = b"x"
        text = "x"

        def close(self):
            pass

    def slow_get(url, stream=True, timeout=30):
        time.sleep(0.2)
        return FakeResponse()

    real_get = sb_module.http_client.get
    sb_module.http_client.get = slow_get
    try:
        sb = StoreBackend()

        with ThreadPoolExecutor(max_workers=10) as pool:
            start = time.monotonic()
            results = list(pool.map(
                lambda i: sb.request_from_url(f"https://example.invalid/{i}"), range(10)
            ))
            elapsed = time.monotonic() - start
    finally:
        sb_module.http_client.get = real_get

    assert all(not isinstance(r, NoConnectionError) for r in results)
    # Liveness/non-serialization ceiling, deliberately generous: what this
    # proves is that the 10 x 0.2s blocking fetches did NOT serialize behind
    # the limiter (fully serial would be >=2.0s). Any ceiling comfortably
    # under 2.0s preserves that proof; 1.8s buys headroom against a loaded
    # CI runner without ever admitting a serialized run (flake hardening).
    assert elapsed < 1.8, (
        f"10 x 0.2s fetches took {elapsed:.2f}s -- fetches are "
        f"serializing (fully serial would be >=2.0s)"
    )


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_store_resilience")
    test_fanout_survives_poison_entry()
    test_remote_file_falls_back_to_cache()
    test_fallback_respects_content_age()
    test_prepare_plugin_survives_failed_thumbnail()
    test_fetches_run_concurrently()
    print("scenario_store_resilience: PASS")


if __name__ == "__main__":
    main()
