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

Background composites the loaded media into the key tiles, and into the
touchscreen strip slice on an SD+. Media resolution runs in two phases:
prebuild_from_path() builds the new object lock-free because a video hash
and a capture open take seconds; apply_prebuilt() swaps under the lock.
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

    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.PageManagement.Page import Page


class Background:
    def __init__(self, deck_controller: "DeckController"):
        self.deck_controller = deck_controller

        self.image: "BackgroundImage | None" = None
        self.video: "BackgroundVideo | None" = None

        # Extend the background onto the SD+ touchscreen strip. An image slice
        # is memoized; the strip re-composites on every dial label change.
        # update_tiles() refreshes _video_strip once per video frame.
        self.extend_to_touchscreen: bool = False
        self._touchscreen_slice: Image.Image | None = None
        self._video_strip: Image.Image | None = None

        # update_tiles() replaces the whole list and nothing mutates it in
        # place. Sequence accepts both element types: a source yields all-Image
        # entries, or None entries on the video cache fallback path.
        self.tiles: Sequence[Image.Image | None] = [None] * deck_controller.deck.key_count()
        # (tiles, (video md5, frame index)) for the frame tiles holds. None
        # when the frame has no name. See get_identified_tile().
        self._identified_tiles: tuple | None = None

    def set_image(self, image: "BackgroundImage", update: bool = True) -> None:
        self.image = image
        if self.video is not None:
            self.video.close()
        self.video = None
        self._touchscreen_slice = None
        self._video_strip = None
        # A content change orphans every cached native. Each key holds the
        # previous background's composited pixels, hashes or frames. Clear
        # them here, or they stay dead until LRU eviction reaches them.
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
        # As in set_image(), a content change orphans every cached native. The
        # md5 in a native tile key makes a source swap collision-free. The
        # clear stops the old video's frames from lingering.
        self._identified_tiles = None
        self.deck_controller.clear_encoded_key_caches()
        # Shield the frame entries for the new video's loop duration.
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
            # update_tiles() refreshes this once per video frame. None unless
            # the video carries extend_touchscreen.
            return self._video_strip
        image = self.image
        if image is None or not self._extend_effective():
            return None
        if self._touchscreen_slice is None:
            self._touchscreen_slice = image.get_touchscreen_image()
        return self._touchscreen_slice

    def prebuild_from_path(self, path: str | None, fps: int = 30, loop: bool = True, allow_keep: bool = True):
        """Build the new background object lock-free, without a touch on
        self.video, self.image or the deck. apply_prebuilt() swaps it in.
        Returns (kind, payload): blank clears the background, noop keeps the
        current one, keep refreshes page, fps and loop only, and video or
        image carries a new object."""
        if path == "":
            path = None
        if path is None:
            return ("blank", None)
        if is_video(path):
            extend = self.extend_to_touchscreen and self.deck_controller.deck.is_touch()
            if allow_keep:
                # The extend mode and the saturation factor bake into the
                # video's canvas geometry and its cache file. A change to
                # either forces a rebuild for the same path, or a playing
                # video keeps showing the old factor.
                if (self.video is not None and self.video.video_path == path
                        and self.video.extend_touchscreen == extend
                        and abs(self.video.saturation - self.deck_controller.get_display_saturation()) <= 0.001):
                    # Carry the path so apply_prebuilt re-checks it. This
                    # verdict is lock-free, and a load_background that races
                    # it can swap self.video first. GifBackground carries the
                    # same three attributes, so a GIF that fell back to cv2
                    # keeps the fallback and skips the failed PIL decode.
                    return ("keep", path)
            if os.path.splitext(path)[1].lower() == ".gif":
                # A .gif goes to the PIL provider so alpha and the per-frame
                # delay timeline survive. The cv2 demuxer drops both. Over
                # budget or undecodable, fall back to the opaque source-fps
                # cv2 path below instead of an OOM risk. The keep-check
                # above stops the warning from repeating.
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
        """Release the resources of a prebuilt payload that no caller applied.
        A video or image payload holds a cv2 capture or a PIL image, and a
        drop without close() leaks it. keep, noop and blank hold nothing."""
        if kind not in ("video", "image") or payload is None:
            return
        try:
            payload.close()
        except Exception:
            log.opt(exception=True).warning(
                "Failed to close an orphaned prebuilt background payload during close()"
            )

    def apply_prebuilt(self, kind: str, payload, fps: int = 30, loop: bool = True, update: bool = True) -> None:
        """Apply the result of prebuild_from_path(). The screensaver
        transition calls this under _background_load_lock, after it re-checks
        the generation. This does no file I/O; it assigns the objects and
        fans out update_all_inputs()."""
        # A load_background that passed its generation gate before close()
        # bumped the generation arrives here with a live payload. The close() sweep already ran, or waits on the lock, so an
        # attach now leaks. close() sets _closing before the sweep.
        if getattr(self.deck_controller, "_closing", False):
            self._discard_prebuilt(kind, payload)
            return
        if kind == "noop":
            return
        if kind == "keep":
            # Re-check the lock-free keep verdict against the current video.
            # A load_background that raced the prebuild can swap in a
            # different file. A mismatch does nothing and self-heals on the
            # next transition, instead of corrupting that video's settings.
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
        """Prebuild and apply in one call, for a caller that does not need the
        lock-free split: load_background, which already holds
        _background_load_lock, and the ScreenSaver setters that act while it
        shows."""
        kind, payload = self.prebuild_from_path(path, fps=fps, loop=loop, allow_keep=allow_keep)
        self.apply_prebuilt(kind, payload, fps=fps, loop=loop, update=update)

    def get_identified_tile(self, key_index: int) -> tuple | None:
        """(tile, (video md5, frame index)) for a video background, or None
        when no tile has a nameable frame. Tiles and identity publish as one
        pair and read as one, so a concurrent update_tiles() cannot pair this
        frame's pixels with the next frame's identity. The media tick, the
        GTK thread and the screensaver thread all call update_tiles()."""
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
        # Refcounting reclaims the old tiles. A close() here races a
        # concurrent composite that still holds one.
        try:
            identity = None
            if self.image is not None:
                self.tiles = self.image.get_tiles(extend_touchscreen=self._extend_effective())
            elif self.video is not None:
                # An extended video frame carries the strip slice as one extra
                # entry after the key tiles. See BackgroundVideoCache.
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
            # A tile error must not kill the media thread. Keep the old tiles
            # and rate-limit the log, because a broken video fails every
            # frame.
            now = time.time()
            if now - getattr(self, "_last_tile_error_log", 0) > 10:
                self._last_tile_error_log = now
                log.opt(exception=True).error("Failed to update background tiles; keeping previous")

class BackgroundImage:
    def __init__(self, deck_controller: "DeckController", image: Image.Image, path: str | None = None) -> None:
        self.deck_controller = deck_controller
        # The source file that image came from, or None for a caller with no
        # file (the test harness). An extend-to-touchscreen toggle can need
        # more canvas height than the fitted copy holds; _ensure_fits_canvas()
        # then re-decodes from this path.
        self.path = path

        # Bake the saturation into the source image once, at load time. The
        # key tiles and the strip slice both derive from self.image, so they
        # inherit one enhancement pass at no per-frame cost. Factor 1.0 skips
        # the ImageEnhance call and the mode conversion, so the bytes stay.
        image = self._prepare_image(image)
        # close() sets this to None. _ensure_fits_canvas() and
        # create_full_deck_sized_image() both handle the released state.
        self.image: Image.Image | None = self._fit_to_canvas(image, self._extend_effective())

    def _extend_effective(self) -> bool:
        # extend_to_touchscreen lives on Background, not on DeckController.
        # This repeats the deck.is_touch() condition of
        # Background._extend_effective without its image check; that check
        # asks if an image background exists, not how to size one.
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
        """The canvas size that create_full_deck_sized_image() targets, with
        the touchscreen strip when extend is on. Returns None when the deck
        geometry is absent; the caller then skips the fit and the re-decode."""
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
        """Re-decode from path when the current canvas needs more resolution
        than the retained image holds. The canvas grows when the user toggles
        touchscreen-extend at runtime, with no fresh page load."""
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
        """Release the retained source-resolution PIL image."""
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

        # Count the pixels that the deck bezel hides.
        total_spacing_x = spacing_x * (key_cols - 1)
        total_spacing_y = spacing_y * (key_rows - 1)

        canvas_width = key_width + total_spacing_x
        canvas_height = key_height + total_spacing_y

        # Grow the canvas below the key grid so the image continues onto the
        # strip: one bezel gap plus the strip in canvas coordinates. The
        # strip spans the full deck width.
        if extend_touchscreen:
            canvas_height += spacing_y + self._get_touchscreen_canvas_height(canvas_width)

        # close() releases the source image. Raise instead of composing a
        # transparent canvas. Background.update_tiles catches the raise and
        # keeps the previous tiles behind a rate-limited log. A blank canvas blanks
        # every key without a log, once per tile refresh.
        source = self.image
        if source is None:
            raise RuntimeError(
                "background image was released (close()) while its tiles were "
                "still being composed"
            )

        # Convert to RGBA before the resize to keep transparency.
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

        # Find the row and the column of the requested key.
        row = key // key_cols
        col = key % key_cols

        # Find the X and Y offset of the key in the full-size image.
        start_x = col * (key_width + spacing_x)
        start_y = row * (key_height + spacing_y)

        # Crop the region that the key occupies.
        region = (start_x, start_y, start_x + key_width, start_y + key_height)
        segment = image.crop(region)

        # Convert to RGBA to keep transparency.
        return segment.convert("RGBA")
    
    def get_tiles(self, extend_touchscreen: bool = False) -> list[Image.Image]:
        # The extension does not move the key crop coordinates; the strip
        # region goes below the key grid.
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
        self._play_start: float | None = None  # wall-clock playback start, set on the first real-time frame
        self._last_frame_tick: float | None = None  # last real-time frame pick, for gap clamping
        # True after the tile cache min-age moves to this video's loop period.
        # The first tick past cache completion sets it. Before that, playback
        # does not run at source fps and the loop period is unknown.
        self._min_age_synced: bool = False

        super().__init__(video_path, deck_controller=deck_controller, extend_touchscreen=extend_touchscreen)

    def get_next_tiles(self) -> tuple[list[Image.Image | None], tuple | None]:
        """(tiles, identity) for the frame this tick lands on. identity is
        (video md5, source frame index), or None for a fallback or alpha
        payload. One pair keeps the pixels with their identity, because
        Mp4FrameCache.get_frame_and_index can serve a different frame."""
        if self.is_cache_complete():
            if not self._min_age_synced:
                # First tick past cache completion. Until now the clamp
                # maximum shielded the frame set, because sequential build
                # playback has no loop period. From here the wall clock picks
                # frames at source fps, so the real loop period applies.
                self._min_age_synced = True
                self.deck_controller.refresh_tile_cache_min_age(self)
            # A full cache makes any frame a free lookup. Pick by wall clock so
            # a slow media loop drops frames instead of playing in slow motion.
            # Playback runs at the source fps. The page fps setting limits
            # how often the media loop renders a frame, and must not change
            # the speed.
            playback_fps = float(self.get_source_fps() or self.fps or 30)
            now = time.time()
            if self._play_start is None:
                # Seed the timebase from the current position. The cache
                # completes mid-play, and a zero base replays a non-looping
                # video or jumps a looping one.
                self._play_start = now - (self.active_frame + 1) / playback_fps
            elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                # Ticks stop while the page is away. Shift the timebase across
                # the gap so playback continues in place, with no fast-forward.
                self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
            self._last_frame_tick = now
            frame = int((now - self._play_start) * playback_fps)
            self.active_frame = frame % self.n_frames if self.loop else min(frame, self.n_frames - 1)
        else:
            # The cache is still decoding, so advance sequentially and let
            # the decoder read every frame. A wall-clock jump leaves a gap and
            # forces an expensive seek.
            self.active_frame += 1
            if self.active_frame >= self.n_frames and self.loop:
                self.active_frame = 0

        frame_index: int | None
        copied_tiles: list[Image.Image | None]
        tiles, frame_index = self.get_tiles_and_index(self.active_frame)
        try:
            # Every path through get_tiles_and_index() yields real Images:
            # decoded tiles, the last good payload, or the alpha fallback.
            # This catch fires only if a cache puts None in place of a tile
            # that it cannot decode.
            copied_tiles = [tile.copy() for tile in tiles]
        except AttributeError:
            copied_tiles = [None for _ in range(len(tiles))]
            frame_index = None
        identity = None if frame_index is None else (self.video_md5, frame_index)
        return copied_tiles, identity
