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
  (c) over a REAL DeckController on a fake deck with a looping background
      video: the second playthrough performs ZERO encodes on passthrough
      keys, and presents byte-identical natives to the first.
  (d) a key with a visible label on that same page keeps the pixel-hash
      path -- it never files an entry under a frame identity.
  (e) a background swap mid-playback empties the cache, and the very next
      frame writes the NEW video's bytes to the device (no stale tile).
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import hashlib
import os
import time

import src.backend.DeckManagement.DeckController as deck_controller_module
from src.backend.DeckManagement.DeckController import BackgroundVideo
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
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


def _make_test_mp4(path: str, base_green: int, size=(320, 240), n_frames=6, fps=15) -> str:
    """A tiny video whose frames carry a moving two-axis gradient, so every
    (frame, key) pair crops to DIFFERENT pixels -- solid frames would let the
    pixel-hash memo share one native across all keys and mask what is being
    measured. `base_green` shifts the palette, which is what makes two of
    these files differ (and so hash to different md5s)."""
    import cv2
    import numpy as np
    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    xs = np.arange(size[0], dtype=np.uint16)[None, :]
    ys = np.arange(size[1], dtype=np.uint16)[:, None]
    for i in range(n_frames):
        frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        frame[:, :, 0] = ((xs * 3 + i * 41) % 256).astype(np.uint8)
        frame[:, :, 1] = base_green
        frame[:, :, 2] = ((ys * 3 + i * 67) % 256).astype(np.uint8)
        writer.write(frame)
    writer.release()
    assert os.path.getsize(path) > 0
    return path


class _EncodeCounter:
    """Counts encode_native_key() calls made through the DeckController
    module (both paint paths call it as a module global)."""

    def __init__(self):
        self.calls = 0
        self._original = None

    def __enter__(self) -> "_EncodeCounter":
        self._original = deck_controller_module.encode_native_key

        def counted(*args, **kwargs):
            self.calls += 1
            return self._original(*args, **kwargs)

        deck_controller_module.encode_native_key = counted
        return self

    def __exit__(self, *exc) -> None:
        deck_controller_module.encode_native_key = self._original


def _record_enqueued_natives(controller) -> dict:
    """Wraps add_image_task so every enqueued native is captured by key
    index, synchronously with update() (the media thread still presents it
    -- this only observes)."""
    enqueued: dict = {}
    original = controller.media_player.add_image_task

    def recording(key_index, native_image, **kwargs):
        enqueued[key_index] = native_image
        return original(key_index, native_image, **kwargs)

    controller.media_player.add_image_task = recording
    return enqueued


def _record_cached_keys(controller) -> list:
    """Every cache key the paint path files an entry under."""
    keys: list = []
    original = controller.native_tile_cache.put

    def recording(key, data):
        keys.append(key)
        return original(key, data)

    controller.native_tile_cache.put = recording
    return keys


def _settle(controller) -> None:
    """Waits out the page load. load_page/load_all_inputs finish on their own
    threads and REBUILD each state's managers, so anything staged on a state
    before that lands (a label) is silently discarded."""
    assert fixtures.wait_until(lambda: controller.active_page is not None, timeout=5), \
        "fixture sanity: no page loaded"
    fixtures.wait_until(lambda: not controller.media_player.image_tasks, timeout=5)
    time.sleep(0.5)


def _start_video(controller, path: str) -> "BackgroundVideo":
    """Installs `path` as the deck background and detaches it from the media
    thread (video.page is what the tick predicate matches on), so the
    scenario is the only thing advancing frames."""
    video = BackgroundVideo(controller, path, loop=True, fps=30)
    video.page = None
    controller.background.set_video(video, update=False)
    # Playing straight through builds the tile cache; from then on frames
    # are picked by wall clock, which _show_frame drives deterministically.
    for _ in range(video.n_frames * 3 + 10):
        if video.is_cache_complete():
            break
        controller.background.update_tiles()
    assert video.is_cache_complete(), "fixture sanity: the tile cache never completed"
    return video


def _show_frame(video, controller, index: int) -> None:
    """Advances the background to a SPECIFIC frame. get_next_tiles() picks
    by wall clock once the cache is complete, so the timebase is rewound to
    place `index` at now; _last_frame_tick is cleared so the resume-gap
    clamp (which shifts the timebase after a >1s stall) can't move it."""
    playback_fps = float(video.get_source_fps() or video.fps or 30)
    video._last_frame_tick = None
    video._play_start = time.time() - index / playback_fps
    controller.background.update_tiles()
    assert video.active_frame == index, (
        f"fixture sanity: wanted frame {index}, background advanced to {video.active_frame}"
    )


def check_second_loop_is_encode_free() -> None:
    video_path = _make_test_mp4(os.path.join(fixtures.DATA_DIR, "assets", "native_tile_a.mp4"), base_green=60)

    controller = fixtures.make_headless_controller(serial="native-tile-1")
    try:
        _settle(controller)
        keys = sorted(controller.inputs[Input.Key], key=lambda k: k.index)
        assert keys, "fixture sanity: expected key inputs"

        video = _start_video(controller, video_path)

        # One key carries a label: it composites more than the bare tile, so
        # it must stay on the pixel-hash path (check (d)).
        labeled = keys[0]
        labeled.get_active_state().label_manager.set_page_label(
            "center", KeyLabel(controller_input=labeled, text="LBL", font_size=15), update=False)
        assert not labeled._tile_passthrough_ok(labeled.get_active_state()), (
            "fixture sanity: a key with a visible label must not be passthrough"
        )
        bare = keys[1:]
        cached_keys = _record_cached_keys(controller)
        enqueued = _record_enqueued_natives(controller)

        loop_natives: list[dict] = []
        loop_encodes: list[int] = []
        for _ in range(2):
            natives: dict = {}
            bare_encodes = 0
            with _EncodeCounter() as counter:
                for frame_index in range(video.n_frames):
                    _show_frame(video, controller, frame_index)
                    for key in keys:
                        # Attributed per key: the labeled key is EXPECTED to
                        # keep encoding (pixel path), only the bare ones must
                        # go quiet once their frames are cached.
                        before = counter.calls
                        key.update()
                        if key is not labeled:
                            bare_encodes += counter.calls - before
                            natives[(frame_index, key.index)] = enqueued[key.index]
            loop_natives.append(natives)
            loop_encodes.append(bare_encodes)

        expected_pairs = video.n_frames * len(bare)
        assert loop_encodes[0] >= expected_pairs, (
            f"fixture sanity: the first loop should encode every (frame, key) pair at least "
            f"once ({expected_pairs}); it encoded {loop_encodes[0]}"
        )
        assert loop_encodes[1] == 0, (
            f"the second playthrough of a looping background video re-encoded "
            f"{loop_encodes[1]} times -- frame identity must make a warmed loop "
            f"encode-free (that is the whole point of gl#163)"
        )
        assert loop_natives[1] == loop_natives[0], (
            "the cached natives presented on loop 2 differ from the bytes encoded on "
            "loop 1 -- the identity cache is serving the wrong frame"
        )

        # (d) the labeled key never took the identity path.
        labeled_entries = [k for k in cached_keys if k[2] == labeled.index]
        assert not labeled_entries, (
            f"a key with a visible label filed {len(labeled_entries)} native tile entries -- "
            f"only keys that composite to exactly the background tile may be keyed by frame identity"
        )
        bare_entries = {k[2] for k in cached_keys}
        assert bare_entries == {k.index for k in bare}, (
            f"expected exactly the bare keys to be identity-cached, got indices {sorted(bare_entries)}"
        )

        print(f"PASS: loop 2 of a background video encodes 0 times "
              f"(loop 1: {loop_encodes[0]}) and presents identical natives")
    finally:
        fixtures.teardown(controller)


def check_background_swap_drops_stale_natives() -> None:
    assets = os.path.join(fixtures.DATA_DIR, "assets")
    first_path = _make_test_mp4(os.path.join(assets, "native_tile_swap_a.mp4"), base_green=20)
    second_path = _make_test_mp4(os.path.join(assets, "native_tile_swap_b.mp4"), base_green=220)

    controller = fixtures.make_headless_controller(serial="native-tile-2")
    try:
        keys = sorted(controller.inputs[Input.Key], key=lambda k: k.index)
        probe = keys[0]
        deck = fixtures.raw_deck(controller)

        video = _start_video(controller, first_path)
        cached_keys = _record_cached_keys(controller)
        enqueued = _record_enqueued_natives(controller)

        _show_frame(video, controller, 0)
        for key in keys:
            key.update()
        assert len(controller.native_tile_cache) > 0, "fixture sanity: playback should fill the native tile cache"
        assert cached_keys, "fixture sanity: expected recorded cache keys"
        stale_native = enqueued[probe.index]
        stale_key = next(k for k in cached_keys if k[2] == probe.index)

        # Swap the background mid-playback.
        second = BackgroundVideo(controller, second_path, loop=True, fps=30)
        second.page = None
        controller.background.set_video(second, update=False)

        assert len(controller.native_tile_cache) == 0, (
            "a background content change must empty the native tile cache -- every entry "
            "is keyed against the OLD video's frames"
        )
        assert controller.native_tile_cache.get(stale_key) is None, "the old video's entries must not survive the swap"

        for _ in range(second.n_frames * 3 + 10):
            if second.is_cache_complete():
                break
            controller.background.update_tiles()
        _show_frame(second, controller, 0)
        for key in keys:
            key.update()

        fresh_native = enqueued[probe.index]
        assert fresh_native != stale_native, "the first frame after the swap re-presented the OLD video's bytes"

        # The device must have been handed the new bytes, not the old ones.
        fresh_hash = hashlib.sha1(fresh_native).hexdigest()[:12]
        stale_hash = hashlib.sha1(stale_native).hexdigest()[:12]
        slot = f"key:{probe.index}"
        assert fixtures.wait_until(
            lambda: (deck.last_op_for(slot) or (None,) * 5)[4] == fresh_hash, timeout=5.0
        ), (
            f"the deck's last write for {slot} is "
            f"{(deck.last_op_for(slot) or (None,) * 5)[4]}, expected the post-swap frame {fresh_hash} "
            f"(stale was {stale_hash})"
        )

        print("PASS: a background swap empties the native tile cache and no stale tile reaches the deck")
    finally:
        fixtures.teardown(controller)


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_native_tile_cache")

    check_accounting_and_eviction()
    check_clear()
    check_disabled_cache_stores_nothing()
    check_env_knob()
    check_second_loop_is_encode_free()
    check_background_swap_drops_stale_natives()

    print("PASS: scenario_native_tile_cache")


if __name__ == "__main__":
    main()
