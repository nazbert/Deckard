"""
Unit + integration scenario (gl#163): the frame-identity native tile cache.

Background video playback used to pay, per frame and per key, a full RGBA
tobytes()+hash plus (on an encode-memo miss) a fresh JPEG encode -- work that
is identical on every loop of the same video. NativeTileCache keys the
encoded bytes by frame identity instead of by composited pixels, so a looping
video encodes once and every later loop is a dict lookup.

Covers:
  (a) NativeTileCache byte accounting, LRU eviction order and clear().
  (b) the DECKARD_NATIVE_TILE_CACHE_MB kill-switch: 0 disables the store
      entirely (get() always misses), a malformed value degrades to the
      default instead of raising out of DeckController.__init__.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import os

from src.backend.DeckManagement.Subclasses.native_tile_cache import (
    DEFAULT_MAX_MB,
    NativeTileCache,
    native_tile_cache_max_bytes,
)


def check_accounting_and_eviction() -> None:
    # Room for exactly two 100-byte entries.
    cache = NativeTileCache(max_bytes=200)

    cache.put(("md5", 0, 0), b"a" * 100)
    cache.put(("md5", 1, 0), b"b" * 100)
    assert cache.total_bytes == 200, f"byte accounting drifted: {cache.total_bytes}"
    assert len(cache) == 2

    # Unlike the pixel-hash memo, a key is admitted on its FIRST sighting --
    # that is what makes the second playback loop encode-free.
    assert cache.get(("md5", 0, 0)) == b"a" * 100, (
        "an identity key must be cached on its first put() -- a doorkeeper "
        "would push every loop's first encode into a second loop"
    )

    # Touch frame 0 so frame 1 is the least-recently-used entry, then
    # overflow: the LRU victim must be frame 1, not the just-used frame 0.
    cache.get(("md5", 0, 0))
    cache.put(("md5", 2, 0), b"c" * 100)
    assert cache.get(("md5", 1, 0)) is None, "LRU eviction must drop the least-recently-used entry"
    assert cache.get(("md5", 0, 0)) == b"a" * 100, "LRU eviction must keep the most-recently-used entry"
    assert cache.total_bytes == 200, f"byte accounting drifted after eviction: {cache.total_bytes}"

    # Re-putting an existing key replaces rather than double-counts it.
    cache.put(("md5", 0, 0), b"d" * 50)
    assert cache.get(("md5", 0, 0)) == b"d" * 50
    assert cache.total_bytes == 150, f"re-put double-counted: {cache.total_bytes}"

    # An entry larger than the whole cap doesn't wedge the cache: it is
    # stored, then immediately evicted down to the cap (never negative
    # accounting, never an unbounded store).
    cache.put(("md5", 9, 0), b"x" * 500)
    assert cache.total_bytes <= 500
    assert cache.total_bytes >= 0

    print("PASS: NativeTileCache byte accounting + LRU eviction")


def check_clear() -> None:
    cache = NativeTileCache(max_bytes=1024)
    cache.put(("md5", 0, 0), b"a" * 10)
    assert cache.get(("md5", 0, 0)) is not None

    cache.clear()
    assert cache.get(("md5", 0, 0)) is None, "clear() must drop cached entries"
    assert cache.total_bytes == 0, "clear() must reset byte accounting"
    assert len(cache) == 0

    # Still usable afterwards (clear is not a teardown).
    cache.put(("md5", 0, 0), b"a" * 10)
    assert cache.get(("md5", 0, 0)) == b"a" * 10

    print("PASS: NativeTileCache clear()")


def check_disabled_cache_stores_nothing() -> None:
    cache = NativeTileCache(max_bytes=0)
    assert not cache.enabled
    cache.put(("md5", 0, 0), b"a" * 10)
    assert cache.get(("md5", 0, 0)) is None, (
        "a disabled cache must never serve bytes -- the kill-switch has to "
        "put playback back on the pixel-hash path exactly"
    )
    assert cache.total_bytes == 0
    assert len(cache) == 0

    print("PASS: NativeTileCache disabled (max_bytes=0) stores nothing")


def check_env_knob() -> None:
    saved = os.environ.get("DECKARD_NATIVE_TILE_CACHE_MB")
    try:
        os.environ.pop("DECKARD_NATIVE_TILE_CACHE_MB", None)
        assert native_tile_cache_max_bytes() == DEFAULT_MAX_MB * 1024 * 1024

        os.environ["DECKARD_NATIVE_TILE_CACHE_MB"] = "0"
        assert native_tile_cache_max_bytes() == 0, "0 must disable the cache (kill-switch)"

        os.environ["DECKARD_NATIVE_TILE_CACHE_MB"] = "8"
        assert native_tile_cache_max_bytes() == 8 * 1024 * 1024

        # A typo must degrade to the default, not raise: this is read in
        # DeckController.__init__, where DeckManager would swallow the
        # exception as "Failed to initialize deck".
        os.environ["DECKARD_NATIVE_TILE_CACHE_MB"] = "sixty-four"
        assert native_tile_cache_max_bytes() == DEFAULT_MAX_MB * 1024 * 1024, (
            "a malformed DECKARD_NATIVE_TILE_CACHE_MB must fall back to the default"
        )

        os.environ["DECKARD_NATIVE_TILE_CACHE_MB"] = "-5"
        assert native_tile_cache_max_bytes() == 0, "a negative cap must disable, not go negative"
    finally:
        if saved is None:
            os.environ.pop("DECKARD_NATIVE_TILE_CACHE_MB", None)
        else:
            os.environ["DECKARD_NATIVE_TILE_CACHE_MB"] = saved

    print("PASS: DECKARD_NATIVE_TILE_CACHE_MB parsing (default / disable / malformed)")


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_native_tile_cache")

    check_accounting_and_eviction()
    check_clear()
    check_disabled_cache_stores_nothing()
    check_env_knob()

    print("PASS: scenario_native_tile_cache")


if __name__ == "__main__":
    main()
