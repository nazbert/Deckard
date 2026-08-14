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

The GIF pipeline holds the one PIL compositor in the app, the budget ladder
that bounds what a GIF retains, and the two providers on them. GifBackground
draws a deck or strip canvas; KeyGIF draws one key. This module imports
nothing from its sibling modules in the deck_controller package.
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


#: extend_touchscreen means __init__ computed the strip geometry. A raise
#: keeps the Background.update_tiles contract: previous tiles kept, one
#: rate-limited log. A key-only tile set leaves the strip frozen and silent.
_STRIP_GEOMETRY_MISSING = (
    "extend_touchscreen is set but the strip geometry was never computed"
)


class GifBudgetExceeded(Exception):
    """decode_gif_frames raises this before it decodes a frame, when the
    estimated footprint exceeds the caller's budget. The caller falls back to
    a bounded path instead of an OOM risk on a many-frame GIF."""


# RAM ceiling for a fully-decoded GIF background. GifBackground estimates
# n_frames x W x H x 4 against it at open, and falls back to the opaque cv2
# path when over. 128MB covers ~200 canvas frames on an SD+ at ~600KB each,
# or ~90 on an XL at ~1.4MB each.
GIF_BG_BUDGET_MB = 128

# Per-GIF ceiling for one key's retained RGBA frame list. Only an
# alpha-carrying GIF builds one, because an opaque GIF plays off the mp4 tile
# cache at O(1) RAM. 32MB is ~220 frames at 2x an SD+ tile, or ~110 at 2x an XL
# tile.
GIF_KEY_BUDGET_MB = 32

# Cache-file variant for the over-budget artifact of
# KeyGIF._cold_streaming_walk. Its frames lost their alpha, so a GIF that is
# under budget must not read it back and lose its transparency. A separate
# variant stops the two renderings from sharing a file.
BOUNDED_TILE_VARIANT = ".bounded"

# Raw DECKARD_GIF_KEY_BUDGET_MB values that already have a warning logged. A
# bad or very small setting then costs one log line per distinct value for
# the life of the process, not one per GIF key per page load.
_warned_gif_budget_values: "set[str]" = set()


def gif_key_budget_bytes() -> int:
    """Byte ceiling for one key GIF's retained frame list, from
    DECKARD_GIF_KEY_BUDGET_MB. Zero or less disables the frame list, and
    every GIF then plays off the mp4 tile cache and trades alpha for a bound.
    A malformed value degrades to the default with a warning, because a typo
    must not cost a key its media at page load; nan, inf and 1e400 count as
    malformed. A value below 1 MiB also warns, because it is smaller than one
    fitted frame at any tile size, so every alpha GIF drops transparency."""
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
    """The dimensions that ImageOps.contain lands on: aspect-preserving and
    shrink-only, so a source inside max_size keeps its size and does not
    multiply retained memory. The result matches ImageOps.contain to within
    one pixel per axis, which rounds each axis on its own recomputed scale;
    94 of 1.1M surveyed pairs differ. The frame-list decode and the video
    route both call this, so a GIF's geometry cannot depend on its route."""
    src_w, src_h = source_size
    max_w, max_h = max_size
    if src_w <= max_w and src_h <= max_h:
        return src_w, src_h
    scale = min(max_w / src_w, max_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def tile_video_size(source_size: "tuple[int, int]", max_size: "tuple[int, int]") -> "tuple[int, int]":
    """The tile-cache geometry for a source contained into max_size. It is
    contained_size() rounded down to even dimensions, and never below 2.
    Even, because mp4v rounds an odd dimension down and leaves every payload
    a pixel off the requested geometry. The floor of 2 exists because a 1-px
    axis has no even value below it. A cached tile is then up to 1 px smaller
    per axis than the retained frame list, and a 3x100 source drops to 2x100.
    Every KeyGIF route calls this, so geometry cannot depend on the route."""
    out_w, out_h = contained_size(source_size, max_size)
    return max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)


def normalize_gif_delay(raw: "int | None") -> int:
    """One frame's GIF duration metadata as the delay to play, in ms.
    Missing or under 20ms becomes 100ms, as Firefox and Chrome do; any other
    value stands. The full decode and the header-only probe share this
    definition, so the two cannot disagree about a timeline."""
    if raw is None or raw < 20:
        return 100
    return raw


def cumulative_gif_delays(delays_ms: "list[int]") -> "list[float]":
    """A delay list in ms as a cumulative timeline in seconds. Element i is
    the wall-clock time at which frame i's display window ends, so a pick for
    elapsed time t is one bisect instead of a per-tick loop."""
    return list(itertools.accumulate(d / 1000.0 for d in delays_ms))


@dataclass(frozen=True, slots=True)
class GifTimeline:
    """A GIF's playback timeline and geometry, with no decoded frame: the
    frame count, the per-frame delays, the cumulative wall-clock edges and
    the source size. A KeyGIF uses it when the pixels come from a tile cache
    that PIL wrote earlier. Timing authority stays here, never in the video
    container."""
    n_frames: int
    frame_delays: "list[int]"
    cum_delays: "list[float]"
    size: "tuple[int, int]"


def gif_header_geometry(path: str) -> "tuple[int, tuple[int, int]]":
    """(frame count, canvas size) from the GIF headers alone. No frame
    decodes and no seek runs, so a caller can price a decode first."""
    gif = Image.open(path)
    try:
        return getattr(gif, "n_frames", 1), gif.size
    finally:
        gif.close()


def probe_gif_timeline(path: str) -> GifTimeline:
    """The playback timeline of the GIF at path, with nothing retained. PIL
    exposes a frame duration only after a seek, and a seek composes the
    previous frame, so this walk runs the decoder but converts, fits,
    saturates and keeps nothing: ~49 ms and O(1) RAM on a 200-frame 500x500
    GIF, against ~320 ms and the full frame list for decode_gif_frames. It
    raises what PIL raises on a corrupt file, and the caller treats that as a
    failed decode."""
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
    """Does this rendered frame carry transparency? Test the pixels, not the
    header. Of 396 real animated GIFs, 75% declare a transparent index on
    frame 0 and 11% render a pixel with alpha under 255, because disposal 1
    declares the index to mean "leave this pixel alone". One extrema pass per
    frame; the caller stops asking once the answer is yes."""
    if frame.mode != "RGBA":
        return False
    extrema = frame.getextrema()
    return len(extrema) >= 4 and extrema[3][0] < 255


def gif_frame_walk(path: str, max_size: "tuple[int, int]" = None,
                   fit_size: "tuple[int, int]" = None,
                   saturation: float = 1.0):
    """Generator over one GIF's frames: PIL composites each frame, converts
    it to RGBA, sizes it, bakes the saturation, and yields (frame, delay_ms).
    This is the one GIF compositor in the app, so the retained frame list,
    the GIF backgrounds and the KeyGIF tile mp4 cannot disagree about frame
    N. Pass max_size or fit_size, or neither: max_size contains shrink-only,
    because upscaling multiplies retained memory; fit_size fits every frame
    to exactly that size, so a background fills the canvas and the per-key
    crop coordinates hold. The walk closes the source handle when it ends,
    and when the consumer abandons it."""
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
    """Every frame of the GIF at path, decoded to RGBA and retained, plus
    its delay timeline. This is GifBackground's entry point; KeyGIF drives
    gif_frame_walk itself so it can decide frame by frame what to keep.
    budget_bytes gates before the decode. It estimates the retained
    footprint from the header as n_frames x out_w x out_h x 4, and raises
    GifBudgetExceeded when it exceeds the budget. Returns (frames_rgba,
    frame_delays_ms, cum_delays), where cum_delays[i] is the second at which
    frame i's display window ends.
    """
    if budget_bytes is not None:
        n_frames, (out_w, out_h) = gif_header_geometry(path)
        if fit_size is not None:
            out_w, out_h = fit_size
        elif max_size is not None:
            # The dimensions ImageOps.contain lands on. The video route's
            # tile size shares them. See contained_size.
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
    """RGBA GIF provider for deck and strip backgrounds.

    It meets the BackgroundVideo contract: get_next_tiles() returns
    (entries, identity), with the strip slice as one extra entry when
    extended, plus the video_path, extend_touchscreen, saturation, page, fps
    and loop attributes that the prebuild keep-check, the media tick and the
    screensaver setters read. PIL decodes it, so alpha and the per-frame
    delay timeline survive; the cv2 demuxer drops both.

    Construction decodes every frame once, fits it to exactly the deck canvas
    so the per-key crop coordinates hold, and owns it until the background
    swap closes this object. Two decks that show the same GIF decode it
    twice, because there is no shared cache here. Construction raises
    GifBudgetExceeded before the decode when the estimate exceeds
    GIF_BG_BUDGET_MB, and the caller falls back to the opaque cv2 path.

    canvas_size overrides the deck-canvas geometry for the strip-background
    route. get_next_frame() then serves whole strip-fitted frames, which the
    touchscreen composite alpha-composites at their exact size.
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

        # Both stay None unless extend_touchscreen is on, because the strip
        # geometry exists only for the extended canvas. Every read pairs with that
        # flag, and the None-guards at those reads make the pairing checkable.
        self.strip_size: "tuple[int, int] | None" = None
        self._strip_box: "tuple[int, int, int, int] | None" = None
        if canvas_size is None:
            # The same canvas and crop geometry as BackgroundVideoCache.
            # Compute it once into plain boxes, because the frame list does
            # not change after the decode.
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
                # continues onto the strip: one bezel gap plus the strip in
                # canvas coordinates. BackgroundImage uses this geometry too.
                self.strip_size = deck_controller.get_touchscreen_image_size()
                strip_canvas_h = round(self.strip_size[1] * canvas_w / self.strip_size[0])
                canvas_h += spacing_y + strip_canvas_h
                self._strip_box = (0, canvas_h - strip_canvas_h, canvas_w, canvas_h)
            canvas_size = (canvas_w, canvas_h)
        else:
            # In strip-background mode it serves whole frames only.
            self.key_count = 0
            self._key_regions = []
            self.extend_touchscreen = False

        self.canvas_size = canvas_size

        self.frames, self.frame_delays, self._cum_delays = decode_gif_frames(
            gif_path, fit_size=canvas_size, saturation=self.saturation,
            budget_bytes=GIF_BG_BUDGET_MB * 1024 * 1024,
        )
        self._total_delay: float = self._cum_delays[-1] if self._cum_delays else 0.0

        # Frame identity for the passthrough-key native-encode memo: (md5,
        # frame index), the exact BackgroundVideo contract. Steady-state loop
        # playback then costs one dict lookup and one USB write per key.
        self.video_md5 = get_video_md5(gif_path)

        self.active_frame: int = -1
        # Wall-clock timeline state. See _pick_frame.
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None
        # (frame index, entries) of the last cropped frame. Most ticks land
        # on the frame already cut, because a 10fps GIF under a 30Hz tick
        # re-uses each crop set about 3 times. The caller always gets copies.
        self._tiles_memo: tuple = (None, None)

    def _pick_frame(self, now: float = None) -> int:
        """Wall-clock frame index for now, from a bisect over the cumulative
        delay timeline plus the away-gap clamp. KeyGIF.get_next_frame runs
        the same arithmetic; see its comments for each branch."""
        # Snapshot the timeline once, because close() swaps frames,
        # _cum_delays and _total_delay from the GTK or screensaver thread
        # while the media tick runs here. Locals hold the guard and the arithmetic on one
        # generation, so a racer cannot land a zero modulo or an index past
        # the list end. Every index returned stays in range for the caller's
        # own frames snapshot.
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
            frame = n - 1  # clamp a float edge, or a non-loop t equal to total
        self.active_frame = frame
        return frame

    def get_next_tiles(self) -> "tuple[list[Image.Image], tuple | None]":
        """(entries, identity) for the frame this tick lands on, per the
        BackgroundVideo.get_next_tiles contract: key tiles, plus the strip
        slice as one extra entry when extended. identity is (md5, frame
        index). Each crop comes straight from the retained RGBA canvas frame,
        so alpha reaches the compositor, and the caller gets copies to paste
        onto in place."""
        frames = self.frames  # snapshot; close() empties this from other threads
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
                # Bottom slice of the extended canvas at strip resolution.
                # It uses the crop and HAMMING resize of
                # BackgroundVideoCache.crop_strip_from_deck_sized_image.
                memo_entries.append(
                    frame.crop(strip_box).resize(strip_size, Image.Resampling.HAMMING)
                )
            self._tiles_memo = (index, memo_entries)
        return [entry.copy() for entry in memo_entries], (self.video_md5, index)

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        """The whole canvas-size RGBA frame for now, on the strip-background
        route. It returns the retained frame itself, and the caller's
        convert("RGBA") copies it before anything pastes onto it. Returns
        None after close() empties the frame list."""
        frames = self.frames
        if not frames:
            return None
        return frames[self._pick_frame(now)]

    def set_playback(self, fps: int, loop: bool) -> None:
        """fps sets only the owner's render cap. The playback position
        follows the wall clock over the GIF's own delay timeline, as
        InputVideo does at natural speed, so no timebase rebase runs."""
        self.fps = fps
        self.loop = loop

    def close(self) -> None:
        """Drop the retained frame list, which is the whole footprint. An
        in-flight tick stays safe, because get_next_tiles and get_next_frame
        snapshot self.frames and a racer finishes on its own reference."""
        self.frames = []
        self.frame_delays = []
        self._cum_delays = []
        self._total_delay = 0.0
        self._tiles_memo = (None, None)


class KeyGIF(SingleKeyAsset):
    """Animated-GIF provider for one key, playing its own per-frame delay
    timeline.

    It holds its frames one of two ways. An opaque GIF uses the shared mp4
    tile registry: one refcounted cache file per (source, size, saturation),
    and one decoded frame in hand, so RAM is O(1) per key instead of O(frame
    count). A 200-frame GIF costs ~29MB retained, and a page of them ~0.9GiB.
    An alpha-carrying GIF uses the retained RGBA frame list, the only form
    that keeps transparency, because an mp4 has no alpha channel.

    Two rules make the split safe. PIL is the only compositor, so the tile
    mp4 is written from PIL-composited frames and FFmpeg never demuxes a GIF
    here, because FFmpeg disagrees with PIL on disposal and partial-extent
    frames (7 of 15 frames on a stock test file, ~48% of pixels off). The
    route follows the rendered alpha, not the header declaration. 75% of real
    GIFs declare a transparent index and 11% ever render one, so the
    declaration left ~64% of GIFs on the expensive path.

    The classification costs one PIL walk the first time a GIF appears at a
    given size, plus an alpha extrema pass and, for an opaque GIF, one
    encode. After that the artifact on disk is the classification, and a warm
    construction walks the delays only, attaches a reader, and decodes no
    pixel.

    With performance.cache-videos off there is no disk cache to route to, so
    every GIF stays on the frame list and no GIF reaches the registry.

    Either way the timeline is PIL's, from wall-clock picking over the
    cumulative per-frame delays, so an irregular GIF plays at its own rhythm
    instead of
    a constant fps. On the video route the picked index goes to the reader
    instead of a list subscript, with identical arithmetic."""

    # Class-level default. The tests build instances attribute by attribute
    # through __new__ to exercise the picking arithmetic, and those instances
    # never take the video route.
    video_cache = None

    def __init__(self, controller_key: "ControllerKey", gif_path: str, fps: int = 30, loop: bool = True):
        super().__init__(controller_key)
        self.gif_path = gif_path
        self.fps = fps
        self.loop = loop

        self.active_frame: int = -1
        # Wall-clock timeline state, as in BackgroundVideo and InputVideo. It
        # keys against a cumulative-delay timeline instead of a fixed fps,
        # because GIF frame durations are per-frame and often irregular.
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None

        # Serialize close() against an in-flight frame fetch on the video
        # route, as InputVideo._close_lock does. A get_frame() that starts
        # after release() resurrects a capture through
        # _maybe_adopt_shared_cache and leaks it.
        self._close_lock = threading.Lock()

        self.frames: "list[Image.Image]" = []
        self._frames_bytes = 0

        # Cap the frame size at 2x the key tile, not the source resolution. A
        # 500px 200-frame GIF costs ~200MB at source and ~46MB fitted. The UI
        # caps the composited size at 200%, so 2x tile is the largest size a
        # frame ever shows at. Both routes stay shrink-only and
        # aspect-preserving. See tile_video_size.
        tile_w, tile_h = self.deck_controller.get_key_image_size()
        fit_size = (max(1, tile_w * 2), max(1, tile_h * 2))

        # Bake the saturation in once, at decode time. The frames are this
        # asset's per-frame memo, so a per-tick enhance re-pays ImageEnhance
        # forever. A saturation change reloads the page and rebuilds this
        # object under the new factor, and the registry keys its cache files
        # on the factor. The default factor skips this step.
        saturation = self.deck_controller.get_display_saturation()

        self.frame_delays: "list[int]" = []
        self._cum_delays: "list[float]" = []
        self._total_delay: float = 0.0

        # With no disk cache there is nothing to route to. Every GIF keeps
        # its frame list and the registry never sees a GIF. A reader with no
        # artifact decodes the source, which brings FFmpeg's divergent
        # compositing and a capture that repeats its last frame forever. Read
        # this once, so one object cannot change routes mid-life.
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

        # The header parse raises on a corrupt file, as the decode after it
        # does. The construct site fails soft to InputVideo.
        n_frames, source_size = gif_header_geometry(self.gif_path)
        out_size = tile_video_size(source_size, fit_size)

        # Pick the artifact before any pixel decode: the lossless one an
        # opaque GIF builds, or the alpha-dropping one the over-budget ladder
        # streams. Separate variants stop a GIF that streamed under a tiny
        # budget from reading back as opaque forever. The estimate uses the
        # retained footprint at the frame list geometry, not the mp4's.
        retained_size = contained_size(source_size, fit_size)
        estimate = n_frames * retained_size[0] * retained_size[1] * 4
        budget = gif_key_budget_bytes()
        over_budget = estimate > budget
        variant = BOUNDED_TILE_VARIANT if over_budget else ""

        # The artifact exists, so an earlier walk saw this GIF at this size
        # and, on the lossless variant, found it opaque. Walk the delays and
        # attach; no frame decodes, converts, fits or stays.
        reader = mp4_tile_cache.attach_promoted(self.gif_path, out_size, saturation,
                                                variant=variant)
        if reader is not None:
            try:
                self._adopt_timeline(probe_gif_timeline(self.gif_path).frame_delays)
            except Exception:
                # Do not leave the registry holding a reference for an
                # object whose constructor raises.
                mp4_tile_cache.release(reader)
                raise
            self.video_cache = reader
            return

        # On a cold start one PIL walk decides everything. See the class
        # docstring.
        if over_budget:
            self._cold_streaming_walk(fit_size, saturation, out_size,
                                      estimate, budget, n_frames, retained_size)
        else:
            self._cold_retained_walk(fit_size, saturation, out_size)

    def _adopt_timeline(self, delays_ms: "list[int]") -> None:
        """Install the per-frame delays as this object's playback timeline.
        One place, so no route can install a timeline the others could not."""
        self.frame_delays = list(delays_ms)
        self._cum_delays = cumulative_gif_delays(self.frame_delays)
        self._total_delay = self._cum_delays[-1] if self._cum_delays else 0.0

    def _composited_walk(self, fit_size: "tuple[int, int]", saturation: float,
                         delays_out: "list[int]", alpha_out: "list[bool]"):
        """The single PIL pass. It composites with PIL, never FFmpeg, fits
        shrink-only to 2x tile, and bakes the saturation, while it records each
        frame's delay and its exact rendered-alpha verdict. It yields the
        frames; the caller decides whether to keep them for the RAM route, or
        hand them to the writer and stay at O(1). The alpha check stops once
        the answer is yes, usually on frame 0."""
        with contextlib.closing(gif_frame_walk(
                self.gif_path, max_size=fit_size, saturation=saturation)) as walk:
            for frame, delay in walk:
                delays_out.append(delay)
                if not alpha_out[0] and frame_has_alpha(frame):
                    alpha_out[0] = True
                yield frame

    def _decode_all(self, fit_size: "tuple[int, int]",
                    saturation: float) -> "tuple[list[Image.Image], bool]":
        """The whole GIF, decoded and retained, with the timeline installed.
        Returns (frames, has_rendered_alpha)."""
        delays: "list[int]" = []
        alpha = [False]
        frames = list(self._composited_walk(fit_size, saturation, delays, alpha))
        self._adopt_timeline(delays)
        return frames, alpha[0]

    def _hold_frame_list(self, frames: "list[Image.Image]") -> None:
        """Keep the decoded frames as this key's per-frame memo, the only
        form that carries alpha. It registers with the image-cache census for
        accounting only, and only on this route; the video route's RAM belongs
        to the reader and counts under video_readers. The entry is not
        evictable, because an eviction re-decodes the GIF on every tick."""
        self.frames = frames
        self._frames_bytes = sum(
            frame.width * frame.height * len(frame.getbands()) for frame in frames
        )
        cache_budget.register(
            self, label=f"gif_frames:{os.path.basename(self.gif_path)}", evictable=False)

    def _cold_retained_walk(self, fit_size: "tuple[int, int]", saturation: float,
                            out_size: "tuple[int, int]") -> None:
        """When under budget, walk once and hold the frames, then let what
        they are decide where they live. An alpha GIF keeps them in RAM. An opaque
        GIF writes them into the shared tile cache and drops them, so the key
        runs at O(1) RAM on pixels that PIL composited. The peak is one fitted
        frame list, held for one encode: ~40ms per 200 frames at 2x tile,
        which is why this stays on the constructing thread."""
        frames, has_alpha = self._decode_all(fit_size, saturation)
        if has_alpha:
            self._hold_frame_list(frames)
            return

        reader = mp4_tile_cache.acquire_from_frames(
            self.gif_path, out_size, saturation, frames)
        if reader is None:
            # The cache is unwritable, from a missing codec or a full or
            # read-only disk. Keep the frames so the key plays correctly, and
            # log the memory cost.
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
        """When over budget, walk once and hold nothing, straight into the
        tile-cache writer. This is the one path that trades visuals for
        bounds. A GIF whose retained frames exceed DECKARD_GIF_KEY_BUDGET_MB,
        32MB by default, plays without alpha instead of taking a page's whole
        memory. The alpha loss logs once, at construction, never per tick."""
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
            # The artifact already exists, because another key built it
            # while this walk started. The writer never consumed the generator, so it
            # collected no delay. Walk them again, cheaply.
            delays = probe_gif_timeline(self.gif_path).frame_delays
        self._adopt_timeline(delays)

    def _source_index(self, cache, index: int) -> int:
        """Map a timeline frame index to the reader's frame index. The cache
        is written frame-for-frame from the PIL walk, so this is usually the
        identity. The reader's count moves at runtime, because a promoted
        cache reports what the container holds and a short read clamps it. A
        difference scales the index into the reader's range instead of a read
        past the end. The PIL delay timeline keeps timing authority."""
        n_video = cache.n_frames
        n_timeline = len(self._cum_delays)
        if n_video <= 0 or n_timeline <= 0 or n_video == n_timeline:
            return index
        return min(n_video - 1, index * n_video // n_timeline)

    def _video_frame(self, index: int) -> Image.Image | None:
        """One frame off the shared tile cache. Check then hold, as
        InputVideo.get_next_frame does. The unlocked peek keeps the post-close
        path fast, and the lock makes close() wait for an in-flight decode
        instead of releasing the reader under it."""
        if self.video_cache is None:
            return None
        with self._close_lock:
            cache = self.video_cache
            if cache is None:
                return None
            return cache.get_frame(self._source_index(cache, index))

    def _frame_at(self, index: int) -> Image.Image | None:
        """The payload for a picked timeline index, from whichever route
        holds it. None after that route releases it."""
        frames = self.frames
        if frames:
            return frames[index]
        return self._video_frame(index)

    def budget_bytes(self) -> int:
        """Pixel bytes of the retained frame list, for the image-cache
        census. It is computed once at decode time, because the list does not
        change for this object's life. Zero on the video route, which registers nothing
        here, because its RAM belongs to the reader and counts under
        video_readers."""
        return self._frames_bytes

    def _frame_count(self) -> int:
        """Frames this object can serve: the retained list on the alpha
        route, and the timeline length on the video route, where the frames
        live in the cache file instead of this object."""
        if self.frames:
            return len(self.frames)
        return len(self._cum_delays) if self.video_cache is not None else 0

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        # One snapshot of the timeline for the whole tick. close() rebinds
        # these to empty containers from the teardown thread, so a re-read
        # can divide by a _total_delay that was non-zero at the guard and zero
        # at the modulo, or index a timeline emptied after it read the length.
        cum_delays = self._cum_delays
        total_delay = self._total_delay
        n = self._frame_count()
        if n == 0:
            return None
        if n == 1 or total_delay <= 0:
            # A single-frame GIF, or one with no usable timing, has nothing
            # to pick.
            self.active_frame = 0
            return self._frame_at(0)

        if now is None:
            now = time.time()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            # Ticks stop while the page or key is away: screensaver, page
            # switch or suspend. Shift the timebase across the gap so playback
            # continues near its old position, with no fast-forward.
            frame_period = cum_delays[0] if cum_delays else total_delay / n
            self._play_start += (now - self._last_frame_tick) - frame_period
        self._last_frame_tick = now

        elapsed = now - self._play_start
        t = elapsed % total_delay if self.loop else min(elapsed, total_delay)

        frame = bisect.bisect_right(cum_delays, t)
        if frame >= n:
            frame = n - 1  # clamp a float edge, or a non-loop t equal to total
        self.active_frame = frame

        return self._frame_at(self.active_frame)

    def get_frame_delay(self) -> float:
        """The delay of the current frame, in seconds."""
        if self.active_frame < 0 or self.active_frame >= len(self.frame_delays):
            return 1.0 / self.fps  # fall back to fps-based timing
        return self.frame_delays[self.active_frame] / 1000.0
    
    def get_raw_image(self) -> Image.Image:
        # get_next_frame() returns None after close() releases the frames. A
        # wider return type on this override alone is incompatible.
        return self.get_next_frame()  # type: ignore[return-value]  # SingleKeyAsset.get_raw_image declares Image.Image, which is too narrow
    
    def close(self) -> None:
        """Drop the retained frame list, which is the whole footprint, and
        leave the object tickable.

        It swaps in empty containers instead of None, because a media tick can
        land after close() and read len(self.frames) and
        len(self.frame_delays). With empty lists the n == 0 arm returns None,
        so a late tick and a second close both do nothing.

        It also leaves the image-cache census at once, so the registered byte
        count stops reporting frames that are gone.

        On the video route it detaches this reader from the shared tile-cache
        registry; the shared cache file and its builder stay for any other key
        that wants them. The timeline empties first, so a racing tick sees
        zero frames and returns before it asks a released reader for pixels.
        The lock then waits out any fetch in flight."""
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
