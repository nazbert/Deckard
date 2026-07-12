"""
Regression test for issue #54 (StoreBackend.get_remote_file /
get_web_image):

  1. get_remote_file never forwarded `data_type` to the StoreCache calls,
     so BINARY fetches (data_type="content", e.g. thumbnails) were keyed
     under the default "text" cache string. A text fetch and a binary
     fetch of the same repo/path collided on one cache file -- opened with
     conflicting modes ("r" vs "rb") -- and the index could never tell the
     two apart. Now the cache key carries the data type end to end
     (is_cached / open_cache_file / get_fetched_date / the write).
  2. get_web_image wrapped the fetch in a bare `except:`, which also
     swallowed asyncio.CancelledError -- a cancelled store load's image
     tasks reported "no image" instead of cancelling. Cancellation must
     propagate; ordinary decode/fetch errors must still be contained.

All network-free: request_from_url is stubbed.
"""
import asyncio

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.StoreCache import StoreCache

REPO = "https://github.com/StreamController/StreamController-Store"


def make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    sb.store_cache = StoreCache()
    return sb


def test_binary_fetch_cached_under_content_key() -> None:
    sb = make_backend()

    class Resp:
        text = "binary-payload"
        content = b"binary-payload"

    async def fetch(url):
        return Resp()

    sb.request_from_url = fetch
    result = asyncio.run(sb.get_remote_file(REPO, "thumb.png", "main", data_type="content"))
    assert result == b"binary-payload"

    assert sb.store_cache.is_cached(url=REPO, path="thumb.png", data_type="content"), (
        "a binary fetch must be cached under its content key"
    )
    assert not sb.store_cache.is_cached(url=REPO, path="thumb.png", data_type="text"), (
        "a binary fetch must NOT be cached under the text key"
    )

    # And the cached copy must be served back under the same key with no
    # second fetch.
    async def must_not_fetch(url):
        raise AssertionError("cached binary fetch must not hit the network")

    sb.request_from_url = must_not_fetch
    again = asyncio.run(sb.get_remote_file(REPO, "thumb.png", "main", data_type="content"))
    assert again == b"binary-payload"


def test_text_and_binary_keys_do_not_collide() -> None:
    sb = make_backend()

    class Resp:
        text = "TEXT CONTENT"
        content = b"\x89BINARY\x00CONTENT"

    async def fetch(url):
        return Resp()

    sb.request_from_url = fetch
    text = asyncio.run(sb.get_remote_file(REPO, "same/path.dat", "main", data_type="text"))
    binary = asyncio.run(sb.get_remote_file(REPO, "same/path.dat", "main", data_type="content"))
    assert text == "TEXT CONTENT"
    assert binary == b"\x89BINARY\x00CONTENT"

    # Both cached, independently, under their own keys.
    async def must_not_fetch(url):
        raise AssertionError("both variants must be independently cached")

    sb.request_from_url = must_not_fetch
    assert asyncio.run(sb.get_remote_file(REPO, "same/path.dat", "main", data_type="text")) == "TEXT CONTENT"
    assert asyncio.run(sb.get_remote_file(REPO, "same/path.dat", "main", data_type="content")) == b"\x89BINARY\x00CONTENT"


def test_stale_fallback_respects_data_type() -> None:
    """The failed-fetch fallback must find the binary copy it itself wrote."""
    from src.backend.Store.StoreBackend import NoConnectionError

    sb = make_backend()

    class Resp:
        text = "img-bytes"
        content = b"img-bytes"

    async def fetch_ok(url):
        return Resp()

    async def fetch_fail(url):
        return NoConnectionError()

    sb.request_from_url = fetch_ok
    asyncio.run(sb.get_remote_file(REPO, "wall.png", "main", data_type="content", force_refetch=True))

    sb.request_from_url = fetch_fail
    fallback = asyncio.run(sb.get_remote_file(REPO, "wall.png", "main", data_type="content", force_refetch=True))
    assert fallback == b"img-bytes", (
        f"stale fallback must serve the binary copy under its content key, got {fallback!r}"
    )


def test_get_web_image_propagates_cancellation() -> None:
    sb = make_backend()

    async def hang_forever(url, path, branch="main", **kw):
        await asyncio.sleep(3600)

    sb.get_remote_file = hang_forever

    async def run_and_cancel():
        task = asyncio.ensure_future(sb.get_web_image(REPO, "thumb.png", "main"))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return f"swallowed: {task.result()!r}"

    outcome = asyncio.run(run_and_cancel())
    assert outcome == "cancelled", (
        f"get_web_image must let CancelledError propagate, got {outcome}"
    )


def test_get_web_image_still_contains_ordinary_errors() -> None:
    sb = make_backend()

    async def boom(url, path, branch="main", **kw):
        raise RuntimeError("simulated fetch explosion")

    sb.get_remote_file = boom
    assert asyncio.run(sb.get_web_image(REPO, "thumb.png", "main")) is None

    async def garbage(url, path, branch="main", **kw):
        return b"not an image"

    sb.get_remote_file = garbage
    assert asyncio.run(sb.get_web_image(REPO, "thumb.png", "main")) is None


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_remote_datatype")
    test_binary_fetch_cached_under_content_key()
    test_text_and_binary_keys_do_not_collide()
    test_stale_fallback_respects_data_type()
    test_get_web_image_propagates_cancellation()
    test_get_web_image_still_contains_ordinary_errors()
    print("scenario_store_remote_datatype: PASS")


if __name__ == "__main__":
    main()
