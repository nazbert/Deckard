"""
Unit-tier scenario (issue #201): KeyGIF must decide how to HOLD a GIF from
its headers, and hold an opaque one off the shared mp4 tile cache instead of
in a retained RGBA frame list.

The frame list is the largest uncapped image holder in the app -- ~147 KB
per frame at 2x an XL tile, so a 200-frame GIF is ~29 MB and a 32-key page
of them ~0.9 GiB, roughly 10x the whole #142 evictable budget. Opaque GIFs
(the common case) do not need it: cv2 only drops the ALPHA channel, so the
existing refcounted key-video registry can serve their pixels at O(1) RAM
while PIL's per-frame delay timeline keeps driving playback.

Covers:
  (a) an opaque GIF attaches to the mp4 tile registry, retains no frames,
      registers NOTHING under the census's gif_frames group (its reader is
      already counted under video_readers), and still serves real images;
  (b) an alpha-carrying GIF stays on the retained frame list -- transparency
      survives, and the gif_frames census still sees it;
  (c) both routes pick the SAME frame index for the same wall clock over an
      irregular delay timeline -- the routing decision must be invisible to
      playback (the frame-index parity guard scales indices if a container
      ever demuxes a different frame count than PIL reports);
  (d) close() releases the reader back to the registry (refcount to zero,
      captures closed) and leaves late media ticks harmless;
  (e) a non-square opaque GIF is built at an aspect-preserving, shrink-only
      tile size -- the same geometry decode_gif_frames' ImageOps.contain
      would have produced, so the route cannot change what a key looks like;
  (f) DECKARD_GIF_KEY_BUDGET_MB follows the house env contract (malformed
      degrades to the default with a warning rather than raising out of a
      page load; 0 disables the retained list entirely);
  (g) an alpha GIF over that budget degrades to the opaque route -- alpha
      dropped, key still playing, footprint bounded -- and the census
      follows the outcome rather than the intent.

GIF fixtures are generated with PIL at runtime (no binary fixtures in-repo,
house convention); frames are visually distinct so PIL never merges them.
"""
import os

import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import KeyGIF, contained_size
from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.Subclasses import mp4_tile_cache

WATCHDOG_SECONDS = 60
TILE = (72, 72)
BUDGET = (TILE[0] * 2, TILE[1] * 2)  # KeyGIF's 2x-tile policy (mem-plan P2.3)


class _StubDeckController:
    """Exactly what KeyGIF.__init__ reads (same stub as scenario_gif_fit)."""

    def __init__(self, key_size: tuple[int, int] = TILE):
        self._key_size = key_size

    def get_key_image_size(self) -> tuple[int, int]:
        return self._key_size

    def get_display_saturation(self) -> float:
        return 1.0


class _StubControllerKey:
    def __init__(self, key_size: tuple[int, int] = TILE):
        self.deck_controller = _StubDeckController(key_size)


def _make_gif(name: str, *, opaque: bool, durations_ms: list[int],
              size=(200, 200)) -> str:
    """An animated GIF with explicit per-frame durations and a disc that
    shifts each frame. `opaque` decides the route under test: a fully opaque
    canvas declares no transparency in its header, a transparent one does."""
    frames = []
    for i in range(len(durations_ms)):
        base = (20, 40, 90, 255) if opaque else (0, 0, 0, 0)
        frame = Image.new("RGBA", size, base)
        draw = ImageDraw.Draw(frame)
        x0 = 10 + i * 7
        draw.ellipse([x0, 20, x0 + 90, 110], fill=(220, 30, 30, 255))
        frames.append(frame)
    path = os.path.join(gl.DATA_PATH, "media", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations_ms, loop=0, disposal=2,
    )
    return path


def _decode(path: str, key_size: tuple[int, int] = TILE) -> KeyGIF:
    return KeyGIF(controller_key=_StubControllerKey(key_size), gif_path=path,
                  fps=30, loop=True)


def _gif_frames_census() -> int:
    return cache_budget.totals().get("gif_frames", 0)


def check_opaque_gif_routes_to_the_tile_cache() -> None:
    """(a) The whole point: an opaque GIF holds no frames."""
    path = _make_gif("opaque_route.gif", opaque=True,
                     durations_ms=[100, 100, 100, 100, 100, 100])
    census_before = _gif_frames_census()
    gif = _decode(path)
    try:
        assert gif.video_cache is not None, (
            "an opaque GIF must attach to the mp4 tile registry"
        )
        assert gif.frames == [], (
            f"an opaque GIF must retain NO decoded frames, got {len(gif.frames)}"
        )
        assert gif.budget_bytes() == 0, (
            f"the video route holds no frame bytes of its own, got {gif.budget_bytes()}"
        )
        assert _gif_frames_census() == census_before, (
            "the video route must not register under gif_frames -- its RAM is the "
            "reader's, already counted under video_readers"
        )
        # The registry knows about it, with this reader as a consumer.
        key = mp4_tile_cache._registry_key(path, gif.video_cache.out_size, 1.0)
        entry = mp4_tile_cache._registry.get(key)
        assert entry is not None and entry.refcount == 1, (
            f"the reader must hold one registry reference, got {entry!r}"
        )

        # ... and it still serves real frames off the timeline.
        assert gif._frame_count() == 6, (
            f"the timeline must still carry every GIF frame, got {gif._frame_count()}"
        )
        frame = gif.get_next_frame(now=1_000_000.0)
        assert frame is not None, "the video route returned no frame at all"
        assert isinstance(frame, Image.Image), f"expected a PIL image, got {type(frame)}"
        assert frame.size == gif.video_cache.out_size, (
            f"frame size {frame.size} != the tile size the registry was asked to "
            f"build ({gif.video_cache.out_size})"
        )
        print("PASS: an opaque GIF plays off the shared tile cache with no retained frames")
    finally:
        gif.close()


def check_alpha_gif_stays_on_the_frame_list() -> None:
    """(b) Alpha still needs PIL: cv2's GIF demuxer drops the channel."""
    path = _make_gif("alpha_route.gif", opaque=False, durations_ms=[100, 100, 100, 100])
    census_before = _gif_frames_census()
    gif = _decode(path)
    try:
        assert gif.video_cache is None, (
            "an alpha-carrying GIF must NOT route to the opaque video path"
        )
        assert len(gif.frames) == 4, (
            f"the alpha route must retain every frame, got {len(gif.frames)}"
        )
        assert all(f.mode == "RGBA" for f in gif.frames), "alpha frames must stay RGBA"
        alpha = gif.frames[0].getchannel("A").getextrema()
        assert alpha[0] == 0 and alpha[1] == 255, (
            f"transparency must survive the decode, got alpha extrema {alpha}"
        )
        assert gif.budget_bytes() > 0, "the retained list must report its bytes"
        assert _gif_frames_census() >= census_before + gif.budget_bytes(), (
            "the retained frame list must still be visible in the gif_frames census"
        )
        print("PASS: an alpha-carrying GIF keeps the retained RGBA frame list")
    finally:
        gif.close()


def check_both_routes_pick_the_same_frames() -> None:
    """(c) The routing decision must be invisible to playback. Same
    irregular timeline, same wall clock, same frame indices -- the video
    route's bisect result is an index into the reader instead of a list
    subscript, nothing else changes."""
    durations = [200, 40, 40, 300, 100, 40, 500]
    opaque = _decode(_make_gif("parity_opaque.gif", opaque=True, durations_ms=durations))
    alpha = _decode(_make_gif("parity_alpha.gif", opaque=False, durations_ms=durations))
    try:
        assert opaque.video_cache is not None and alpha.video_cache is None, (
            "fixture sanity: the two fixtures must take different routes"
        )
        assert opaque.frame_delays == alpha.frame_delays == [200, 40, 40, 300, 100, 40, 500], (
            f"both routes must read the same delays: {opaque.frame_delays} vs "
            f"{alpha.frame_delays}"
        )
        assert opaque._cum_delays == alpha._cum_delays, (
            f"timeline mismatch: {opaque._cum_delays} vs {alpha._cum_delays}"
        )

        T0 = 1_000_000.0
        # Walk the whole loop plus a wrap, in steps that never exceed the 1s
        # away-gap threshold (that clamp has its own scenario).
        for step in range(60):
            now = T0 + step * 0.04
            opaque.get_next_frame(now=now)
            alpha.get_next_frame(now=now)
            assert opaque.active_frame == alpha.active_frame, (
                f"route divergence at t+{step * 0.04:.2f}s: video route picked "
                f"{opaque.active_frame}, frame list picked {alpha.active_frame}"
            )
            assert opaque.get_frame_delay() == alpha.get_frame_delay(), (
                f"frame delay divergence at t+{step * 0.04:.2f}s"
            )

        # The parity guard: a reader whose count disagrees with PIL's must
        # scale rather than index past the end (FFmpeg demuxes 1:1 today, so
        # drive the guard directly).
        cache = opaque.video_cache
        real_n = cache.n_frames
        try:
            cache.n_frames = 3  # container reports FEWER frames than the timeline
            assert opaque._source_index(cache, 6) == 2, (
                f"a short container must scale the last index into range, got "
                f"{opaque._source_index(cache, 6)}"
            )
            assert opaque._source_index(cache, 0) == 0
            cache.n_frames = 14  # ... and more frames than the timeline
            assert opaque._source_index(cache, 6) == 12, (
                f"a longer container must scale up, got {opaque._source_index(cache, 6)}"
            )
        finally:
            cache.n_frames = real_n
        assert opaque._source_index(cache, 4) == 4, (
            "matching counts must be the identity mapping"
        )
        print("PASS: both routes pick identical frames over an irregular timeline")
    finally:
        opaque.close()
        alpha.close()


def check_close_releases_the_reader() -> None:
    """(d) close() detaches from the registry (refcount to zero, captures
    closed) and leaves a late media tick harmless."""
    path = _make_gif("release_route.gif", opaque=True, durations_ms=[100, 100, 100])
    gif = _decode(path)
    gif.get_next_frame(now=1_000_000.0)
    cache = gif.video_cache
    key = mp4_tile_cache._registry_key(path, cache.out_size, 1.0)
    assert mp4_tile_cache._registry.get(key) is not None, "fixture sanity: registered"

    gif.close()

    assert gif.video_cache is None, "close() must drop the reader reference"
    assert mp4_tile_cache._registry.get(key) is None, (
        "the last consumer's close() must drop the registry entry (refcount 0)"
    )
    assert cache.cap is None and cache._cache_cap is None, (
        "close() must release the reader's captures"
    )
    assert gif.get_next_frame(now=1_000_100.0) is None, (
        "a media tick after close() must be a no-op on the video route too"
    )
    gif.close()  # idempotent
    print("PASS: close() releases the shared reader; late ticks stay no-ops")


def check_video_route_geometry_matches_the_frame_list() -> None:
    """(e) A non-square GIF must not be cropped or squished by taking the
    video route: the registry is asked for exactly the size ImageOps.contain
    would have produced, and shrink-only still holds for a small source."""
    wide = _decode(_make_gif("wide_opaque.gif", opaque=True,
                             durations_ms=[100, 100, 100], size=(320, 160)))
    small = _decode(_make_gif("small_opaque.gif", opaque=True,
                              durations_ms=[100, 100], size=(40, 40)))
    try:
        # 320x160 into a 144x144 budget -> 144x72 (the binding dimension is
        # width), which is what scenario_gif_fit pins for the frame list.
        assert contained_size((320, 160), BUDGET) == (144, 72), "helper sanity"
        assert wide.video_cache.out_size == (144, 72), (
            f"a 2:1 source must be built at the aspect-preserving 144x72, got "
            f"{wide.video_cache.out_size}"
        )
        frame = wide.get_next_frame(now=1_000_000.0)
        assert frame.size == (144, 72), (
            f"the served frame must carry that geometry, got {frame.size}"
        )
        # Shrink-only: a 40x40 source is already inside the budget.
        assert small.video_cache.out_size == (40, 40), (
            f"a smaller-than-budget source must keep its own size, got "
            f"{small.video_cache.out_size}"
        )
        print("PASS: the video route builds the same geometry the frame list would")
    finally:
        wide.close()
        small.close()


def check_budget_env_contract() -> None:
    """(f) DECKARD_GIF_KEY_BUDGET_MB follows the house env contract
    (native_tile_cache.native_tile_cache_max_bytes): a malformed value
    degrades to the default with a warning instead of raising out of a page
    load, including the values float() accepts but int() cannot take. 0 and
    negatives disable the RAM route entirely."""
    from src.backend.DeckManagement.DeckController import (
        GIF_KEY_BUDGET_MB, gif_key_budget_bytes,
    )

    default = GIF_KEY_BUDGET_MB * 1024 * 1024
    previous = os.environ.get("DECKARD_GIF_KEY_BUDGET_MB")
    try:
        os.environ.pop("DECKARD_GIF_KEY_BUDGET_MB", None)
        assert gif_key_budget_bytes() == default, "unset must use the default"

        for malformed in ("plenty", "", "nan", "inf", "-inf", "1e400"):
            os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = malformed
            assert gif_key_budget_bytes() == default, (
                f"malformed DECKARD_GIF_KEY_BUDGET_MB={malformed!r} must degrade "
                f"to the default, got {gif_key_budget_bytes()}"
            )

        os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = "8"
        assert gif_key_budget_bytes() == 8 * 1024 * 1024, "a valid value must apply"
        os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = "0.5"
        assert gif_key_budget_bytes() == 512 * 1024, "fractional MB must apply"
        for off in ("0", "-4"):
            os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = off
            assert gif_key_budget_bytes() == 0, (
                f"{off!r} must disable the retained frame list entirely"
            )
    finally:
        if previous is None:
            os.environ.pop("DECKARD_GIF_KEY_BUDGET_MB", None)
        else:
            os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = previous
    print("PASS: the GIF key budget env var degrades instead of raising")


def check_over_budget_alpha_degrades_to_the_video_route() -> None:
    """(g) An alpha GIF over the per-GIF budget degrades to the opaque route
    -- alpha is dropped, the key keeps playing, and the footprint stays
    bounded (the same ladder GifBackground walks down to cv2). The census
    must follow the outcome: no gif_frames registration for a GIF that never
    built a frame list."""
    path = _make_gif("over_budget_alpha.gif", opaque=False,
                     durations_ms=[100, 100, 100, 100, 100])
    previous = os.environ.get("DECKARD_GIF_KEY_BUDGET_MB")
    os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = "0.01"  # 10 KB: one frame is ~83 KB
    census_before = _gif_frames_census()
    try:
        gif = _decode(path)
    finally:
        if previous is None:
            os.environ.pop("DECKARD_GIF_KEY_BUDGET_MB", None)
        else:
            os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = previous
    try:
        assert gif.frames == [], (
            f"an over-budget GIF must retain no frames, got {len(gif.frames)}"
        )
        assert gif.video_cache is not None, (
            "an over-budget alpha GIF must degrade to the mp4 tile cache, not fail"
        )
        assert _gif_frames_census() == census_before, (
            "a GIF that never built a frame list must not appear under gif_frames"
        )
        frame = gif.get_next_frame(now=1_000_000.0)
        assert frame is not None, "the degraded key must still play"
        # The documented cost of the degrade: cv2 has no alpha channel.
        assert frame.mode == "RGB", (
            f"the video route serves opaque frames, got mode {frame.mode}"
        )
        # Under the default budget the very same GIF keeps its frame list.
        unbudgeted = _decode(path)
        try:
            assert unbudgeted.video_cache is None and len(unbudgeted.frames) == 5, (
                "at the default budget this GIF must stay on the frame list -- "
                "otherwise the degrade above proved nothing"
            )
        finally:
            unbudgeted.close()
        print("PASS: an over-budget alpha GIF degrades to the bounded video route")
    finally:
        gif.close()


def main() -> None:
    fixtures.install_stub_globals({"performance": {"cache-videos": True}})
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_gif_opaque_route")
    check_opaque_gif_routes_to_the_tile_cache()
    check_alpha_gif_stays_on_the_frame_list()
    check_both_routes_pick_the_same_frames()
    check_close_releases_the_reader()
    check_video_route_geometry_matches_the_frame_list()
    check_budget_env_contract()
    check_over_budget_alpha_degrades_to_the_video_route()
    print("PASS: scenario_gif_opaque_route")


if __name__ == "__main__":
    main()
