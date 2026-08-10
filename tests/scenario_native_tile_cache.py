"""
Unit + integration scenario: the frame-identity native tile cache.

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
import threading
import time

import src.backend.DeckManagement.deck_controller.inputs as inputs_module
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

        # float() accepts these; int() does not (ValueError for nan,
        # OverflowError for inf), and nan even survives the sign test since
        # every nan comparison is False. They must take the same
        # degrade-with-a-warning path as a typo, not raise out of
        # DeckController.__init__.
        for hostile in ("nan", "inf", "-inf", "1e400"):
            os.environ["DECKARD_NATIVE_TILE_CACHE_MB"] = hostile
            assert native_tile_cache_max_bytes() == DEFAULT_MAX_MB * 1024 * 1024, (
                f"a non-finite DECKARD_NATIVE_TILE_CACHE_MB={hostile!r} must fall back "
                f"to the default"
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
    """Counts encode_native_key() calls made through deck_controller.inputs
    (both paint paths live there and call it as a module global, so that is
    the namespace the name has to be replaced in)."""

    def __init__(self):
        self.calls = 0
        self._original = None

    def __enter__(self) -> "_EncodeCounter":
        self._original = inputs_module.encode_native_key

        def counted(*args, **kwargs):
            self.calls += 1
            return self._original(*args, **kwargs)

        inputs_module.encode_native_key = counted
        return self

    def __exit__(self, *exc) -> None:
        inputs_module.encode_native_key = self._original


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
    before that lands (a label) is silently discarded.

    The background future is part of that wait, not an extra: load_page hands
    load_background to a worker thread, and for a page with no background of
    its own that thread ends in `Background.set_video(None)`. A scenario that
    installs its own video before that lands has it evicted mid-build, and
    the tile cache then never completes -- the exact shape the flake was reported
    as, produced by CPU contention delaying that thread into the window.
    Every wait here is content-based, so a loaded runner is slower, not
    wrong."""
    assert fixtures.wait_until(lambda: controller.active_page is not None, timeout=15), \
        "fixture sanity: no page loaded"
    def _background_load_done() -> bool:
        future = getattr(controller, "_bg_future", None)
        return future is None or future.done()

    assert fixtures.wait_until(_background_load_done, timeout=15), \
        "fixture sanity: the page's background load never finished"
    assert fixtures.wait_until(
        lambda: not controller.media_player.tasks and not controller.media_player.image_tasks,
        timeout=15), \
        "fixture sanity: the media player never drained its page-load tasks"

    # Empty queues mean DEQUEUED, not done: perform_media_player_tasks pops
    # the whole batch and only then runs it, so load_all_inputs can still be
    # executing while `tasks` reads empty. Wait for a marker instead. It is
    # submitted through the same path, and tasks run in order within a batch
    # and across batches, so the marker having RUN means everything queued
    # ahead of it has finished -- a real completion signal rather than a
    # sleep long enough to usually cover one (which is exactly the
    # loaded-runner assumption).
    ran = threading.Event()
    controller.media_player.add_task(ran.set)
    assert ran.wait(timeout=15), (
        "fixture sanity: the media player never ran the settle marker -- the "
        "page-load tasks queued ahead of it have not completed"
    )


def _start_video(controller, path: str) -> "BackgroundVideo":
    """Installs `path` as the deck background and detaches it from the media
    thread (video.page is what the tick predicate matches on), so the
    scenario is the only thing advancing frames.

    Call _settle() first: the media thread's predicate is `video.page is
    active_page`, so `page = None` only detaches once a page is actually
    loaded -- before that it MATCHES, and the media thread drives the
    sequential build concurrently (interleaved frame requests go backwards,
    which aborts the writer for good)."""
    assert controller.active_page is not None, (
        "fixture sanity: _start_video needs a loaded page -- with active_page "
        "still None, `video.page = None` matches the media thread's tick "
        "predicate instead of detaching from it"
    )
    video = BackgroundVideo(controller, path, loop=True, fps=30)
    video.page = None
    controller.background.set_video(video, update=False)
    # Playing straight through builds the tile cache; from then on frames
    # are picked by wall clock, which _show_frame drives deterministically.
    # Bounded by a deadline, not by a tick count: the old budget was sized
    # for the decode count, so a starved runner read as a broken cache.
    # A deadline alone would let the build get arbitrarily wasteful without
    # anyone noticing, so keep an efficiency bound too -- just a generous one,
    # since the count is what a starved runner cannot be judged on. A clean
    # build takes exactly n_frames ticks (measured: 6); 10x catches the
    # 100x-class regression (a re-seek or a re-decode per tick) and nothing
    # a slow machine can produce.
    max_ticks = video.n_frames * 10
    deadline = time.monotonic() + 30.0
    ticks = 0
    while not video.is_cache_complete():
        assert ticks <= max_ticks, (
            f"the tile cache build took {ticks} ticks for {video.n_frames} frames "
            f"(budget {max_ticks}); a clean build spends one tick per frame, so "
            f"this is re-seeking or re-decoding rather than progressing"
        )
        assert controller.background.video is video, (
            "fixture sanity: the background was replaced mid-build -- a page-load "
            "background thread landed inside the window (see _settle)"
        )
        assert not video.is_build_terminal(), (
            f"fixture sanity: the tile cache build went terminal after {ticks} ticks "
            f"(source frame {video.last_frame_index} of {video.n_frames})"
        )
        assert time.monotonic() < deadline, (
            f"fixture sanity: the tile cache never completed -- {ticks} ticks, source "
            f"frame {video.last_frame_index} of {video.n_frames}, writer "
            f"{'open' if video._writer is not None else 'closed'}"
        )
        controller.background.update_tiles()
        ticks += 1
    return video


# Retry budget for _show_frame, deliberately tiny. A clean run needs ZERO
# retries (measured: 0 across the 14 calls this scenario makes), and the cause
# the retry exists to absorb -- one deschedule longer than a frame period --
# costs exactly one. Anything beyond that is not scheduler noise, it is the
# landing ceasing to be a deterministic function of the timebase, and an
# unbounded retry would hide precisely that: retry until it lands and every
# downstream contract assert goes green again on a background that is no
# longer frame-accurate.
_MAX_ATTEMPTS_PER_CALL = 3
_MAX_TOTAL_RETRIES = 3
_frame_retries = 0


def _show_frame(video, controller, index: int) -> None:
    """Advances the background to a SPECIFIC frame. get_next_tiles() picks
    by wall clock once the cache is complete, so the timebase is rewound to
    place `index` at now; _last_frame_tick is cleared so the resume-gap
    clamp (which shifts the timebase after a >1s stall) can't move it.

    Retried on a miss, on a budget: the rewind and the pick are two separate
    wall-clock reads, so a thread descheduled between them for longer than one
    frame period (67ms at this fixture's 15fps source) lands one frame late
    on a loaded runner. The landing requirement stays exact, the retry only absorbs that
    one deschedule, and the budget is what keeps 'retry until it lands' from
    standing in for 'lands where the timebase says'."""
    global _frame_retries
    playback_fps = float(video.get_source_fps() or video.fps or 30)
    for attempt in range(1, _MAX_ATTEMPTS_PER_CALL + 1):
        if attempt > 1:
            _frame_retries += 1
            assert _frame_retries <= _MAX_TOTAL_RETRIES, (
                f"_show_frame has now spent {_frame_retries} retries across this "
                f"scenario (budget {_MAX_TOTAL_RETRIES}, clean runs need 0): the "
                f"frame the background lands on is no longer a deterministic "
                f"function of the timebase, and retrying until it lands would "
                f"leave every contract assert below green on a background that "
                f"is not frame-accurate"
            )
        video._last_frame_tick = None
        video._play_start = time.time() - index / playback_fps
        controller.background.update_tiles()
        if video.active_frame == index:
            return
    raise AssertionError(
        f"fixture sanity: wanted frame {index}, background advanced to "
        f"{video.active_frame} on all {_MAX_ATTEMPTS_PER_CALL} attempts"
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
            f"encode-free (that is the whole point of the native tile cache)"
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
        # Before anything is read off the controller: load_all_inputs rebuilds
        # the inputs, and the page's own background load ends in
        # set_video(None), which would evict the video installed below.
        _settle(controller)
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
