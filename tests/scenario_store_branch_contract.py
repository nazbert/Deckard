"""
Regression test for gl#24 -- two ways the store tab froze or built garbage
URLs, exercised WITHOUT network:

1. get_official_store_branch returned the NoConnectionError INSTANCE when
   versions.json couldn't be fetched (and the cache was too stale);
   get_stores appended it verbatim to its (url, branch) tuples and build_url
   interpolated the object's repr into request URLs and cache keys. Now the
   branch contract is str-only, falling back to STORE_BRANCH (uncached, so a
   later successful fetch still corrects it).

2. json.loads(versions_file) was unguarded: a truncated cached versions.json
   (served by 782a1dac's stale-cache fallback) raised JSONDecodeError through
   the page's load(), the @log.catch swallowed it with the spinner still up,
   and StorePage._loaded=True blocked any retry until the window was
   recreated. Now the parse is guarded AND StorePage re-arms itself
   (_loaded=False + error page) whenever a load fails.
"""
import asyncio
import time

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    sb.official_store_branch_cache = None
    return sb


async def _fetch_fail(url):
    return NoConnectionError()


def test_branch_is_str_when_offline_and_uncached() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()
    sb.request_from_url = _fetch_fail

    branch = asyncio.run(sb.get_official_store_branch())
    assert isinstance(branch, str) and branch, (
        f"offline+uncached must fall back to a str branch, got {branch!r}"
    )
    assert sb.official_store_branch_cache is None, (
        "the fallback branch must not be cached -- a later successful fetch "
        "has to be able to correct it"
    )

    stores = asyncio.run(sb.get_stores())
    for url, b in stores:
        assert isinstance(b, str) and b, f"get_stores yielded non-str branch {b!r} for {url}"


def test_branch_survives_truncated_cached_versions_json() -> None:
    fixtures.install_stub_globals()
    sb = _make_backend()

    # Seed the cache with a TRUNCATED versions.json (what a crash mid-write
    # used to leave behind, see gl#25), stamped fresh...
    with sb.store_cache.open_cache_file(
        url=StoreBackend.STORE_REPO_URL, branch="versions", path="versions.json", mode="w"
    ) as f:
        f.write('{"1.5.0-beta')  # deliberately truncated/invalid JSON
    # ...then fail the live fetch so the stale fallback serves it.
    sb.request_from_url = _fetch_fail

    branch = asyncio.run(sb.get_official_store_branch())
    assert isinstance(branch, str) and branch, (
        f"truncated cached versions.json must not break the branch contract, got {branch!r}"
    )


def test_custom_store_entries_are_sanitized() -> None:
    fixtures.install_stub_globals(app_settings={
        "store": {
            "enable-custom-stores": True,
            "custom-stores": [
                {"url": "https://github.com/someone/store", "branch": None},
                {"url": None, "branch": "main"},  # must be skipped entirely
                {"url": "https://github.com/other/store", "branch": "1.5.0"},
            ],
        },
    })
    sb = _make_backend()
    sb.request_from_url = _fetch_fail

    stores = asyncio.run(sb.get_stores())
    urls = [u for u, _ in stores]
    assert None not in urls, f"url-less custom store must be skipped, got {stores}"
    for url, b in stores:
        assert isinstance(b, str) and b, f"get_stores yielded non-str branch {b!r} for {url}"


def test_store_page_rearms_after_failed_load() -> None:
    """Drives the REAL StorePage.ensure_loaded/_load_guarded/
    show_connection_error methods on a duck-typed stand-in (no GTK widget
    construction in this headless harness)."""
    from src.windows.Store.StorePage import StorePage
    from gi.repository import GLib

    class FakePage:
        ensure_loaded = StorePage.ensure_loaded
        _load_guarded = StorePage._load_guarded
        show_connection_error = StorePage.show_connection_error

        def __init__(self):
            self._loaded = False
            self.load_calls = 0
            self.fail = True
            self.visible_child = None
            self.no_connection_page = object()

        def load(self):
            self.load_calls += 1
            if self.fail:
                raise RuntimeError("boom: simulated JSONDecodeError-style load failure")

        def set_visible_child(self, child):
            self.visible_child = child

    page = FakePage()

    # 1. Failing load: must re-arm (_loaded False) and show the error page.
    page.ensure_loaded()
    assert fixtures.wait_until(lambda: page.load_calls == 1 and not page._loaded), (
        "failed load must reset _loaded so the tab can retry"
    )
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 3.0
    while page.visible_child is None and time.monotonic() < deadline:
        ctx.iteration(False)
    assert page.visible_child is page.no_connection_page, (
        "failed load must land on the error page"
    )

    # 2. Retry after the failure: ensure_loaded must actually load again.
    page.fail = False
    page.ensure_loaded()
    assert fixtures.wait_until(lambda: page.load_calls == 2), (
        "revisiting the tab after a failure must retry the load"
    )
    assert page._loaded is True

    # 3. And a loaded tab stays a no-op.
    page.ensure_loaded()
    time.sleep(0.1)
    assert page.load_calls == 2, "an already-loaded tab must not reload"


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_branch_contract")
    test_branch_is_str_when_offline_and_uncached()
    test_branch_survives_truncated_cached_versions_json()
    test_custom_store_entries_are_sanitized()
    test_store_page_rearms_after_failed_load()
    print("scenario_store_branch_contract: PASS")


if __name__ == "__main__":
    main()
