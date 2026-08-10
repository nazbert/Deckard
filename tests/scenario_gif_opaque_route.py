"""
Unit-tier scenario: KeyGIF must hold an OPAQUE GIF off the
shared mp4 tile cache instead of in a retained RGBA frame list -- without
ever letting a second compositor near the pixels.

The frame list is the largest uncapped image holder in the app -- ~147 KB
per frame at 2x an XL tile, so a 200-frame GIF is ~29 MB and a 32-key page
of them ~0.9 GiB, roughly 10x the whole evictable image-cache budget. Opaque GIFs
(the common case) do not need it: an mp4 only lacks the ALPHA channel, so
the existing refcounted key-video registry can serve their pixels at O(1)
RAM while PIL's per-frame delay timeline keeps driving playback.

Two rules from the design's v2 are what make that safe, and most of the
legs below exist to pin them:

  * PIL IS THE ONLY COMPOSITOR. The tile mp4 is written FROM PIL-composited
    frames; FFmpeg never demuxes a GIF. FFmpeg's own GIF compositing
    disagrees with PIL on disposal and partial-extent frames, so a route
    that let it build the cache silently changed what keys looked like.
  * THE ROUTE FOLLOWS RENDERED ALPHA, not the header's declaration: 75% of
    real GIFs declare a transparent index and only 11% ever render one.

Covers:
  (a) an opaque GIF attaches to the mp4 tile registry, retains no frames,
      is not a member of the census registry at all (its reader is already
      counted under video_readers), and still serves real images;
  (b) an alpha-carrying GIF stays on the retained frame list -- transparency
      survives, and the gif_frames census still sees it;
  (c) both routes pick the SAME frame index for the same wall clock over an
      irregular delay timeline -- the routing decision must be invisible to
      playback (the frame-index parity guard scales indices if a container
      ever reports a different frame count than PIL);
  (d) close() releases the reader back to the registry (refcount to zero,
      captures closed) and leaves late media ticks harmless;
  (e) a non-square opaque GIF is built at an aspect-preserving, shrink-only
      tile size -- the same geometry decode_gif_frames' ImageOps.contain
      would have produced, so the route cannot change what a key looks like;
  (f) DECKARD_GIF_KEY_BUDGET_MB follows the house env contract (malformed
      degrades to the default with ONE warning per distinct value rather
      than raising out of a page load; 0 disables the retained list; a
      sub-1-MiB budget is announced rather than silently dropping alpha
      app-wide);
  (g) an alpha GIF over that budget degrades to the bounded route -- alpha
      dropped, key still playing, footprint bounded -- the census follows
      the outcome rather than the intent, and the degraded artifact is a
      SEPARATE cache variant so raising the budget gives the GIF its
      transparency back;
  (h) a GIF that DECLARES transparency but renders none takes the video
      route -- the dominant real-world population, and the whole point of
      classifying on pixels;
  (i) per-frame CONTENT parity through the promoted mp4, on two compositing
      shapes: a mixed disposal-2 / sub-canvas-extent GIF (each frame index
      must show the disc where the SOURCE drew it -- compositing parity AND
      frame-index mapping, where an off-by-one is a whole disc-step away),
      and a partial-extent GIF whose untouched canvas is where PIL and
      FFmpeg visibly disagree;
  (j) odd and tiny geometries land on the even-dimension clamp mp4v needs;
  (k) a warm construction (the artifact already on disk) attaches to the
      PROMOTED file and composites not one frame;
  (l) close() waits for an in-flight frame fetch instead of releasing the
      reader underneath it;
  (m) with performance.cache-videos off every GIF stays in RAM and the
      registry is never touched -- a reader with no artifact would fall
      back to the SOURCE, whose end-of-source path releases the capture and
      then repeats one frame forever.

GIF fixtures are generated with PIL at runtime (no binary fixtures in-repo,
house convention); frames are visually distinct so PIL never merges them.
"""
import io
import os
import threading
import time

import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

import numpy as np
from PIL import Image, ImageDraw, ImageSequence
from loguru import logger as log

import globals as gl
import src.backend.DeckManagement.deck_controller.gif_pipeline as gif_pipeline
from src.backend.DeckManagement.DeckController import (
    BOUNDED_TILE_VARIANT, KeyGIF, contained_size, tile_video_size,
)
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


DISC_STEP = 20  # source px between one frame's disc and the next
DISC_SIZE = 40


def _make_gif(name: str, *, opaque: bool, durations_ms: list[int],
              size=(200, 200), disposal=2) -> str:
    """An animated GIF with explicit per-frame durations and a disc that
    shifts DISC_STEP px each frame. `opaque` decides the route under test: a
    fully opaque canvas renders no alpha, a transparent one does.

    `disposal` may be a per-frame list: mixing 2 (restore to background)
    with 1 (leave in place) is what makes PIL write sub-canvas frame extents
    for some frames and full-canvas ones for others -- the compositing shape
    that a second compositor gets wrong."""
    frames = []
    for i in range(len(durations_ms)):
        base = (20, 40, 90, 255) if opaque else (0, 0, 0, 0)
        frame = Image.new("RGBA", size, base)
        draw = ImageDraw.Draw(frame)
        x0 = 10 + i * DISC_STEP
        draw.ellipse([x0, 20, x0 + DISC_SIZE, 60], fill=(230, 20, 20, 255))
        frames.append(frame)
    path = os.path.join(gl.DATA_PATH, "media", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations_ms, loop=0, disposal=disposal,
    )
    return path


def _decode(path: str, key_size: tuple[int, int] = TILE) -> KeyGIF:
    return KeyGIF(controller_key=_StubControllerKey(key_size), gif_path=path,
                  fps=30, loop=True)


def _gif_frames_census() -> int:
    return cache_budget.totals().get("gif_frames", 0)


def _in_census_registry(obj) -> bool:
    """Membership, not bytes: a registrant reporting 0 bytes is still IN the
    budget's registry (and its label still lands in the telemetry CSV). The
    video route must not be there at all."""
    return any(cache is obj for cache in cache_budget._snapshot())


def _disc_centroid_x(frame: Image.Image) -> float:
    """Mean x of the red disc's pixels. Thresholded generously: mp4v is
    lossy and the frames have been resampled, so the disc's EDGE moves by a
    pixel -- its centre does not."""
    array = np.asarray(frame.convert("RGB"), dtype=int)
    mask = (array[:, :, 0] > 150) & (array[:, :, 1] < 120) & (array[:, :, 2] < 120)
    xs = np.nonzero(mask)[1]
    assert xs.size > 50, f"no disc found in the served frame ({xs.size} matching pixels)"
    return float(xs.mean())


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
        assert not _in_census_registry(gif), (
            "the video route must not be a MEMBER of the cache-budget registry at "
            "all -- reporting zero bytes is not the same as staying out of it"
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
    """(b) Alpha still needs PIL: an mp4 has no alpha channel."""
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
        assert _in_census_registry(gif), (
            "the retained frame list must be a MEMBER of the cache-budget registry"
        )
        assert _gif_frames_census() >= census_before + gif.budget_bytes(), (
            "the retained frame list must still be visible in the gif_frames census"
        )
        print("PASS: an alpha-carrying GIF keeps the retained RGBA frame list")
    finally:
        gif.close()


def _declare_unused_transparency(path: str) -> None:
    """Turn on the transparent-colour flag in the first frame's graphic
    control extension, pointing at a palette index no pixel uses.

    Done by patching the two bytes rather than writing the GIF with PIL,
    because PIL cannot produce this file: its writer drops a transparency
    index that the pixel data never references. The file is still perfectly
    ordinary -- three quarters of real animated GIFs declare an index like
    this, most of them never rendering a transparent pixel with it.

    GCE layout (GIF89a): 21 F9 04 <packed> <delay lo> <delay hi>
    <transparent index> 00. Bit 0 of <packed> is the transparent-colour
    flag."""
    raw = bytearray(open(path, "rb").read())
    assert raw[10] & 0x80, "fixture sanity: expected a global colour table"
    gct_entries = 2 ** ((raw[10] & 0x07) + 1)
    gce = raw.find(b"\x21\xf9\x04")
    assert gce > 0, "fixture sanity: no graphic control extension found"
    raw[gce + 3] |= 0x01                 # transparent colour flag on
    raw[gce + 6] = gct_entries - 1       # ... pointing at an unused entry
    with open(path, "wb") as file:
        file.write(raw)


def check_declared_but_opaque_gif_takes_the_video_route() -> None:
    """(h) The population the declaration test got wrong. This GIF DECLARES
    a transparent index on frame 0 and never renders one -- 64% of real
    GIFs behave this way (75% declare, 11% render), so routing on the
    declaration left the dominant population paying for a frame list it did
    not need, which is the opposite of what this issue is for."""
    path = _make_gif("declared_opaque.gif", opaque=True,
                     durations_ms=[100, 100, 100, 100])
    _declare_unused_transparency(path)

    assert "transparency" in Image.open(path).info, (
        "fixture sanity: this GIF must DECLARE transparency in its header"
    )
    assert all(
        frame.convert("RGBA").getextrema()[3][0] == 255
        for frame in ImageSequence.Iterator(Image.open(path))
    ), "fixture sanity: ... and must still render every pixel opaque"

    gif = _decode(path)
    try:
        assert gif.video_cache is not None and gif.frames == [], (
            "a GIF that declares transparency but renders none must take the video "
            "route -- the declaration is not the question, the pixels are"
        )
        print("PASS: a declared-but-opaque GIF routes to the tile cache")
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
        # scale rather than index past the end (the cache is written frame-
        # for-frame today, so drive the guard directly).
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


SPLICE_PALETTE = [255, 0, 255,   # 0: canvas fill PIL uses for uncovered area
                  230, 20, 20,   # 1: the disc
                  250, 250, 250,  # 2: the logical screen's background colour
                  10, 10, 10]    # 3: the sub-image's own backdrop
SPLICE_CANVAS = (200, 200)
SPLICE_PATCH = (100, 100)
SPLICE_ORIGIN = (50, 50)


def _make_partial_extent_gif(name: str, n_frames: int, disposals: list[int]) -> str:
    """A GIF whose frames -- INCLUDING FRAME 0 -- cover only part of the
    logical screen, assembled block by block.

    PIL's writer cannot produce this: it always writes frame 0 at full
    extent. It is nonetheless ordinary GIF89a, and it is the shape where
    compositors visibly disagree -- the area no frame ever paints is
    palette index 0 to PIL and the logical screen's background colour to
    FFmpeg (measured: 75% of pixels differ). Only the LZW-coded pixel data
    is borrowed from PIL, by saving each patch as its own GIF and splicing
    its image block in at a chosen position.

    Structure: GIF89a header, logical screen descriptor + global colour
    table, then per frame a graphic control extension (delay + disposal)
    and an image block, terminated by 0x3B."""
    def image_block(patch: Image.Image, left: int, top: int) -> bytes:
        buffer = io.BytesIO()
        patch.save(buffer, format="GIF")
        raw = bytearray(buffer.getvalue())
        start = raw.index(0x2C)  # image separator
        descriptor = raw[start:start + 10]
        assert not descriptor[9] & 0x80, (
            "fixture sanity: the patch must reuse the global colour table"
        )
        assert bytes(raw[13:19]) == bytes(SPLICE_PALETTE[:6]), (
            f"fixture sanity: PIL renumbered the patch palette to "
            f"{bytes(raw[13:19]).hex()} -- the spliced indices would mean other "
            f"colours than the global table's"
        )
        data = raw[start + 10:raw.rindex(b"\x3b")]  # LZW payload + terminator
        return bytes(
            b"\x2c"
            + left.to_bytes(2, "little") + top.to_bytes(2, "little")
            + patch.width.to_bytes(2, "little") + patch.height.to_bytes(2, "little")
            + bytes([descriptor[9]]) + data
        )

    colors = len(SPLICE_PALETTE) // 3
    size_bits = max(1, (colors - 1).bit_length()) - 1
    table = bytearray(SPLICE_PALETTE) + bytes(3 * (2 ** (size_bits + 1) - colors))
    blocks = [
        b"GIF89a"
        + SPLICE_CANVAS[0].to_bytes(2, "little") + SPLICE_CANVAS[1].to_bytes(2, "little")
        + bytes([0x80 | size_bits, 2, 0])  # GCT present; background colour = index 2
        + bytes(table)
        + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"  # loop forever
    ]
    for index in range(n_frames):
        # Indices 0 and 1 ONLY, in that order: PIL renumbers a saved patch's
        # palette to its used entries in ascending order, so using the first
        # two makes that renumbering the identity and the spliced indices
        # keep meaning what they mean in the table above.
        patch = Image.new("P", SPLICE_PATCH, 0)
        patch.putpalette(SPLICE_PALETTE)
        x0 = 5 + index * 10
        ImageDraw.Draw(patch).ellipse([x0, 10, x0 + 40, 60], fill=1)
        blocks.append(bytes([0x21, 0xF9, 0x04, (disposals[index] & 0x07) << 2,
                             10, 0, 0, 0x00]))  # 100ms, this frame's disposal
        blocks.append(image_block(patch, *SPLICE_ORIGIN))
    blocks.append(b"\x3b")

    path = os.path.join(gl.DATA_PATH, "media", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as file:
        file.write(b"".join(blocks))
    return path


def check_partial_extent_canvas_survives_the_route() -> None:
    """(i, second half -- the compositor-identity pin) The frames served
    through the promoted mp4 must be PIL's compositing, not some other
    library's.

    A GIF whose frames never cover the whole logical screen makes the two
    disagree loudly: PIL fills the untouched canvas with palette index 0,
    FFmpeg with the screen's declared background colour. Both are defensible
    readings of the spec; only one of them is what this app has always
    drawn, and a route that quietly swapped compositors changed 75% of the
    pixels on files like this one."""
    n = 6
    path = _make_partial_extent_gif("partial_extent.gif", n, [2, 1, 1, 2, 1, 1])
    source = Image.open(path)
    for index in range(n):
        source.seek(index)
        assert source.tile[0].extents == (50, 50, 150, 150), (
            f"fixture sanity: frame {index} must be a sub-canvas extent, got "
            f"{source.tile[0].extents}"
        )

    gif = _decode(path)
    try:
        cache = gif.video_cache
        assert cache is not None and cache.is_cache_complete() and cache.cap is None, (
            "fixture sanity: served from the promoted mp4, with no source capture"
        )
        scale = cache.out_size[0] / SPLICE_CANVAS[0]
        for index in range(n):
            frame = gif._frame_at(index).convert("RGB")
            # The untouched canvas, sampled well away from any edge so mp4v's
            # chroma ringing at the magenta/red boundary cannot reach it.
            for corner in ((3, 3), (frame.width - 4, 3), (3, frame.height - 4)):
                pixel = frame.getpixel(corner)
                assert all(abs(a - b) < 24 for a, b in zip(pixel, (255, 0, 255))), (
                    f"frame {index} at {corner}: canvas is {pixel}, PIL composites "
                    f"(255, 0, 255) there -- a different compositor filled it"
                )
            expected = (SPLICE_ORIGIN[0] + 5 + index * 10 + 20) * scale
            got = _disc_centroid_x(frame)
            assert abs(got - expected) < 3.0, (
                f"frame {index}: disc centre at x={got:.1f}, source drew it at "
                f"x={expected:.1f}"
            )
        print("PASS: a partial-extent GIF composites identically through the mp4 route")
    finally:
        gif.close()


def check_frame_content_survives_the_route() -> None:
    """(i) The H1 pin, and the one leg that would have caught the shipped
    defect: per-INDEX content parity through the promoted mp4, on a GIF
    whose frames mix disposal 2 (restore to background, full extent) with
    disposal 1 (leave in place, sub-canvas extent).

    Every frame's disc must appear where the SOURCE drew it -- an
    independently computed position, not a second decode of the same file,
    so this cannot pass by comparing a compositor to itself. It fails on
    two distinct regressions:
      * a different compositor (FFmpeg's disposal handling smears or blanks
        the disposed region -- the disc lands somewhere else or dissolves);
      * an off-by-one in the timeline-index -> reader-index mapping, which
        moves the disc a whole DISC_STEP (14 px on screen, ~5x tolerance).
    """
    n = 6
    path = _make_gif("disposal_extents.gif", opaque=True, durations_ms=[100] * n,
                     size=(200, 200), disposal=[2, 1, 1, 2, 1, 1])
    # Fixture sanity: the file really does carry sub-canvas frame extents.
    source = Image.open(path)
    extents = []
    for index in range(n):
        source.seek(index)
        extents.append(source.tile[0].extents)
    assert any(e != (0, 0, 200, 200) for e in extents), (
        f"fixture sanity: expected some sub-canvas frame extents, got {extents}"
    )

    gif = _decode(path)
    try:
        cache = gif.video_cache
        assert cache is not None, "fixture sanity: this GIF must take the video route"
        assert cache.is_cache_complete(), (
            "the frames under test must come from the PROMOTED mp4, not a live decode"
        )
        assert cache.cap is None, (
            "no GIF reader may hold a capture on the SOURCE -- that is the second "
            "compositor this design exists to keep out"
        )
        scale = cache.out_size[0] / 200
        for index in range(n):
            expected = (10 + index * DISC_STEP + DISC_SIZE / 2) * scale
            got = _disc_centroid_x(gif._frame_at(index))
            assert abs(got - expected) < 3.0, (
                f"frame {index}: disc centre at x={got:.1f}, source drew it at "
                f"x={expected:.1f} (one frame step is {DISC_STEP * scale:.1f}px)"
            )
        print("PASS: disposal/extent compositing and frame indices survive the mp4 route")
    finally:
        gif.close()


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


def check_close_waits_for_an_inflight_fetch() -> None:
    """(l) The _close_lock's whole job, on a real thread. A media tick and a
    page teardown genuinely race, and releasing the reader while a decode is
    in flight is how InputVideo's leak happened: the fetch resurrects a
    capture on an object nobody will ever close again.

    Drops the lock and this leg fails twice over -- the reader is observed
    closed underneath the in-flight fetch, and close() no longer waits."""
    path = _make_gif("close_race.gif", opaque=True, durations_ms=[100] * 4)
    gif = _decode(path)
    cache = gif.video_cache
    assert cache is not None, "fixture sanity: this GIF must take the video route"

    entered = threading.Event()
    observed = {}
    real_get_frame = cache.get_frame

    def slow_get_frame(index):
        entered.set()
        time.sleep(0.3)
        # Read straight off the reader: release() -> close() nulls this.
        observed["released_underneath"] = cache._cache_cap is None
        return real_get_frame(index)

    cache.get_frame = slow_get_frame
    fetched = {}

    def tick():
        fetched["frame"] = gif._video_frame(0)

    ticker = threading.Thread(target=tick, name="gif-tick", daemon=True)
    ticker.start()
    assert entered.wait(10), "the fetch thread never started"

    started = time.monotonic()
    gif.close()
    close_seconds = time.monotonic() - started
    ticker.join(10)
    assert not ticker.is_alive(), "the fetch thread never finished"

    assert observed.get("released_underneath") is False, (
        "close() released the reader while a frame fetch was in flight -- the "
        "fetch was left decoding a closed capture"
    )
    assert close_seconds > 0.15, (
        f"close() returned in {close_seconds * 1000:.0f}ms without waiting for the "
        f"in-flight fetch (it must block on _close_lock)"
    )
    assert fetched["frame"] is not None, "the in-flight fetch must still complete"
    print("PASS: close() waits for an in-flight frame fetch instead of racing it")


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


def check_odd_geometry_is_clamped_even() -> None:
    """(j) mp4v silently rounds odd dimensions DOWN (a 133x144 writer
    produces 132x144 frames), which would leave every payload a pixel off
    the geometry that was asked for -- so the tile size is the contained
    size rounded to even, floored at 2. Untested, this clamp can be deleted
    or inverted with the whole suite still green."""
    assert tile_video_size((133, 144), BUDGET) == (132, 144), "odd width must round down"
    assert tile_video_size((21, 21), BUDGET) == (20, 20), "both axes round down"
    assert tile_video_size((3, 5), BUDGET) == (2, 4), "a tiny source still rounds down"
    assert tile_video_size((1000, 3), BUDGET) == (144, 2), (
        "a 1px axis has no even value below it, so it clamps UP to 2 rather than "
        "producing an unwritable 0-height video"
    )

    odd = _decode(_make_gif("odd_opaque.gif", opaque=True, durations_ms=[100, 100, 100],
                            size=(133, 144)))
    tiny = _decode(_make_gif("tiny_opaque.gif", opaque=True, durations_ms=[100, 100],
                             size=(3, 5)))
    try:
        assert odd.video_cache.out_size == (132, 144), (
            f"the 133x144 source must build at 132x144, got {odd.video_cache.out_size}"
        )
        assert odd.get_next_frame(now=1_000_000.0).size == (132, 144), (
            "the served payload must match the built geometry exactly -- an odd "
            "request would leave it a pixel short of what was asked for"
        )
        assert tiny.video_cache.out_size == (2, 4), (
            f"the 3x5 source must build at 2x4, got {tiny.video_cache.out_size}"
        )
        assert tiny.get_next_frame(now=1_000_000.0).size == (2, 4), (
            "even a 2x4 tile video must decode back at the size it was written"
        )
        print("PASS: odd and tiny geometries land on the even clamp mp4v needs")
    finally:
        odd.close()
        tiny.close()


def check_warm_construction_serves_the_promoted_file() -> None:
    """(k) The steady state, and the page-load win: once the artifact
    exists, a KeyGIF for the same (source, size, saturation) must attach to
    the PROMOTED file and composite not a single frame.

    Enforced with a tripwire rather than a stopwatch -- gif_frame_walk is
    the only compositor in the app, so replacing it with a raise proves the
    warm path never decodes a pixel."""
    path = _make_gif("warm_route.gif", opaque=True, durations_ms=[100, 120, 140, 160])
    cold = _decode(path)
    out_size = cold.video_cache.out_size
    delays = list(cold.frame_delays)
    cold.close()  # refcount 0: the entry is dropped, the FILE stays

    key = mp4_tile_cache._registry_key(path, out_size, 1.0)
    assert mp4_tile_cache._registry.get(key) is None, (
        "fixture sanity: the registry entry must be gone before the warm load"
    )

    # Patched on gif_pipeline, the module KeyGIF._composited_walk resolves the
    # name from: a stand-in installed anywhere else would leave the real
    # compositor reachable and the tripwire would never fire.
    original_walk = gif_pipeline.gif_frame_walk

    def _no_compositing(*args, **kwargs):
        raise AssertionError(
            "a warm KeyGIF composited a frame -- the artifact on disk IS the "
            "decode it is supposed to replace"
        )

    gif_pipeline.gif_frame_walk = _no_compositing
    try:
        warm = _decode(path)
    finally:
        gif_pipeline.gif_frame_walk = original_walk

    try:
        cache = warm.video_cache
        assert cache is not None and warm.frames == [], (
            "a warm construction must attach to the tile cache, not decode"
        )
        assert cache.is_cache_complete(), (
            "the warm reader must be serving the promoted cache file"
        )
        assert cache._cache_cap is not None and cache.cap is None, (
            "the warm reader must hold a capture on the CACHE and none on the source"
        )
        entry = mp4_tile_cache._registry.get(key)
        assert entry is not None and entry.ready, (
            f"the rediscovered entry must be marked ready, got {entry!r}"
        )
        assert entry.builder_thread is None, (
            "no builder may ever start for a GIF -- FFmpeg must not demux one"
        )
        assert warm.frame_delays == delays, (
            f"the warm timeline must match the cold one: {warm.frame_delays} != {delays}"
        )
        frame = warm.get_next_frame(now=1_000_000.0)
        assert frame is not None and frame.size == out_size, (
            f"the warm reader must serve real frames at {out_size}, got "
            f"{None if frame is None else frame.size}"
        )

        # A second live consumer shares the entry rather than rebuilding.
        shared = _decode(path)
        try:
            assert entry.refcount == 2, (
                f"two keys on one GIF must share one registry entry, got refcount "
                f"{entry.refcount}"
            )
        finally:
            shared.close()
        print("PASS: a warm KeyGIF attaches to the promoted file without compositing")
    finally:
        warm.close()


def check_budget_env_contract() -> None:
    """(f) DECKARD_GIF_KEY_BUDGET_MB follows the house env contract
    (native_tile_cache.native_tile_cache_max_bytes): a malformed value
    degrades to the default with a warning instead of raising out of a page
    load, including the values float() accepts but int() cannot take. 0 and
    negatives disable the RAM route entirely.

    The warnings are asserted, not assumed: the malformed one fires ONCE per
    distinct value (it is read per GIF key per page load, so a per-read
    warning floods the log), and a sub-1-MiB budget -- under a single fitted
    frame, so every alpha GIF in the app silently loses its transparency --
    must announce itself rather than degrade in silence."""
    from src.backend.DeckManagement.DeckController import (
        GIF_KEY_BUDGET_MB, gif_key_budget_bytes,
    )

    default = GIF_KEY_BUDGET_MB * 1024 * 1024
    previous = os.environ.get("DECKARD_GIF_KEY_BUDGET_MB")
    warnings: list[str] = []
    sink_id = log.add(lambda message: warnings.append(str(message)), level="WARNING")
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

        # The malformed value the app has never seen warns exactly once, no
        # matter how many keys read it.
        warnings.clear()
        os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = "thirty-two"
        for _ in range(5):
            assert gif_key_budget_bytes() == default
        malformed_warnings = [w for w in warnings if "thirty-two" in w]
        assert len(malformed_warnings) == 1, (
            f"a malformed budget must warn ONCE per distinct value, got "
            f"{len(malformed_warnings)}: {malformed_warnings}"
        )
        assert "DECKARD_GIF_KEY_BUDGET_MB" in malformed_warnings[0], (
            f"the warning must name the knob: {malformed_warnings[0]}"
        )

        # A sub-1-MiB budget is legal but app-wide alpha loss: say so.
        warnings.clear()
        os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = "0.25"
        for _ in range(3):
            assert gif_key_budget_bytes() == 262144
        small_warnings = [w for w in warnings if "0.25" in w]
        assert len(small_warnings) == 1, (
            f"a sub-1-MiB budget must warn once, got {len(small_warnings)}: "
            f"{small_warnings}"
        )
        assert "transparency" in small_warnings[0], (
            f"the warning must say what it costs: {small_warnings[0]}"
        )
    finally:
        log.remove(sink_id)
        if previous is None:
            os.environ.pop("DECKARD_GIF_KEY_BUDGET_MB", None)
        else:
            os.environ["DECKARD_GIF_KEY_BUDGET_MB"] = previous
    print("PASS: the GIF key budget env var degrades, and says so exactly once")


def check_over_budget_alpha_degrades_to_the_video_route() -> None:
    """(g) An alpha GIF over the per-GIF budget degrades to the bounded
    route -- alpha is dropped, the key keeps playing, and the footprint
    stays bounded (the same ladder GifBackground walks down to cv2). The
    census must follow the outcome: no gif_frames registration for a GIF
    that never built a frame list.

    That artifact is written under its own cache VARIANT, so it can never be
    mistaken later for proof that the GIF was opaque: the same file at the
    default budget must get its transparency back, which is what the second
    half of this leg pins."""
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
        # The documented cost of the degrade: an mp4 has no alpha channel.
        assert frame.mode == "RGB", (
            f"the video route serves opaque frames, got mode {frame.mode}"
        )
        assert gif.video_cache.cache_path.endswith(f"{BOUNDED_TILE_VARIANT}.mp4"), (
            f"the alpha-dropped artifact must live under its own cache variant, got "
            f"{gif.video_cache.cache_path}"
        )
        assert os.path.isfile(gif.video_cache.cache_path), (
            "the bounded artifact must actually be on disk"
        )

        # Under the default budget the very same GIF keeps its frame list --
        # the bounded artifact must not shadow the lossless decision.
        unbudgeted = _decode(path)
        try:
            assert unbudgeted.video_cache is None and len(unbudgeted.frames) == 5, (
                "at the default budget this GIF must stay on the frame list -- "
                "otherwise the degrade above proved nothing, and a one-off small "
                "budget would cost this GIF its alpha forever"
            )
        finally:
            unbudgeted.close()
        print("PASS: an over-budget alpha GIF degrades to the bounded video route")
    finally:
        gif.close()


def check_cache_videos_off_keeps_every_gif_in_ram() -> None:
    """(m) With performance.cache-videos off there is no artifact to route
    to, so every GIF -- opaque included -- keeps its frame list, exactly as
    it did before this issue, and no GIF touches the registry.

    Reproduced before the fix: an opaque GIF acquired a reader, no builder
    was started, and the reader's own end-of-source path released the source
    capture -- after one pass every later frame request returned the same
    payload forever. This walks three full loops and demands distinct
    pixels."""
    app_settings = gl.settings_manager.get_app_settings()
    app_settings.setdefault("performance", {})["cache-videos"] = False
    try:
        path = _make_gif("cache_off.gif", opaque=True, durations_ms=[100] * 8)
        gif = _decode(path)
    finally:
        app_settings["performance"]["cache-videos"] = True
    try:
        assert gif.video_cache is None, (
            "with the video cache disabled a GIF must never attach a reader"
        )
        assert len(gif.frames) == 8, (
            f"every frame must be retained instead, got {len(gif.frames)}"
        )
        key = mp4_tile_cache._registry_key(path, tile_video_size((200, 200), BUDGET), 1.0)
        assert mp4_tile_cache._registry.get(key) is None, (
            "a GIF must not appear in the tile registry at all in this config"
        )

        T0 = 1_000_000.0
        payloads = []
        indices = []
        # 8 frames x 100ms = 0.8s per loop; three loops at 50ms steps.
        for step in range(48):
            frame = gif.get_next_frame(now=T0 + step * 0.05)
            assert frame is not None, f"playback stopped at step {step}"
            indices.append(gif.active_frame)
            payloads.append(hash(frame.tobytes()))
        for loop in range(3):
            visited = sorted(set(indices[loop * 16:(loop + 1) * 16]))
            assert visited == list(range(8)), (
                f"loop {loop + 1} did not visit every frame (froze?): {visited}"
            )
        assert len(set(payloads)) == 8, (
            f"playback served {len(set(payloads))} distinct images over three loops "
            f"-- a frozen key repeats one forever"
        )
        print("PASS: with cache-videos off every GIF stays in RAM and keeps animating")
    finally:
        gif.close()


def main() -> None:
    fixtures.install_stub_globals({"performance": {"cache-videos": True}})
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_gif_opaque_route")
    check_opaque_gif_routes_to_the_tile_cache()
    check_alpha_gif_stays_on_the_frame_list()
    check_declared_but_opaque_gif_takes_the_video_route()
    check_both_routes_pick_the_same_frames()
    check_frame_content_survives_the_route()
    check_partial_extent_canvas_survives_the_route()
    check_close_releases_the_reader()
    check_close_waits_for_an_inflight_fetch()
    check_video_route_geometry_matches_the_frame_list()
    check_odd_geometry_is_clamped_even()
    check_warm_construction_serves_the_promoted_file()
    check_budget_env_contract()
    check_over_budget_alpha_degrades_to_the_video_route()
    check_cache_videos_off_keeps_every_gif_in_ram()
    print("PASS: scenario_gif_opaque_route")


if __name__ == "__main__":
    main()
