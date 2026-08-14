"""
Regression test for StoreBackend.get_remote_file and get_web_image.

The cache key carries the data type end to end, so a text fetch and a binary
fetch of one repo path never collide on one cache file. get_web_image guards
with except Exception, so a KeyboardInterrupt escapes while an ordinary
decode or fetch error stays contained. request_from_url is stubbed.
"""

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl  # noqa: F401

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.StoreCache import StoreCache

REPO = "https://github.com/StreamController/StreamController-Store"


def make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    sb.store_cache = StoreCache()
    return sb


def test_binary_fetch_cached_under_content_key() -> None:
    sb = make_backend()

    class Resp:
        text = "binary-payload"
        content = b"binary-payload"

    def fetch(url):
        return Resp()

    sb.request_from_url = fetch
    result = sb.get_remote_file(REPO, "thumb.png", "main", data_type="content")
    assert result == b"binary-payload"

    assert sb.store_cache.is_cached(url=REPO, path="thumb.png", data_type="content"), (
        "a binary fetch must be cached under its content key"
    )
    assert not sb.store_cache.is_cached(url=REPO, path="thumb.png", data_type="text"), (
        "a binary fetch must NOT be cached under the text key"
    )

    # The cached copy must come back under the same key, with no second
    # fetch.
    def must_not_fetch(url):
        raise AssertionError("cached binary fetch must not hit the network")

    sb.request_from_url = must_not_fetch
    again = sb.get_remote_file(REPO, "thumb.png", "main", data_type="content")
    assert again == b"binary-payload"


def test_text_and_binary_keys_do_not_collide() -> None:
    sb = make_backend()

    class Resp:
        text = "TEXT CONTENT"
        content = b"\x89BINARY\x00CONTENT"

    def fetch(url):
        return Resp()

    sb.request_from_url = fetch
    text = sb.get_remote_file(REPO, "same/path.dat", "main", data_type="text")
    binary = sb.get_remote_file(REPO, "same/path.dat", "main", data_type="content")
    assert text == "TEXT CONTENT"
    assert binary == b"\x89BINARY\x00CONTENT"

    # Both cached, independently, under their own keys.
    def must_not_fetch(url):
        raise AssertionError("both variants must be independently cached")

    sb.request_from_url = must_not_fetch
    assert sb.get_remote_file(REPO, "same/path.dat", "main", data_type="text") == "TEXT CONTENT"
    assert sb.get_remote_file(REPO, "same/path.dat", "main", data_type="content") == b"\x89BINARY\x00CONTENT"


def test_stale_fallback_respects_data_type() -> None:
    """The failed-fetch fallback must find the binary copy it itself wrote."""
    from src.backend.Store.store_result import StoreFetchError

    sb = make_backend()

    class Resp:
        text = "img-bytes"
        content = b"img-bytes"

    def fetch_ok(url):
        return Resp()

    def fetch_fail(url):
        raise StoreFetchError(url, "429 rate limit")

    sb.request_from_url = fetch_ok
    sb.get_remote_file(REPO, "wall.png", "main", data_type="content", force_refetch=True)

    sb.request_from_url = fetch_fail
    fallback = sb.get_remote_file(REPO, "wall.png", "main", data_type="content", force_refetch=True)
    assert fallback == b"img-bytes", (
        f"stale fallback must serve the binary copy under its content key, got {fallback!r}"
    )


def test_get_web_image_propagates_base_exceptions() -> None:
    sb = make_backend()

    def interrupt(url, path, branch="main", **kw):
        raise KeyboardInterrupt()

    sb.get_remote_file = interrupt
    try:
        sb.get_web_image(REPO, "thumb.png", "main")
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError(
            "get_web_image must let a BaseException propagate, not report 'no image'"
        )


def test_get_web_image_still_contains_ordinary_errors() -> None:
    sb = make_backend()

    def boom(url, path, branch="main", **kw):
        raise RuntimeError("simulated fetch explosion")

    sb.get_remote_file = boom
    assert sb.get_web_image(REPO, "thumb.png", "main") is None

    def garbage(url, path, branch="main", **kw):
        return b"not an image"

    sb.get_remote_file = garbage
    assert sb.get_web_image(REPO, "thumb.png", "main") is None


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_remote_datatype")
    test_binary_fetch_cached_under_content_key()
    test_text_and_binary_keys_do_not_collide()
    test_stale_fallback_respects_data_type()
    test_get_web_image_propagates_base_exceptions()
    test_get_web_image_still_contains_ordinary_errors()
    print("scenario_store_remote_datatype: PASS")


if __name__ == "__main__":
    main()
