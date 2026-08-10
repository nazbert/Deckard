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

The background media group: one Background per deck -- the compositor that
turns whatever is currently loaded into the key tiles (and, on an SD+, the
touchscreen strip slice) -- plus the two sources it plays. BackgroundImage
fits a still to the deck canvas once and crops tiles out of it;
BackgroundVideo picks frames off BackgroundVideoCache by wall clock so a
slow media loop drops frames instead of playing in slow motion.

Media resolution is deliberately two-phase: prebuild_from_path() constructs
the new object lock-free (hashing a video file and opening its capture can
take seconds), apply_prebuilt() performs the swap under the caller's lock.

The one in-package edge: a .gif diverts to gif_pipeline's GifBackground so
alpha and the per-frame delay timeline survive, falling back to the opaque
cv2 path when that is over budget or undecodable. Everything else here
reads geometry, saturation and cache policy off the duck-typed controller
that owns it, which is why the type-only imports below are the whole of its
knowledge about it.
"""
import gc
import os
import time

from PIL import Image, ImageEnhance, ImageOps
from loguru import logger as log

from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground, GifBudgetExceeded

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.backend.DeckManagement.DeckController import DeckController
    from src.backend.PageManagement.Page import Page


class Background:
    def __init__(self, deck_controller: "DeckController"):
        self.deck_controller = deck_controller

        self.image: "BackgroundImage | None" = None
        self.video: "BackgroundVideo | None" = None

        # Extend the background onto the touchscreen strip (SD+). For static
        # images the slice is memoized because the strip re-composites on
        # every dial label change; for videos update_tiles() refreshes
        # _video_strip once per frame.
        self.extend_to_touchscreen: bool = False
        self._touchscreen_slice: Image.Image | None = None
        self._video_strip: Image.Image | None = None

        # Read-only view: update_tiles() replaces the whole list (from sources
        # that yield either all-Image or, on the video cache's defensive path,
        # None entries) and nothing mutates it in place, so Sequence is both
        # accurate and the only way to accept both element types.
        self.tiles: Sequence[Image.Image | None] = [None] * deck_controller.deck.key_count()
        # (tiles, (video md5, frame index)) for the frame `tiles` holds, or
        # None for anything whose frame can't be named -- see
        # get_identified_tile().
        self._identified_tiles: tuple | None = None

    def set_image(self, image: "BackgroundImage", update: bool = True) -> None:
        self.image = image
        if self.video is not None:
            self.video.close()
        self.video = None
        self._touchscreen_slice = None
        self._video_strip = None
        # mem-plan P2.5: a content change orphans every cached native --
        # every entry was keyed against the OLD background's composited
        # pixels/hashes (or its frames). Left uncleared, a full memo from
        # the previous background would simply sit there dead until LRU
        # eviction happened to churn through it.
        self._identified_tiles = None
        self.deck_controller.clear_encoded_key_caches()
        self.deck_controller.refresh_tile_cache_min_age(None)
        gc.collect()

        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def set_video(self, video: "BackgroundVideo | None", update: bool = True) -> None:
        if self.video is not None:
            self.video.close()
        self.image = None
        self.video = video
        self._touchscreen_slice = None
        self._video_strip = None
        # mem-plan P2.5: see set_image()'s comment -- same reasoning applies
        # to a video-to-video (or image-to-video) content change. The md5 in
        # a native tile key already makes a source swap collision-free; the
        # clear is what stops the old video's frames lingering.
        self._identified_tiles = None
        self.deck_controller.clear_encoded_key_caches()
        # The new video's loop duration is what its frame entries must be
        # shielded for; see refresh_tile_cache_min_age.
        self.deck_controller.refresh_tile_cache_min_age(video)
        gc.collect()

        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def set_extend_to_touchscreen(self, extend: bool, update: bool = True) -> None:
        if extend == self.extend_to_touchscreen:
            return
        self.extend_to_touchscreen = extend
        self._touchscreen_slice = None
        self._video_strip = None

        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def _extend_effective(self) -> bool:
        return (
            self.extend_to_touchscreen
            and self.image is not None
            and self.deck_controller.deck.is_touch()
        )

    def get_touchscreen_image(self) -> Image.Image | None:
        """The strip-sized slice of the current background (image or video
        frame), or None if the background does not extend to the touchscreen."""
        if self.video is not None:
            # Refreshed by update_tiles() once per video frame; None unless
            # the video was built with extend_touchscreen.
            return self._video_strip
        image = self.image
        if image is None or not self._extend_effective():
            return None
        if self._touchscreen_slice is None:
            self._touchscreen_slice = image.get_touchscreen_image()
        return self._touchscreen_slice

    def prebuild_from_path(self, path: str | None, fps: int = 30, loop: bool = True, allow_keep: bool = True):
        """Phase-1 (lock-free) media resolution (plan §4 M3): constructs the
        new background object (if any) WITHOUT touching self.video/self.image
        or the deck. Building a BackgroundVideo hashes the whole source file
        and opens a capture -- can take seconds -- so this exists to let a
        caller (the screensaver transition) do that work before acquiring
        any lock. apply_prebuilt() is the phase-2 (under _background_load_lock)
        counterpart that actually performs the swap.

        Returns a (kind, payload) tuple:
          * ("blank", None)  -- path is empty/None: clear to no background.
          * ("noop", None)   -- non-video path that doesn't exist: leave
                                 whatever is currently showing alone (mirrors
                                 set_from_path's historical no-op here).
          * ("keep", None)   -- an equivalent video is already loaded
                                 (allow_keep); apply_prebuilt just refreshes
                                 its page/fps/loop, no rebuild.
          * ("video"|"image", obj) -- a freshly constructed object to swap in.
        """
        if path == "":
            path = None
        if path is None:
            return ("blank", None)
        if is_video(path):
            extend = self.extend_to_touchscreen and self.deck_controller.deck.is_touch()
            if allow_keep:
                # The extend mode and the saturation factor are both baked into
                # the video's canvas geometry/pixels and its cache file, so a
                # change to either forces a rebuild even for the same path
                # (otherwise a saturation change on an already-playing video
                # background would silently keep showing the old factor).
                if (self.video is not None and self.video.video_path == path
                        and self.video.extend_touchscreen == extend
                        and abs(self.video.saturation - self.deck_controller.get_display_saturation()) <= 0.001):
                    # Carry the path so apply_prebuilt can re-verify: this
                    # verdict is made lock-free, and a racing load_background
                    # may swap self.video before phase 2 applies it.
                    # (Holds for GifBackground too -- it carries the same
                    # three attributes; and for a GIF that fell back to the
                    # cv2 path below, keeping the fallback avoids re-paying
                    # the failed PIL decode attempt on every transition.)
                    return ("keep", path)
            if os.path.splitext(path)[1].lower() == ".gif":
                # .gif diverts to the PIL provider so alpha and the
                # per-frame delay timeline survive (cv2's demuxer drops
                # both). Over budget, or undecodable by PIL: fall back to
                # the EXISTING cv2 path below -- opaque, source-fps,
                # today's behavior -- rather than risk an OOM. One warning
                # per construction; the keep-check above stops it repeating
                # while the fallback stays loaded.
                try:
                    return ("video", GifBackground(self.deck_controller, path, loop=loop, fps=fps, extend_touchscreen=extend))
                except GifBudgetExceeded as e:
                    log.warning(f"GIF background over budget, falling back to the opaque cv2 path: {e}")
                except Exception:
                    log.opt(exception=True).warning(f"GIF background decode failed, falling back to the opaque cv2 path: {path}")
            return ("video", BackgroundVideo(self.deck_controller, path, loop=loop, fps=fps, extend_touchscreen=extend))
        if not os.path.isfile(path):
            return ("noop", None)
        with Image.open(path) as image:
            return ("image", BackgroundImage(self.deck_controller, image.copy(), path=path))

    def _discard_prebuilt(self, kind: str, payload) -> None:
        """Release the resources a prebuilt-but-never-applied payload holds
        (a known residual): a "video"/"image" payload already opened its
        cv2 capture / retained its PIL image in prebuild_from_path. Dropping
        the object without closing it leaks that handle. "keep"/"noop"/"blank"
        carry no fresh resource, so they are no-ops here."""
        if kind not in ("video", "image") or payload is None:
            return
        try:
            payload.close()
        except Exception:
            log.opt(exception=True).warning(
                "Failed to close an orphaned prebuilt background payload during close()"
            )

    def apply_prebuilt(self, kind: str, payload, fps: int = 30, loop: bool = True, update: bool = True) -> None:
        """Phase-2 counterpart to prebuild_from_path(): performs the actual
        swap. Callers that need the lock-free/locked split (the screensaver
        transition, plan §4 M3) call this under _background_load_lock with a
        generation re-check already done; no file I/O happens here, only
        object assignment + the same update_all_inputs() fan-out set_video/
        set_image already trigger."""
        # Authoritative close-vs-load guard (a known residual): a
        # load_background that already passed load_background's
        # _page_is_current(gen) gate before close() bumped the generation is
        # in-flight HERE with a freshly prebuilt payload -- prebuild_from_path
        # already opened its cv2 capture / retained its image. If it attached
        # now, close()'s step-7 sweep (which already ran, or is blocked on
        # _background_load_lock waiting for us) would never see it and it would
        # leak until process exit. _closing is set at the very top of close(),
        # before the sweep, so re-checking it here catches every ordering.
        # Release the orphaned payload's resources instead of dropping it on
        # the floor.
        if getattr(self.deck_controller, "_closing", False):
            self._discard_prebuilt(kind, payload)
            return
        if kind == "noop":
            return
        if kind == "keep":
            # Re-verify the lock-free keep verdict against the video that is
            # current NOW: a load_background racing the prebuild may have
            # swapped in a different file, and refreshing fps/loop on that
            # one would be wrong. A mismatch degrades to a no-op (rare,
            # self-heals on the next transition) rather than corrupting the
            # unrelated video's playback settings.
            if self.video is not None and self.video.video_path == payload:
                self.video.page = self.deck_controller.active_page
                self.video.fps = fps
                self.video.loop = loop
            else:
                log.warning("Stale 'keep' background verdict (video swapped mid-transition); leaving current background untouched")
            return
        if kind == "video":
            self.set_video(payload, update=update)
        elif kind == "image":
            self.set_image(payload, update=update)
        else:  # "blank"
            self.set_video(None, update=False)
            self._touchscreen_slice = None
            self.update_tiles()
            if update:
                self.deck_controller.update_all_inputs()

    def set_from_path(self, path: str | None, fps: int = 30, loop: bool = True, update: bool = True, allow_keep: bool = True) -> None:
        """Synchronous convenience wrapper (prebuild + apply in one call) for
        callers that don't need the lock-free/locked split -- load_background
        (already under _background_load_lock itself) and ScreenSaver's
        setters that act while already showing (plan §4 M3)."""
        kind, payload = self.prebuild_from_path(path, fps=fps, loop=loop, allow_keep=allow_keep)
        self.apply_prebuilt(kind, payload, fps=fps, loop=loop, update=update)

    def get_identified_tile(self, key_index: int) -> tuple | None:
        """(tile, (video md5, frame index)) for a video background, or None
        when there is no tile whose frame can be named (image/blank
        background, mid-rebuild, fallback frame). Tiles and identity are
        published as ONE pair and handed out as one read, so a concurrent
        update_tiles() can never let a caller pair this frame's pixels with
        the next frame's identity -- update_tiles() runs on the media tick
        but also on the GTK/screensaver threads (set_image/set_video/
        apply_prebuilt), so the media thread is not its only writer."""
        pair = self._identified_tiles
        if pair is None:
            return None
        tiles, identity = pair
        if key_index >= len(tiles):
            return None
        tile = tiles[key_index]
        if tile is None:
            return None
        return tile, identity

    def update_tiles(self) -> None:
        # Old tiles are reclaimed by refcounting once unreferenced; closing them
        # here would race a concurrent composite still holding one.
        try:
            identity = None
            if self.image is not None:
                self.tiles = self.image.get_tiles(extend_touchscreen=self._extend_effective())
            elif self.video is not None:
                # An extended video frame carries the strip slice as one extra
                # entry after the key tiles (see BackgroundVideoCache).
                entries, identity = self.video.get_next_tiles()
                key_count = self.deck_controller.deck.key_count()
                if self.video.extend_touchscreen and len(entries) > key_count:
                    self._video_strip = entries[key_count]
                    entries = entries[:key_count]
                self.tiles = entries
            else:
                self.tiles = [self.deck_controller.generate_alpha_key() for _ in range(self.deck_controller.deck.key_count())]
            self._identified_tiles = None if identity is None else (self.tiles, identity)
        except Exception:
            # A tile error must not kill the media thread; keep the old tiles.
            # Rate-limited: a broken video would otherwise log every frame.
            now = time.time()
            if now - getattr(self, "_last_tile_error_log", 0) > 10:
                self._last_tile_error_log = now
                log.opt(exception=True).error("Failed to update background tiles; keeping previous")

class BackgroundImage:
    def __init__(self, deck_controller: "DeckController", image: Image.Image, path: str | None = None) -> None:
        self.deck_controller = deck_controller
        # mem-plan P2.4: source-resolution RGBA used to be retained for the
        # whole page lifetime (design doc §3.2 -- "33MB for 4K"). `path` is
        # the source file `image` was decoded from, if any (None for
        # non-file-backed callers, e.g. the test harness) -- kept so a later
        # extend-to-touchscreen toggle that needs more canvas height than
        # the fitted copy retains can re-decode from source (see
        # _ensure_fits_canvas(), called from create_full_deck_sized_image()).
        self.path = path

        # Saturation is baked into the source image once, here, at load time.
        # create_full_deck_sized_image()/get_tiles()/get_touchscreen_image()
        # all derive from self.image, so the key tiles and the touchscreen
        # strip slice inherit the same single enhancement pass -- no
        # per-frame cost, no double-enhancement. Factor 1.0 (the default)
        # skips the ImageEnhance call and any mode conversion entirely, so
        # the stored image is byte-identical to today's behavior.
        image = self._prepare_image(image)
        # Nulled by close(); _ensure_fits_canvas/create_full_deck_sized_image
        # both handle the released state.
        self.image: Image.Image | None = self._fit_to_canvas(image, self._extend_effective())

    def _extend_effective(self) -> bool:
        # extend_to_touchscreen lives on Background (self.deck_controller.
        # background), not on DeckController itself -- mirrors Background.
        # _extend_effective's own condition (deck.is_touch()), minus its
        # "self.image is not None" check, which is about whether Background
        # currently has an image background at all, not about sizing one.
        background = getattr(self.deck_controller, "background", None)
        extend = bool(getattr(background, "extend_to_touchscreen", False)) if background is not None else False
        deck = getattr(self.deck_controller, "deck", None)
        return extend and deck is not None and deck.is_touch()

    def _prepare_image(self, image: Image.Image) -> Image.Image:
        saturation = self.deck_controller.get_display_saturation()
        if abs(saturation - 1.0) > 0.001:
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image = ImageEnhance.Color(image).enhance(saturation)
        return image

    def _canvas_size(self, extend_touchscreen: bool) -> "tuple[int, int] | None":
        """The full-deck canvas size create_full_deck_sized_image() targets,
        including the touchscreen strip when extend is on. None when the
        deck geometry isn't available (minimal test stubs exercising only
        the saturation step) -- fitting/re-decoding is then skipped, same
        as today's unconditional retention."""
        deck = getattr(self.deck_controller, "deck", None)
        if deck is None:
            return None
        key_rows, key_cols = deck.key_layout()
        key_width, key_height = self.deck_controller.get_key_image_size()
        spacing_x, spacing_y = self.deck_controller.key_spacing

        canvas_width = key_width * key_cols + spacing_x * (key_cols - 1)
        canvas_height = key_height * key_rows + spacing_y * (key_rows - 1)

        if extend_touchscreen and deck.is_touch():
            canvas_height += spacing_y + self._get_touchscreen_canvas_height(canvas_width)

        return (canvas_width, canvas_height)

    def _fit_to_canvas(self, image: Image.Image, extend_touchscreen: bool) -> Image.Image:
        canvas = self._canvas_size(extend_touchscreen)
        if canvas is None:
            return image
        budget = (canvas[0] * 2, canvas[1] * 2)
        if image.width > budget[0] or image.height > budget[1]:
            image.thumbnail(budget, Image.Resampling.LANCZOS)
        return image

    def _ensure_fits_canvas(self, extend_touchscreen: bool) -> None:
        """Re-decodes from `path` if the CURRENT canvas (which may have
        grown since __init__ -- the touchscreen-extend setting can be
        toggled at runtime without a fresh page/media load) needs more
        resolution than the retained image has."""
        if not self.path or self.image is None:
            return
        canvas = self._canvas_size(extend_touchscreen)
        if canvas is None:
            return
        if canvas[0] <= self.image.width and canvas[1] <= self.image.height:
            return
        try:
            with Image.open(self.path) as fresh:
                fresh = fresh.copy()
        except (OSError, FileNotFoundError):
            return
        fresh = self._prepare_image(fresh)
        old_image = self.image
        self.image = self._fit_to_canvas(fresh, extend_touchscreen)
        if old_image is not None:
            old_image.close()

    def close(self) -> None:
        """Releases the retained source-resolution PIL image (design doc
        bug 19: close_image_ressources()/DeckController.close() call this;
        BackgroundImage previously had no close() at all, an AttributeError
        waiting to happen the first time anything actually called it)."""
        if self.image is not None:
            self.image.close()
            self.image = None

    def create_full_deck_sized_image(self, extend_touchscreen: bool = False) -> Image.Image:
        self._ensure_fits_canvas(extend_touchscreen)
        key_rows, key_cols = self.deck_controller.deck.key_layout()
        key_width, key_height = self.deck_controller.get_key_image_size()
        spacing_x, spacing_y = self.deck_controller.key_spacing

        key_width *= key_cols
        key_height *= key_rows

        # Compute the total number of extra non-visible pixels that are obscured by
        # the bezel of the StreamDeck.
        total_spacing_x = spacing_x * (key_cols - 1)
        total_spacing_y = spacing_y * (key_rows - 1)

        # Compute final full deck image size, based on the number of buttons and
        # obscured pixels.
        canvas_width = key_width + total_spacing_x
        canvas_height = key_height + total_spacing_y

        # Grow the canvas below the key grid so the image continues onto the
        # touchscreen strip: one bezel gap plus the strip mapped into canvas
        # coordinates (the strip spans the full deck width).
        if extend_touchscreen:
            canvas_height += spacing_y + self._get_touchscreen_canvas_height(canvas_width)

        # close() releases the source image. Raise rather than compose a
        # transparent canvas: Background.update_tiles catches this and KEEPS the
        # previous tiles behind a rate-limited log, so the failure stays loud and
        # self-preserving. Returning a blank canvas here would silently blank
        # every key instead (and do it once per tile refresh, unlogged).
        source = self.image
        if source is None:
            raise RuntimeError(
                "background image was released (close()) while its tiles were "
                "still being composed"
            )

        # Convert to RGBA first to preserve transparency, then resize
        img_rgba = source.convert("RGBA")
        return ImageOps.fit(img_rgba, (canvas_width, canvas_height), Image.Resampling.LANCZOS)

    def _get_touchscreen_canvas_height(self, canvas_width: int) -> int:
        """Height of the touchscreen strip in key-grid canvas coordinates."""
        strip_width, strip_height = self.deck_controller.get_touchscreen_image_size()
        return round(strip_height * canvas_width / strip_width)

    def get_touchscreen_image(self) -> Image.Image:
        """The bottom slice of the extended canvas, at strip resolution."""
        canvas = self.create_full_deck_sized_image(extend_touchscreen=True)
        strip_width, strip_height = self.deck_controller.get_touchscreen_image_size()
        slice_height = self._get_touchscreen_canvas_height(canvas.width)
        strip_slice = canvas.crop(
            (0, canvas.height - slice_height, canvas.width, canvas.height)
        )
        return strip_slice.resize((strip_width, strip_height), Image.Resampling.LANCZOS)
    
    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key):
        deck = self.deck_controller.deck


        key_rows, key_cols = deck.key_layout()
        key_width, key_height = deck.key_image_format()['size']
        spacing_x, spacing_y = self.deck_controller.key_spacing

        # Determine which row and column the requested key is located on.
        row = key // key_cols
        col = key % key_cols

        # Compute the starting X and Y offsets into the full size image that the
        # requested key should display.
        start_x = col * (key_width + spacing_x)
        start_y = row * (key_height + spacing_y)

        # Compute the region of the larger deck image that is occupied by the given
        # key, and crop out that segment of the full image.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        segment = image.crop(region)

        # Return the segment directly, converting to RGBA to preserve transparency
        return segment.convert("RGBA")
    
    def get_tiles(self, extend_touchscreen: bool = False) -> list[Image.Image]:
        # Key crop coordinates are unaffected by the extension: the strip
        # region is appended below the key grid.
        full_deck_sized_image = self.create_full_deck_sized_image(extend_touchscreen)

        tiles: list[Image.Image] = []
        for key in range(self.deck_controller.deck.key_count()):
            key_image = self.crop_key_image_from_deck_sized_image(full_deck_sized_image, key)
            tiles.append(key_image)

        return tiles

class BackgroundVideo(BackgroundVideoCache):
    def __init__(self, deck_controller: "DeckController", video_path: str, loop: bool = True, fps: int = 30, extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller
        self.video_path = video_path
        self.loop = loop
        self.fps = fps

        self.page: Page | None = self.deck_controller.active_page

        self.active_frame: int = -1
        self._play_start: float | None = None  # wall-clock playback start, set on first real-time frame
        self._last_frame_tick: float | None = None  # last real-time frame pick, for gap clamping
        # Whether the tile cache's min-age has been retuned to this video's
        # real loop period. False until the first tick after the cache
        # completes: before that, playback is not running at source fps and
        # the loop period is not knowable (refresh_tile_cache_min_age).
        self._min_age_synced: bool = False

        super().__init__(video_path, deck_controller=deck_controller, extend_touchscreen=extend_touchscreen)

    def get_next_tiles(self) -> tuple[list[Image.Image | None], tuple | None]:
        """(tiles, identity) for the frame this tick lands on, where identity
        is (video md5, source frame index) or None when the tiles' frame
        can't be named (fallback/alpha payload). Returned as one pair so a
        caller can never file one frame's pixels under another's identity --
        the frame actually served is not always the one asked for (see
        Mp4FrameCache.get_frame_and_index)."""
        if self.is_cache_complete():
            if not self._min_age_synced:
                # First tick past cache completion. Up to here the frame set
                # was shielded by the conservative clamp maximum, because
                # sequential build playback has no knowable loop period; from
                # here frames are picked by wall clock at source fps, so the
                # real one can go in. One bool test per tick to get it.
                self._min_age_synced = True
                self.deck_controller.refresh_tile_cache_min_age(self)
            # Cache built -> any frame is a free lookup. Pick it by wall-clock so a
            # slow media loop drops frames (stays real-time) instead of playing the
            # video in slow-motion. Playback runs at the SOURCE's fps -- the
            # page's fps setting only limits how often the media loop renders
            # a new frame (the tick divider in MediaPlayerThread.run), it must
            # not change playback speed.
            playback_fps = float(self.get_source_fps() or self.fps or 30)
            now = time.time()
            if self._play_start is None:
                # Seed the timebase from the current position, not zero: the cache
                # completes mid-play (sequential decode or async disk load), and a
                # zero base would replay a non-looping video / jump a looping one.
                self._play_start = now - (self.active_frame + 1) / playback_fps
            elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                # Ticks stop while the page is away; shift the timebase across the
                # gap so playback resumes in place instead of fast-forwarding.
                self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
            self._last_frame_tick = now
            frame = int((now - self._play_start) * playback_fps)
            self.active_frame = frame % self.n_frames if self.loop else min(frame, self.n_frames - 1)
        else:
            # Still decoding into the cache: advance sequentially so every frame is
            # decoded (wall-clock jumps would leave gaps and force expensive seeks).
            self.active_frame += 1
            if self.active_frame >= self.n_frames and self.loop:
                self.active_frame = 0

        frame_index: int | None
        copied_tiles: list[Image.Image | None]
        tiles, frame_index = self.get_tiles_and_index(self.active_frame)
        try:
            # Defensive: every path through get_tiles_and_index() currently
            # yields real Images (decoded tiles, the last good payload, or
            # the alpha fallback), so this only fires if a future cache
            # substitutes None for a tile it could not decode.
            copied_tiles = [tile.copy() for tile in tiles]
        except AttributeError:
            copied_tiles = [None for _ in range(len(tiles))]
            frame_index = None
        identity = None if frame_index is None else (self.video_md5, frame_index)
        return copied_tiles, identity
