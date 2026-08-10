"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

---

The GIF pipeline: the one PIL compositor in the app, the budget ladder that
bounds what a GIF may retain, and the two providers built on them --
GifBackground (a deck or strip canvas) and KeyGIF (one key).

A leaf of the deck_controller package: it imports nothing from its siblings,
so the compositor can never come to depend on the controller it renders for.
Everything here reads geometry and saturation off duck-typed owners, which
is why the type-only imports below are the whole of its knowledge about them.
"""
import bisect
import contextlib
import itertools
import math
import os
import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageEnhance, ImageOps, ImageSequence
from loguru import logger as log

from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.Subclasses import mp4_tile_cache
from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import get_video_md5

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
    from src.backend.PageManagement.Page import Page


#: extend_touchscreen implies the strip geometry was computed in __init__.
#: If that ever breaks, raising keeps Background.update_tiles' contract --
#: previous tiles retained, one rate-limited log -- instead of publishing a
#: key-only tile set that would leave the strip frozen and silent.
_STRIP_GEOMETRY_MISSING = (
    "extend_touchscreen is set but the strip geometry was never computed"
)


class GifBudgetExceeded(Exception):
    """Raised by decode_gif_frames when the estimated decoded footprint
    exceeds the caller's budget, BEFORE any frame is decoded -- callers fall
    back to a bounded path (GifBackground -> the existing cv2/mp4 pipeline)
    instead of risking an OOM on a pathological many-frame GIF."""


# RAM ceiling for a fully-decoded GIF background. One constant,
# no new cache layer: GifBackground estimates n_frames x W x H x 4 against it
# at open and falls back to the opaque cv2 path when over. 128MB covers
# ~200 canvas frames on an SD+ (~600KB each) / ~90 on an XL (~1.4MB each) --
# generous for the looping decorations this feature targets, small next to
# what the old unbounded cv2 canvas pool used to swallow (mem-plan §3).
GIF_BG_BUDGET_MB = 128

# Per-GIF ceiling for a KEY's retained RGBA frame list. Only
# alpha-carrying GIFs build one at all -- opaque ones play off the mp4 tile
# cache at O(1) RAM -- so this bounds the exception, not the common case.
# 32MB is ~220 frames at 2x an SD+ tile / ~110 at 2x an XL tile: far beyond
# the looping badges and spinners this feature targets, and small enough that
# a pathological page cannot outweigh the whole image-cache budget by itself.
GIF_KEY_BUDGET_MB = 32

# Cache-file variant for the over-budget ladder's artifact (see
# KeyGIF._cold_streaming_walk). Its frames have had alpha flattened away, so
# it must never be read back by a GIF that is now UNDER budget and could
# have kept its transparency -- a separate variant keeps the two renderings
# from ever sharing a file.
BOUNDED_TILE_VARIANT = ".bounded"

# Raw DECKARD_GIF_KEY_BUDGET_MB values already warned about, so a bad (or
# pathologically small) setting costs ONE log line per distinct value for the
# life of the process instead of one per GIF key per page load. Same
# once-per-distinct-value discipline as cache_budget._warned_ceiling_values.
_warned_gif_budget_values: "set[str]" = set()


def gif_key_budget_bytes() -> int:
    """Byte ceiling for one key GIF's retained frame list, from
    DECKARD_GIF_KEY_BUDGET_MB. 0 (or a negative value) disables the frame
    list entirely: every GIF then plays off the mp4 tile cache, trading alpha
    for a hard bound.

    A malformed value degrades to the default with a warning, per the
    house contract (native_tile_cache.native_tile_cache_max_bytes): this is
    read during page load, where an exception would surface as a key that
    silently lost its media -- a tuning knob typo must never cost content.
    "Malformed" includes the values float() ACCEPTS but int() cannot take
    ("nan", "inf", "1e400"): they parse, survive the sign test (every nan
    comparison is False), then raise from int().

    A sub-1-MiB budget also warns: it is smaller than a SINGLE fitted frame
    at any tile size, so every alpha GIF in the app silently loses its
    transparency to the bounded route. That is a legitimate setting (the
    knob is "bound this hard"), but it is never what a typo'd "0.5" meant."""
    raw = os.environ.get("DECKARD_GIF_KEY_BUDGET_MB")
    if raw is None:
        return GIF_KEY_BUDGET_MB * 1024 * 1024
    try:
        mb = float(raw)
        usable = math.isfinite(mb)
    except ValueError:
        usable = False
    if not usable:
        if raw not in _warned_gif_budget_values:
            _warned_gif_budget_values.add(raw)
            log.warning(
                f"Ignoring malformed DECKARD_GIF_KEY_BUDGET_MB={raw!r}; "
                f"using the default {GIF_KEY_BUDGET_MB}"
            )
        return GIF_KEY_BUDGET_MB * 1024 * 1024
    if mb < 0:
        return 0
    if 0 < mb < 1 and raw not in _warned_gif_budget_values:
        _warned_gif_budget_values.add(raw)
        log.warning(
            f"DECKARD_GIF_KEY_BUDGET_MB={raw!r} is under 1 MiB -- smaller than one "
            f"fitted GIF frame, so EVERY alpha-carrying GIF key will drop its "
            f"transparency for the bounded mp4 route"
        )
    return int(mb * 1024 * 1024)


def contained_size(source_size: "tuple[int, int]", max_size: "tuple[int, int]") -> "tuple[int, int]":
    """The dimensions ImageOps.contain lands on: aspect-preserving,
    SHRINK-ONLY (a source already inside max_size keeps its own size --
    upscaling would multiply retained memory for zero display benefit,
    mem-plan P2.3).

    "Lands on" to within a pixel, not bit-for-bit: ImageOps.contain rounds
    each axis independently (round(w * scale) with its own recomputed
    scale), so the two disagree by 1 px on one axis for a small minority of
    extreme aspect ratios (94 of 1.1M source/target pairs surveyed). The
    routes below share THIS function, so they cannot drift from each other;
    the residual is a 1-px difference from what a raw ImageOps.contain call
    elsewhere would produce.

    Shared by the frame-list decode (its pre-decode budget estimate) and the
    video route (the tile size it asks the mp4 registry to build), so a GIF's
    geometry cannot depend on which route it took."""
    src_w, src_h = source_size
    max_w, max_h = max_size
    if src_w <= max_w and src_h <= max_h:
        return src_w, src_h
    scale = min(max_w / src_w, max_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def tile_video_size(source_size: "tuple[int, int]", max_size: "tuple[int, int]") -> "tuple[int, int]":
    """The tile-cache geometry for a source that would be CONTAINED into
    max_size -- i.e. contained_size(), rounded down to even dimensions (and
    never below 2).

    Even because mp4v silently rounds odd dimensions down (a 133x144 writer
    produces 132x144 frames), which would leave every payload a pixel off
    the geometry that was asked for; the >= 2 floor because a 1-px axis has
    no even value below it. So a cached tile can be up to 1 px smaller per
    axis than the retained frame list's, and a pathologically thin source
    (3x100 -> 2x100) loses a third of its width -- accepted: the alternative
    is no cache at all for those, and compositing scales per tick anyway.

    Shared by every KeyGIF route so a GIF's geometry cannot depend on which
    one it took."""
    out_w, out_h = contained_size(source_size, max_size)
    return max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)


def normalize_gif_delay(raw: "int | None") -> int:
    """One frame's GIF `duration` metadata -> the delay actually played, in
    ms, normalized the browser way (Firefox/Chrome): missing or < 20ms ->
    100ms, anything else trusted as-is.

    The old "< 50 -> x10 centiseconds" heuristic played legitimate fast GIFs
    (40ms == 25fps) 10x too slow, and an all-zero-duration GIF made the
    cumulative timeline degenerate and froze on frame 0 (scenario_gif_delays
    pins both). One definition, shared by the full decode and the
    header-only probe, so the two can never disagree about a timeline."""
    if raw is None or raw < 20:
        return 100
    return raw


def cumulative_gif_delays(delays_ms: "list[int]") -> "list[float]":
    """Delay list (ms) -> cumulative timeline in seconds, where element i is
    the wall-clock time at which frame i's display window ENDS. Picking a
    frame for elapsed time t is then a single bisect instead of a per-tick
    increment-and-compare loop (KeyGIF.get_next_frame /
    GifBackground._pick_frame index it directly)."""
    return list(itertools.accumulate(d / 1000.0 for d in delays_ms))


@dataclass(frozen=True, slots=True)
class GifTimeline:
    """A GIF's playback timeline and geometry, with NO decoded frame
    retained: the frame count, the per-frame delays, the cumulative
    wall-clock edges, and the source dimensions.

    This is what a KeyGIF needs when the pixels come from somewhere else --
    a tile cache PIL already wrote on an earlier page load.
    Timing authority always lives here, never in the video container."""
    n_frames: int
    frame_delays: "list[int]"
    cum_delays: "list[float]"
    size: "tuple[int, int]"


def gif_header_geometry(path: str) -> "tuple[int, tuple[int, int]]":
    """(frame count, canvas size) from the GIF's headers alone -- no frame
    decoded, no seek. Enough to price a decode before paying for it."""
    gif = Image.open(path)
    try:
        return getattr(gif, "n_frames", 1), gif.size
    finally:
        gif.close()


def probe_gif_timeline(path: str) -> GifTimeline:
    """The playback timeline of the GIF at `path`, retaining NOTHING.

    Cost note: the frame count and the size come from headers alone, but PIL
    only exposes a frame's `duration` after seeking to it, and seeking
    composes the previous frame (GifImagePlugin._seek -> load()). So this
    walk does run the decoder -- what it never does is convert, fit,
    saturate or KEEP anything, which is where both the memory and the bulk
    of the time go: measured on a 200-frame 500x500 GIF, ~49 ms and O(1) RAM
    here against ~320 ms and the whole retained frame list for
    decode_gif_frames.

    Raises whatever PIL raises on a corrupt/truncated file -- callers treat
    that exactly like a failed decode (fail soft to the opaque video path)."""
    gif = Image.open(path)
    try:
        size = gif.size
        n_frames = getattr(gif, "n_frames", 1)
        delays_ms: "list[int]" = []
        for index in range(n_frames):
            gif.seek(index)
            delays_ms.append(normalize_gif_delay(gif.info.get("duration")))
    finally:
        gif.close()

    return GifTimeline(
        n_frames=len(delays_ms),
        frame_delays=delays_ms,
        cum_delays=cumulative_gif_delays(delays_ms),
        size=size,
    )


def frame_has_alpha(frame: Image.Image) -> bool:
    """Does this RENDERED frame actually carry transparency?

    The exact test, not the declared one. A GIF's header transparency index
    says almost nothing about the composited result: a survey of 396 real
    animated GIFs found 75% declaring a transparent index on frame 0 and
    only 11% ever rendering a pixel with alpha < 255 -- delta-encoding
    against the previous frame (disposal 1) declares the index purely to
    mean "leave this pixel alone". Routing on the declaration therefore
    stranded ~64% of real GIFs on the expensive path for nothing, which is
    why v2 asks the pixels instead.

    One C-level extrema pass per frame (min alpha over the band); the caller
    stops asking once the answer is yes."""
    if frame.mode != "RGBA":
        return False
    extrema = frame.getextrema()
    return len(extrema) >= 4 and extrema[3][0] < 255


def gif_frame_walk(path: str, max_size: "tuple[int, int]" = None,
                   fit_size: "tuple[int, int]" = None,
                   saturation: float = 1.0):
    """Generator over one GIF's frames: PIL composites each frame onto the
    running canvas (disposal, partial extents and all), converts to RGBA,
    sizes it, bakes the saturation, and yields `(frame, delay_ms)`.

    THE one GIF compositor in the app. Everything that shows GIF pixels --
    the retained frame list, GIF backgrounds, and the tile mp4 KeyGIF writes
    for an opaque GIF -- consumes this walk, so no two of them can disagree
    about what frame N looks like (the review found FFmpeg's own GIF demuxer
    disagreeing with PIL on 7 of 15 frames of a stock file).

    Sizing -- exactly one of the two, or neither:
      * max_size: shrink-only ImageOps.contain. A frame larger than this is
        contained (aspect preserved); a smaller one keeps its own size --
        upscaling a small GIF would multiply retained memory for zero
        display benefit (KeyGIF's 2x-tile policy, mem-plan P2.3).
      * fit_size: every frame is ImageOps.fit to EXACTLY this size.
        Backgrounds must fill the canvas so per-key crop coordinates hold --
        the same fill contract as the cv2 background cache's re-encode.

    The source handle is closed when the walk finishes OR when the consumer
    abandons it (generator close), so no fd or full-res PIL frame cache
    outlives the pass."""
    # The source file is only needed for the duration of the walk -- close it
    # as soon as it ends so the app doesn't hold a dangling fd + full-res
    # frame cache alive underneath the fitted copies a caller keeps.
    gif = Image.open(path)
    try:
        for frame in ImageSequence.Iterator(gif):
            decoded = frame.convert("RGBA")
            if fit_size is not None:
                if decoded.size != fit_size:
                    decoded = ImageOps.fit(decoded, fit_size, Image.Resampling.LANCZOS)
            elif max_size is not None and (decoded.width > max_size[0] or decoded.height > max_size[1]):
                decoded = ImageOps.contain(decoded, max_size)
            if abs(saturation - 1.0) > 0.001:
                decoded = ImageEnhance.Color(decoded).enhance(saturation)
            yield decoded, normalize_gif_delay(gif.info.get('duration'))
    finally:
        gif.close()


def decode_gif_frames(path: str, max_size: "tuple[int, int]" = None,
                      fit_size: "tuple[int, int]" = None,
                      saturation: float = 1.0,
                      budget_bytes: int = None) -> "tuple[list[Image.Image], list[int], list[float]]":
    """Every frame of the GIF at `path`, decoded to RGBA and retained, plus
    its delay timeline -- gif_frame_walk() materialized. GifBackground's
    entry point (KeyGIF drives the walk itself so it can decide, frame by
    frame, whether to keep what it is looking at).

    budget_bytes: pre-decode gate -- the retained footprint is estimated
    from the header (n_frames x out_w x out_h x 4) and GifBudgetExceeded is
    raised before decoding when it would exceed the budget.

    Returns (frames_rgba, frame_delays_ms, cum_delays) where cum_delays[i]
    is the wall-clock second at which frame i's display window ENDS (the
    callers' bisect timelines index it directly).
    """
    if budget_bytes is not None:
        n_frames, (out_w, out_h) = gif_header_geometry(path)
        if fit_size is not None:
            out_w, out_h = fit_size
        elif max_size is not None:
            # The dims ImageOps.contain would land on (shared with the
            # video route's tile size -- see contained_size).
            out_w, out_h = contained_size((out_w, out_h), max_size)
        estimate = n_frames * out_w * out_h * 4
        if estimate > budget_bytes:
            raise GifBudgetExceeded(
                f"{path}: ~{estimate / (1024 * 1024):.1f}MB decoded "
                f"({n_frames} frames at {out_w}x{out_h} RGBA) exceeds the "
                f"{budget_bytes / (1024 * 1024):.1f}MB budget"
            )

    frames: "list[Image.Image]" = []
    delays_ms: "list[int]" = []
    with contextlib.closing(gif_frame_walk(path, max_size=max_size, fit_size=fit_size,
                                           saturation=saturation)) as walk:
        for frame, delay in walk:
            frames.append(frame)
            delays_ms.append(delay)

    return frames, delays_ms, cumulative_gif_delays(delays_ms)


class GifBackground:
    """RGBA GIF provider for deck/strip backgrounds.

    Satisfies the contract BackgroundVideo (the cv2/mp4 canvas cache)
    exposes to the compositor -- get_next_tiles() -> (entries, identity)
    with the strip slice as one extra entry when extended, plus the
    video_path/extend_touchscreen/saturation/page/fps/loop attributes the
    prebuild keep-check, the media tick, and the screensaver setters read --
    but decodes through PIL so alpha and the per-frame delay timeline
    survive (cv2's GIF demuxer drops both).

    Frames are decoded once at construction, fitted to exactly the deck
    canvas (per-key crop coordinates must hold), and owned by this object:
    same lifetime as BackgroundVideo, freed with the background swap
    (Background.set_image/set_video close the old provider). No global
    cache, no registry -- two decks showing the same GIF decode it twice
    (accepted v1 trade-off; the mp4 tile cache's refcounted registry stays
    the only one). Construction raises GifBudgetExceeded before decoding
    when the estimated footprint exceeds GIF_BG_BUDGET_MB; callers fall
    back to the existing opaque cv2 path.

    canvas_size overrides the deck-canvas geometry for the strip-background
    route (ControllerTouchScreenState._get_background_video_frame): frames
    are fitted to the strip and served whole via get_next_frame() -- the
    per-touchscreen composite alpha_composites the frame, so the exact-size
    RGBA decode is load-bearing there.
    """

    def __init__(self, deck_controller: "DeckController", gif_path: str, loop: bool = True,
                 fps: int = 30, extend_touchscreen: bool = False,
                 canvas_size: "tuple[int, int] | None" = None) -> None:
        self.deck_controller = deck_controller
        self.video_path = gif_path
        self.loop = loop
        self.fps = fps

        self.page: Page | None = deck_controller.active_page
        self.saturation = deck_controller.get_display_saturation()

        deck = deck_controller.deck
        self.extend_touchscreen = extend_touchscreen and deck.is_touch()

        # Both stay None unless extend_touchscreen is on -- the strip geometry
        # only exists for the extended canvas. Every read is paired with that
        # flag; the None-guards at those reads make the pairing checkable.
        self.strip_size: "tuple[int, int] | None" = None
        self._strip_box: "tuple[int, int, int, int] | None" = None
        if canvas_size is None:
            # Same canvas/crop geometry as BackgroundVideoCache -- computed
            # once into plain boxes here since the frame list is fixed after
            # decode (no per-call geometry methods needed).
            key_rows, key_cols = deck.key_layout()
            self.key_count = deck.key_count()
            key_w, key_h = deck.key_image_format()['size']
            spacing_x, spacing_y = deck_controller.key_spacing

            canvas_w = key_w * key_cols + spacing_x * (key_cols - 1)
            canvas_h = key_h * key_rows + spacing_y * (key_rows - 1)

            self._key_regions: "list[tuple[int, int, int, int]]" = []
            for key in range(self.key_count):
                row, col = divmod(key, key_cols)
                x = col * (key_w + spacing_x)
                y = row * (key_h + spacing_y)
                self._key_regions.append((x, y, x + key_w, y + key_h))

            if self.extend_touchscreen:
                # Extend the canvas below the key grid so the frame
                # continues onto the touchscreen strip: one bezel gap plus
                # the strip mapped into canvas coordinates (same geometry as
                # BackgroundImage/BackgroundVideoCache).
                self.strip_size = deck_controller.get_touchscreen_image_size()
                strip_canvas_h = round(self.strip_size[1] * canvas_w / self.strip_size[0])
                canvas_h += spacing_y + strip_canvas_h
                self._strip_box = (0, canvas_h - strip_canvas_h, canvas_w, canvas_h)
            canvas_size = (canvas_w, canvas_h)
        else:
            # Strip-background mode: whole-frame service only.
            self.key_count = 0
            self._key_regions = []
            self.extend_touchscreen = False

        self.canvas_size = canvas_size

        self.frames, self.frame_delays, self._cum_delays = decode_gif_frames(
            gif_path, fit_size=canvas_size, saturation=self.saturation,
            budget_bytes=GIF_BG_BUDGET_MB * 1024 * 1024,
        )
        self._total_delay: float = self._cum_delays[-1] if self._cum_delays else 0.0

        # Frame identity for the passthrough-key native-encode memo
        # (Background.get_identified_tile consumers): (md5, frame index),
        # the exact BackgroundVideo contract, so steady-state loop playback
        # is a dict lookup + USB write per key.
        self.video_md5 = get_video_md5(gif_path)

        self.active_frame: int = -1
        # Wall-clock timeline state -- KeyGIF.get_next_frame's arithmetic
        # (see _pick_frame).
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None
        # (frame index, entries) of the last cropped frame: at loop FPS most
        # ticks land on the frame already cut (a 10fps GIF under a 30Hz tick
        # re-uses each crop set ~3x). Handed out as copies either way.
        self._tiles_memo: tuple = (None, None)

    def _pick_frame(self, now: float = None) -> int:
        """Wall-clock frame index for `now`: bisect over the cumulative
        delay timeline plus the away-gap clamp -- KeyGIF.get_next_frame's
        arithmetic kept in lockstep (see that method's comments for the
        rationale on each branch)."""
        # Snapshot the timeline once: close() swaps frames/_cum_delays/
        # _total_delay from another thread mid-call (deck route: GTK/
        # screensaver swap racing the media tick). Locals keep the guard and
        # the arithmetic looking at the SAME generation -- a racer can't
        # land a zero modulo or an emptied-list index between them. (The
        # caller indexes its own frames snapshot; every index returned here
        # comes from either the shared full timeline or the 0 fallback, so
        # it stays in range for that snapshot too.)
        cum = self._cum_delays
        total = self._total_delay
        n = len(self.frames)
        if n <= 1 or total <= 0 or not cum:
            self.active_frame = 0
            return 0

        if now is None:
            now = time.time()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            self._play_start += (now - self._last_frame_tick) - cum[0]
        self._last_frame_tick = now

        elapsed = now - self._play_start
        t = elapsed % total if self.loop else min(elapsed, total)

        frame = bisect.bisect_right(cum, t)
        if frame >= n:
            frame = n - 1  # float-edge / non-loop clamp landing on t == total
        self.active_frame = frame
        return frame

    def get_next_tiles(self) -> "tuple[list[Image.Image], tuple | None]":
        """(entries, identity) for the frame this tick lands on --
        BackgroundVideo.get_next_tiles's contract: key tiles, plus the strip
        slice as one extra entry when extended; identity is (md5, frame
        index). Crops are cut straight from the retained RGBA canvas frame
        (no paste onto an opaque intermediate, so alpha reaches the
        compositor) and handed out as copies: consumers paste onto the
        tiles/strip slice in place."""
        frames = self.frames  # snapshot: close() empties this from other threads
        strip_size = self.strip_size
        strip_box = self._strip_box
        if not frames:
            entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
            if self.extend_touchscreen:
                if strip_size is None:
                    raise RuntimeError(_STRIP_GEOMETRY_MISSING)
                entries.append(Image.new("RGBA", strip_size, (0, 0, 0, 0)))
            return entries, None

        index = self._pick_frame()
        memo_index, memo_entries = self._tiles_memo
        if memo_index != index or memo_entries is None:
            frame = frames[index]
            memo_entries = [frame.crop(box) for box in self._key_regions]
            if self.extend_touchscreen:
                if strip_size is None or strip_box is None:
                    raise RuntimeError(_STRIP_GEOMETRY_MISSING)
                # Bottom slice of the extended canvas at strip resolution
                # (same crop+HAMMING resize as BackgroundVideoCache.
                # crop_strip_from_deck_sized_image).
                memo_entries.append(
                    frame.crop(strip_box).resize(strip_size, Image.Resampling.HAMMING)
                )
            self._tiles_memo = (index, memo_entries)
        return [entry.copy() for entry in memo_entries], (self.video_md5, index)

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        """The whole canvas-size RGBA frame for now (strip-background
        route). Returns the retained frame itself -- the caller's
        convert("RGBA") copies before anything pastes onto it. None once
        close() has emptied the frame list."""
        frames = self.frames
        if not frames:
            return None
        return frames[self._pick_frame(now)]

    def set_playback(self, fps: int, loop: bool) -> None:
        """fps is only the owner's render cap here (playback position is
        wall-clock over the GIF's own delay timeline, mirroring InputVideo's
        natural_speed arm) -- no timebase rebase needed."""
        self.fps = fps
        self.loop = loop

    def close(self) -> None:
        """Drops the retained frame list (the whole footprint). Safe against
        an in-flight tick: get_next_tiles/get_next_frame snapshot
        self.frames, so a racer finishes on its own reference and the list
        is reclaimed right after."""
        self.frames = []
        self.frame_delays = []
        self._cum_delays = []
        self._total_delay = 0.0
        self._tiles_memo = (None, None)


class KeyGIF(SingleKeyAsset):
    """Animated-GIF provider for one key, playing its own per-frame delay
    timeline.

    Holds its frames one of two ways:

      * OPAQUE GIF -> the shared mp4 tile registry (mp4_tile_cache): one
        refcounted cache file per (source, size, saturation), one reader
        with one decoded frame in hand. RAM is O(1) per key instead of
        O(frame count) -- the whole point of the issue, since a 200-frame
        GIF is ~29MB retained and a page of them ~0.9GiB.
      * ALPHA-CARRYING GIF -> the retained RGBA frame list, the only way to
        keep transparency (an mp4 has no alpha channel).

    Two rules make that split safe, both learned the hard way in review:

    1. PIL IS THE ONLY COMPOSITOR. The tile mp4 is written FROM PIL-
       composited frames (mp4_tile_cache.acquire_from_frames); FFmpeg never
       demuxes a GIF here. FFmpeg's own GIF compositing disagrees with PIL
       on disposal and partial-extent frames -- 7 of 15 frames on a stock
       test file, ~48% of pixels off -- so letting it build the cache
       silently changed what opaque keys looked like.
    2. THE ROUTE IS DECIDED BY RENDERED ALPHA, not by the header's
       transparency declaration: 75% of real GIFs declare it, 11% ever
       render it (see frame_has_alpha), so declaring was the wrong question
       and left ~64% of GIFs on the expensive path.

    Which means the classification costs one PIL walk the FIRST time a GIF
    is seen at a given size -- the same walk main paid unconditionally, plus
    an alpha extrema pass and (for an opaque GIF) one encode. Afterwards the
    artifact on disk IS the classification: a warm construction walks the
    delays only and attaches a reader, decoding no pixels at all.

    With `performance.cache-videos` off there is no disk cache to route to,
    so every GIF stays on the frame list and no GIF ever reaches the
    registry.

    Either way the TIMELINE is PIL's: wall-clock picking over the cumulative
    per-frame delays, so an irregularly-timed GIF plays at its own rhythm
    rather than at a video's constant fps. On the video route the picked
    index is handed to the reader instead of subscripting a list; the
    arithmetic is identical (pinned by scenario_gif_timeline /
    scenario_gif_delays)."""

    # Class-level default: get_next_frame's picking arithmetic is exercised
    # against synthetic timelines on instances built attribute-by-attribute
    # via __new__ (scenario_gif_timeline), which never touch the video route.
    video_cache = None

    def __init__(self, controller_key: "ControllerKey", gif_path: str, fps: int = 30, loop: bool = True):
        super().__init__(controller_key)
        self.gif_path = gif_path
        self.fps = fps
        self.loop = loop

        self.active_frame: int = -1
        # Wall-clock timeline state (presenter-migration-plan.md §4 M4):
        # mirrors BackgroundVideo/InputVideo's wall-clock picking, but keyed
        # against a cumulative-delay timeline instead of a fixed fps, since
        # GIF frame durations are per-frame and often irregular.
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None

        # Serializes close() against an in-flight frame fetch on the video
        # route -- InputVideo._close_lock's contract: a
        # get_frame() that starts after release() can resurrect a capture
        # through _maybe_adopt_shared_cache and leak it.
        self._close_lock = threading.Lock()

        self.frames: "list[Image.Image]" = []
        self._frames_bytes = 0

        # mem-plan P2.3: cap frame size at 2x the key tile instead of source
        # resolution -- a 500px/200-frame GIF is ~200MB at source res vs
        # ~46MB fitted. Composited size is decided per tick by
        # add_image_to_background/get_composed_layout (UI max is 200%,
        # ImageEditor.py), so 2x tile is the largest a frame is ever
        # displayed at. Shrink-only and aspect-preserving on BOTH routes
        # (see tile_video_size).
        tile_w, tile_h = self.deck_controller.get_key_image_size()
        fit_size = (max(1, tile_w * 2), max(1, tile_h * 2))

        # Saturation is baked in once, at decode/build time -- the frames are
        # this asset's per-frame memo (get_next_frame only picks from them),
        # so enhancing per tick would re-pay ImageEnhance forever. A
        # saturation change reloads the page, which rebuilds this object
        # under the new factor (see set_display_saturation) -- the same
        # contract as InputImage/BackgroundImage, and the registry keys its
        # cache files on the factor for the video route. Skipped entirely at
        # the default factor.
        saturation = self.deck_controller.get_display_saturation()

        self.frame_delays: "list[int]" = []
        self._cum_delays: "list[float]" = []
        self._total_delay: float = 0.0

        # No disk cache configured -> nothing to route to. Every GIF keeps
        # its frame list, and the registry
        # never sees a GIF at all: a reader with no artifact to read falls
        # back to decoding the SOURCE, which for a GIF means both FFmpeg's
        # divergent compositing and (with no builder to promote anything) a
        # capture that is released at end-of-source and then repeats its
        # last frame forever. Read once, here, so one object cannot change
        # routes mid-life.
        if not mp4_tile_cache.cache_videos_enabled():
            frames, _ = self._decode_all(fit_size, saturation)
            self._hold_frame_list(frames)
            budget = gif_key_budget_bytes()
            if budget and self._frames_bytes > budget:
                log.warning(
                    f"{self.gif_path}: {self._frames_bytes / (1024 * 1024):.1f}MB of "
                    f"GIF frames retained, over the "
                    f"{budget / (1024 * 1024):.1f}MB per-GIF budget -- the bounded "
                    f"route needs performance.cache-videos, which is disabled"
                )
            return

        # Raises on a corrupt/truncated file (the header parse), exactly
        # like the decode it precedes -- the construct site fails soft to
        # InputVideo.
        n_frames, source_size = gif_header_geometry(self.gif_path)
        out_size = tile_video_size(source_size, fit_size)

        # Which of the two artifacts this GIF is allowed to use, decided
        # before any pixel is touched: the lossless one an opaque GIF builds
        # for itself, or the alpha-dropping one the over-budget ladder
        # streams. They are separate cache variants on purpose -- sharing
        # one name would let a GIF that once streamed under a tiny budget be
        # read back forever as "proven opaque", so raising the budget could
        # never give it its transparency back. The estimate is the RETAINED
        # footprint, measured against the frame list's own geometry rather
        # than the mp4's even-rounded one.
        retained_size = contained_size(source_size, fit_size)
        estimate = n_frames * retained_size[0] * retained_size[1] * 4
        budget = gif_key_budget_bytes()
        over_budget = estimate > budget
        variant = BOUNDED_TILE_VARIANT if over_budget else ""

        # WARM: that artifact already exists, so this GIF has been walked
        # before at this size -- and, on the lossless variant, found opaque.
        # Walk the delays and attach: no frame is decoded, converted, fitted
        # or retained. This is the steady state after first sight, and the
        # page-load win.
        reader = mp4_tile_cache.attach_promoted(self.gif_path, out_size, saturation,
                                                variant=variant)
        if reader is not None:
            try:
                self._adopt_timeline(probe_gif_timeline(self.gif_path).frame_delays)
            except Exception:
                # Never leave the registry holding a reference for an object
                # whose constructor is about to raise.
                mp4_tile_cache.release(reader)
                raise
            self.video_cache = reader
            return

        # COLD: one PIL walk decides everything (see the class docstring).
        if over_budget:
            self._cold_streaming_walk(fit_size, saturation, out_size,
                                      estimate, budget, n_frames, retained_size)
        else:
            self._cold_retained_walk(fit_size, saturation, out_size)

    def _adopt_timeline(self, delays_ms: "list[int]") -> None:
        """Per-frame delays -> this object's playback timeline. One place,
        so a route can never install a timeline the others could not."""
        self.frame_delays = list(delays_ms)
        self._cum_delays = cumulative_gif_delays(self.frame_delays)
        self._total_delay = self._cum_delays[-1] if self._cum_delays else 0.0

    def _composited_walk(self, fit_size: "tuple[int, int]", saturation: float,
                         delays_out: "list[int]", alpha_out: "list[bool]"):
        """The single PIL pass v2 is built on: composite (PIL, never
        FFmpeg), fit shrink-only to 2x tile, bake saturation -- recording
        each frame's delay and the exact rendered-alpha verdict as it goes.

        Yields the frames; the caller decides whether to KEEP them (the RAM
        route, and the source for an opaque GIF's mp4) or hand them straight
        to the writer and stay O(1). The alpha check stops asking once the
        answer is yes -- a truly-alpha GIF usually settles it on frame 0."""
        with contextlib.closing(gif_frame_walk(
                self.gif_path, max_size=fit_size, saturation=saturation)) as walk:
            for frame, delay in walk:
                delays_out.append(delay)
                if not alpha_out[0] and frame_has_alpha(frame):
                    alpha_out[0] = True
                yield frame

    def _decode_all(self, fit_size: "tuple[int, int]",
                    saturation: float) -> "tuple[list[Image.Image], bool]":
        """The whole GIF, decoded and retained, timeline installed.
        Returns (frames, has_rendered_alpha)."""
        delays: "list[int]" = []
        alpha = [False]
        frames = list(self._composited_walk(fit_size, saturation, delays, alpha))
        self._adopt_timeline(delays)
        return frames, alpha[0]

    def _hold_frame_list(self, frames: "list[Image.Image]") -> None:
        """Keep the decoded frames as this key's per-frame memo: the only
        representation that carries alpha.

        Registers with the image-cache census, accounting-only and only for
        this route -- the video route's RAM is the reader's, already counted
        under video_readers. Never evictable: these frames ARE the memo, so
        evicting them would re-decode the GIF every media tick."""
        self.frames = frames
        self._frames_bytes = sum(
            frame.width * frame.height * len(frame.getbands()) for frame in frames
        )
        cache_budget.register(
            self, label=f"gif_frames:{os.path.basename(self.gif_path)}", evictable=False)

    def _cold_retained_walk(self, fit_size: "tuple[int, int]", saturation: float,
                            out_size: "tuple[int, int]") -> None:
        """Under budget: walk once holding the frames, then let what they
        turned out to BE decide where they live.

        Alpha -> they stay (the RAM route, main's exact cost and pixels).
        Opaque -> they are written into the shared tile cache and dropped;
        the key ends up at O(1) RAM with pixels PIL itself composited. The
        peak here is one fitted frame list, which is main's *steady* state,
        held for the length of one encode (~40ms per 200 frames at 2x tile
        -- measured, so this stays on the constructing thread rather than
        buying a thread and a serve-from-list-until-promoted handover)."""
        frames, has_alpha = self._decode_all(fit_size, saturation)
        if has_alpha:
            self._hold_frame_list(frames)
            return

        reader = mp4_tile_cache.acquire_from_frames(
            self.gif_path, out_size, saturation, frames)
        if reader is None:
            # Cache unwritable (no codec, full/read-only disk): keeping the
            # frames is the honest degrade -- the key plays, correctly, at
            # main's footprint. The warning names the cost.
            log.warning(
                f"Could not build the tile cache for {self.gif_path}; keeping "
                f"{len(frames)} frames in RAM for this key instead"
            )
            self._hold_frame_list(frames)
            return
        self.video_cache = reader

    def _cold_streaming_walk(self, fit_size: "tuple[int, int]", saturation: float,
                             out_size: "tuple[int, int]", estimate: int, budget: int,
                             n_frames: int, retained_size: "tuple[int, int]") -> None:
        """Over budget: walk once WITHOUT holding anything, straight into
        the tile-cache writer.

        This is the one path in the design that can trade visuals for
        bounds: a GIF whose retained frames would exceed
        DECKARD_GIF_KEY_BUDGET_MB (default 32MB) plays without alpha rather
        than being allowed to blow a page's memory up on its own. Bounded
        beats pretty -- the same ladder GifBackground walks down to the cv2
        path -- and the alpha loss is announced once, at construction, never
        per tick."""
        log.warning(
            f"{self.gif_path}: ~{estimate / (1024 * 1024):.1f}MB of frames "
            f"({n_frames} at {retained_size[0]}x{retained_size[1]} RGBA) exceeds the "
            f"{budget / (1024 * 1024):.1f}MB per-GIF budget -- streaming it into the "
            f"mp4 tile cache instead, at one frame of RAM"
        )
        delays: "list[int]" = []
        alpha = [False]
        reader = mp4_tile_cache.acquire_from_frames(
            self.gif_path, out_size, saturation,
            self._composited_walk(fit_size, saturation, delays, alpha),
            variant=BOUNDED_TILE_VARIANT)
        if reader is None:
            raise RuntimeError(
                f"GIF is over the per-GIF frame budget and its tile cache could "
                f"not be built: {self.gif_path}"
            )
        if alpha[0]:
            log.warning(
                f"{self.gif_path} carries transparency, which the bounded mp4 route "
                f"cannot keep -- this key plays opaque. Raise "
                f"DECKARD_GIF_KEY_BUDGET_MB above "
                f"{estimate / (1024 * 1024):.1f} to keep it in RAM instead"
            )
        self.video_cache = reader
        if not delays:
            # The artifact already existed (another key built it while this
            # walk was being set up), so the generator was never consumed
            # and the delays never collected. Walk them, cheaply.
            delays = probe_gif_timeline(self.gif_path).frame_delays
        self._adopt_timeline(delays)

    def _source_index(self, cache, index: int) -> int:
        """Timeline frame index -> the reader's frame index.

        The cache is written frame-for-frame from the PIL walk, so this is
        normally the identity. It is not trusted, because the reader's count
        MOVES at runtime -- a promoted cache reports what the container
        actually holds, and a short read clamps it -- so a disagreement
        scales the index into the reader's range rather than reading past
        the end. Timing authority stays with the PIL delay timeline either
        way."""
        n_video = cache.n_frames
        n_timeline = len(self._cum_delays)
        if n_video <= 0 or n_timeline <= 0 or n_video == n_timeline:
            return index
        return min(n_video - 1, index * n_video // n_timeline)

    def _video_frame(self, index: int) -> Image.Image | None:
        """One frame off the shared tile cache. Check-then-hold, exactly
        InputVideo.get_next_frame's pattern: the unlocked peek keeps the
        post-close hot path free, the lock makes close() wait for an
        in-flight decode instead of releasing the reader underneath it."""
        if self.video_cache is None:
            return None
        with self._close_lock:
            cache = self.video_cache
            if cache is None:
                return None
            return cache.get_frame(self._source_index(cache, index))

    def _frame_at(self, index: int) -> Image.Image | None:
        """The payload for a picked timeline index, whichever route holds
        it. None once the route it came from has been released."""
        frames = self.frames
        if frames:
            return frames[index]
        return self._video_frame(index)

    def budget_bytes(self) -> int:
        """Pixel bytes of the retained frame list (image-cache census). Computed
        once at decode time: the list is immutable for this object's life.
        Zero on the video route, which registers nothing here -- its RAM is
        the reader's, counted under video_readers."""
        return self._frames_bytes

    def _frame_count(self) -> int:
        """Frames this object can serve. The retained list on the alpha
        route; the timeline's length on the video route, where the frames
        live in the cache file rather than in this object."""
        if self.frames:
            return len(self.frames)
        return len(self._cum_delays) if self.video_cache is not None else 0

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        # ONE snapshot of the timeline for the whole tick. close() rebinds
        # these to fresh empty containers from the teardown thread, so
        # re-reading them mid-arithmetic could divide by a _total_delay that
        # was non-zero at the guard and zero at the modulo (reproduced:
        # ~2e-5 per close at the default switch interval -- bounded by the
        # media tick's own guard, but it costs a dropped tick and a 250ms
        # backoff exactly at page teardown), or index a timeline emptied
        # after its length was read.
        cum_delays = self._cum_delays
        total_delay = self._total_delay
        n = self._frame_count()
        if n == 0:
            return None
        if n == 1 or total_delay <= 0:
            # Single-frame GIF, or no usable timing info: nothing to pick.
            self.active_frame = 0
            return self._frame_at(0)

        if now is None:
            now = time.time()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            # Ticks stopped while the page/key was away (screensaver, page
            # switch, suspend): shift the timebase across the gap so playback
            # resumes near where it left off instead of fast-forwarding
            # through the whole gap (mirrors BackgroundVideo's gap clamp).
            frame_period = cum_delays[0] if cum_delays else total_delay / n
            self._play_start += (now - self._last_frame_tick) - frame_period
        self._last_frame_tick = now

        elapsed = now - self._play_start
        t = elapsed % total_delay if self.loop else min(elapsed, total_delay)

        frame = bisect.bisect_right(cum_delays, t)
        if frame >= n:
            frame = n - 1  # guard the end: float-edge / non-loop clamp landing on t == total
        self.active_frame = frame

        return self._frame_at(self.active_frame)

    def get_frame_delay(self) -> float:
        """Get delay for current frame in seconds"""
        if self.active_frame < 0 or self.active_frame >= len(self.frame_delays):
            return 1.0 / self.fps  # Fallback to fps-based timing
        return self.frame_delays[self.active_frame] / 1000.0  # Convert ms to seconds
    
    def get_raw_image(self) -> Image.Image:
        # get_next_frame() is None once close() has released the frames;
        # widening only this override would be an incompatible return type.
        return self.get_next_frame()  # type: ignore[return-value]  # root cause: SingleKeyAsset.get_raw_image -> Image.Image is too narrow (Subclasses/SingleKeyAsset.py)
    
    def close(self) -> None:
        """Drops the retained frame list (the whole footprint) and leaves the
        object safely tickable.

        Swaps in fresh EMPTY containers instead of None/del: a
        media tick can land after close() -- teardown races the media loop --
        and get_next_frame()/get_frame_delay() read len(self.frames) /
        len(self.frame_delays) on every tick, where `None` raised TypeError
        and the deleted attribute raised AttributeError. With empty lists the
        n == 0 arm already returns None, so a late tick and a double close are
        both no-ops by construction (GifBackground.close()'s pattern -- the
        two providers stay in lockstep).

        Also leaves the image-cache census immediately: the frames are gone, so the
        registered byte count must stop being reported rather than linger
        until GC drops the weak registration.

        Video route: detaches this reader from the shared tile-cache registry
        (InputVideo.close()'s contract -- the shared cache file and its
        builder outlive us if another key still wants them). The timeline is
        emptied FIRST, so a tick racing this call sees zero frames and
        returns before it can ask a released reader for pixels; the lock then
        waits out any fetch already in flight. Idempotent."""
        self.frames = []
        self.frame_delays = []
        self._cum_delays = []
        self._total_delay = 0.0
        self._frames_bytes = 0
        cache_budget.unregister(self)
        with self._close_lock:
            if self.video_cache is not None:
                mp4_tile_cache.release(self.video_cache)
                self.video_cache = None
