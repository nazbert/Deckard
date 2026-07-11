import os

import cv2
import numpy as np
from PIL import Image
from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import Mp4FrameCache, VID_CACHE

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import DeckController

class BackgroundVideoCache(Mp4FrameCache):
    """Background video, cached as a re-encoded video at deck-canvas resolution.

    The source video is decoded once (during the first playthrough); each
    canvas-fitted frame is appended to a small mp4 in the cache directory.
    From then on frames are decoded on demand from that file, so no frame
    data is held in RAM beyond the decoder's own buffers. Key tiles and the
    touchscreen strip slice are cropped out of the canvas frame per request.

    Build/promote/decode-ahead discipline lives in `Mp4FrameCache`
    (mp4_tile_cache.py) -- this class keeps the tiling/strip/saturation-crop
    logic that is specific to the background path; behavior is unchanged
    from before the extraction (single instance, build interleaved with
    playback ticks, same on-disk directory layout/naming).
    """

    def __init__(self, video_path, deck_controller: "DeckController", extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller

        self.key_layout = self.deck_controller.deck.key_layout()
        self.key_count = self.deck_controller.deck.key_count()
        self.key_size = self.deck_controller.deck.key_image_format()['size']
        self.spacing = self.deck_controller.key_spacing

        # When extending onto the touchscreen strip, each frame carries the
        # strip slice as one extra entry after the key tiles, and the canvas
        # the frame is fitted to is taller — so extended caches are
        # incompatible with plain ones and live in their own directory.
        self.extend_touchscreen = extend_touchscreen and self.deck_controller.deck.is_touch()
        self.strip_size = self.deck_controller.get_touchscreen_image_size() if self.extend_touchscreen else None
        self.entries_per_frame = self.key_count + (1 if self.extend_touchscreen else 0)

        self.key_layout_str = f"{self.key_layout[0]}x{self.key_layout[1]}"
        if self.extend_touchscreen:
            self.key_layout_str += "+strip"

        self._legacy_cache_path: str = None  # set by _default_cache_path()

        saturation = deck_controller.get_display_saturation()
        super().__init__(video_path, out_size=self._canvas_size(), saturation=saturation)

    # --- geometry / cache-path hooks --------------------------------------

    def _default_cache_path(self) -> str:
        # entry.split(".")[0] (video_cache_sweeper.py) still resolves this to
        # video_md5 with the suffix present, since the suffix is appended
        # after the first dot-delimited component -- verified, sweeper needs
        # no changes.
        cache_dir = os.path.join(VID_CACHE, self.key_layout_str)
        self._legacy_cache_path = os.path.join(cache_dir, f"{self.video_md5}.cache")
        return os.path.join(cache_dir, f"{self.video_md5}{self._sat_suffix}.mp4")

    def _canvas_size(self) -> tuple[int, int]:
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

        key_width *= key_cols
        key_height *= key_rows

        # Compute the total number of extra non-visible pixels that are obscured by
        # the bezel of the StreamDeck.
        total_spacing_x = spacing_x * (key_cols - 1)
        total_spacing_y = spacing_y * (key_rows - 1)

        canvas_width = key_width + total_spacing_x
        canvas_height = key_height + total_spacing_y

        # Extend the canvas below the key grid so the frame continues onto the
        # touchscreen strip: one bezel gap plus the strip mapped into canvas
        # coordinates (same geometry as BackgroundImage).
        if self.extend_touchscreen:
            canvas_height += spacing_y + self._get_strip_canvas_height(canvas_width)

        return (canvas_width, canvas_height)

    def _on_promoted(self) -> None:
        self._remove_legacy_cache()

    def _writer_enabled(self) -> bool:
        # Unlike KeyVideoCache (gated once by its registry's acquire()),
        # this single self-contained instance decides for itself whether to
        # build -- same "performance.cache-videos" read/behavior as before
        # the Mp4FrameCache extraction.
        return gl.settings_manager.get_app_settings().get("performance", {}).get("cache-videos", True)

    def _remove_legacy_cache(self) -> None:
        # Pre-rewrite caches were bz2'd pickles of raw frame tiles — large
        # and no longer readable by this code.
        if self._legacy_cache_path and os.path.isfile(self._legacy_cache_path):
            try:
                os.remove(self._legacy_cache_path)
                log.info(f"Removed legacy pickle video cache {self._legacy_cache_path}")
            except OSError:
                pass

    # --- frame access ------------------------------------------------------

    def _generate_alpha_frame(self) -> list:
        """Fallback frame: transparent key tiles (and strip slice if extended)."""
        entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
        if self.extend_touchscreen:
            entries.append(Image.new("RGBA", self.strip_size, (0, 0, 0, 0)))
        return entries

    def _fallback_payload(self):
        # Mp4FrameCache.get_frame already prefers `self.last_payload` (the
        # last successfully decoded tile list) over calling this; it only
        # lands here when nothing has ever decoded successfully yet.
        return self._generate_alpha_frame()

    def get_tiles(self, n: int) -> list[Image.Image]:
        return self.get_frame(n)

    def _payload_from_bgr(self, frame_bgr: np.ndarray) -> list[Image.Image]:
        canvas = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        entries = [
            self.crop_key_image_from_deck_sized_image(canvas, key)
            for key in range(self.key_count)
        ]
        if self.extend_touchscreen:
            entries.append(self.crop_strip_from_deck_sized_image(canvas))
        return entries

    def _get_strip_canvas_height(self, canvas_width: int) -> int:
        """Height of the touchscreen strip in key-grid canvas coordinates."""
        strip_width, strip_height = self.strip_size
        return round(strip_height * canvas_width / strip_width)

    def crop_strip_from_deck_sized_image(self, image: Image.Image) -> Image.Image:
        """The bottom slice of the extended canvas, at strip resolution."""
        slice_height = self._get_strip_canvas_height(image.width)
        strip_slice = image.crop(
            (0, image.height - slice_height, image.width, image.height)
        )
        return strip_slice.resize(self.strip_size, Image.Resampling.HAMMING)

    def crop_key_image_from_deck_sized_image(self, image: Image.Image, key):
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

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
        return image.crop(region)
