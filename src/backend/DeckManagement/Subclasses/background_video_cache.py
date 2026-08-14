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
    from src.backend.DeckManagement.deck_controller.controller import DeckController

class BackgroundVideoCache(Mp4FrameCache):
    """Background video, cached as a re-encoded video at deck-canvas resolution.

    The source video is decoded once (during the first playthrough); each
    canvas-fitted frame is appended to a small mp4 in the cache directory.
    From then on frames are decoded on demand from that file, so no frame
    data is held in RAM beyond the decoder's own buffers. Key tiles and the
    touchscreen strip slice are cropped out of the canvas frame per request.

    Mp4FrameCache (mp4_tile_cache.py) owns the build, promote and
    decode-ahead discipline. This class keeps the tiling, strip and
    saturation-crop logic of the background path: one instance, a build
    interleaved with playback ticks, and the on-disk directory layout and
    naming.
    """

    def __init__(self, video_path, deck_controller: "DeckController", extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller

        self.key_layout = self.deck_controller.deck.key_layout()
        self.key_count = self.deck_controller.deck.key_count()
        self.key_size = self.deck_controller.deck.key_image_format()['size']
        self.spacing = self.deck_controller.key_spacing

        # When the frame extends onto the touchscreen strip, it carries the
        # strip slice as one extra entry after the key tiles, and the canvas
        # is taller. Extended caches are therefore incompatible with plain
        # ones and live in their own directory.
        self.extend_touchscreen = extend_touchscreen and self.deck_controller.deck.is_touch()
        # The annotation follows the real value: a (width, height) pair.
        self.strip_size: tuple[int, int] | None = (
            self.deck_controller.get_touchscreen_image_size()
            if self.extend_touchscreen else None)
        self.entries_per_frame = self.key_count + (1 if self.extend_touchscreen else 0)

        self.key_layout_str = f"{self.key_layout[0]}x{self.key_layout[1]}"
        if self.extend_touchscreen:
            self.key_layout_str += "+strip"

        self._legacy_cache_path: str | None = None  # set by _default_cache_path()

        saturation = deck_controller.get_display_saturation()
        super().__init__(video_path, out_size=self._canvas_size(), saturation=saturation)

    # Geometry and cache-path hooks.

    def _default_cache_path(self) -> str:
        # entry.split(".")[0] in video_cache_sweeper.py still resolves this to
        # video_md5 with the suffix present, because the suffix comes after
        # the first dot-delimited component. The sweeper needs no change.
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

        # Extend the canvas below the key grid, so the frame continues onto
        # the touchscreen strip: one bezel gap plus the strip mapped into
        # canvas coordinates. This matches BackgroundImage geometry.
        if self.extend_touchscreen:
            canvas_height += spacing_y + self._get_strip_canvas_height(canvas_width)

        return (canvas_width, canvas_height)

    def _on_promoted(self) -> None:
        self._remove_legacy_cache()

    def _writer_enabled(self) -> bool:
        # This instance is self-contained and decides for itself whether to
        # build. KeyVideoCache instead gates once through its registry's
        # acquire(). Both read the "performance.cache-videos" setting.
        return gl.settings_manager.app().cache_videos

    def _remove_legacy_cache(self) -> None:
        # A legacy cache is a bz2 pickle of raw frame tiles. It is large, and
        # no code here can read it.
        if self._legacy_cache_path and os.path.isfile(self._legacy_cache_path):
            try:
                os.remove(self._legacy_cache_path)
                log.info(f"Removed legacy pickle video cache {self._legacy_cache_path}")
            except OSError:
                pass

    # Frame access.

    def _require_strip_size(self) -> tuple[int, int]:
        """The strip size, or a raise.

        strip_size is set when extend_touchscreen is on, which is the only
        condition under which the strip helpers below run. There is one
        exception. get_touchscreen_image_size() returns None for a dead deck,
        so a cache built against a dying deck can reach here extended and
        sizeless. The raise contains that at one site instead of a None unpack
        three call sites deep.
        """
        strip_size = self.strip_size
        if strip_size is None:
            raise RuntimeError(
                "this background video cache has no touchscreen strip size "
                "(not extended, or the deck was already gone when it was built)")
        return strip_size

    def _generate_alpha_frame(self) -> list:
        """Fallback frame: transparent key tiles (and strip slice if extended)."""
        entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
        if self.extend_touchscreen:
            entries.append(Image.new("RGBA", self._require_strip_size(), (0, 0, 0, 0)))
        return entries

    def _fallback_payload(self):
        # Mp4FrameCache.get_frame prefers self.last_payload, the last tile
        # list it decoded, over this call. It reaches here only when no decode
        # has succeeded yet.
        return self._generate_alpha_frame()

    def get_tiles(self, n: int) -> list[Image.Image]:
        return self.get_frame(n)

    def get_tiles_and_index(self, n: int) -> tuple[list[Image.Image], int]:
        """get_tiles() plus the source frame index the tiles come from. The
        index is None when unknown. See Mp4FrameCache.get_frame_and_index."""
        return self.get_frame_and_index(n)

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
        strip_width, strip_height = self._require_strip_size()
        return round(strip_height * canvas_width / strip_width)

    def crop_strip_from_deck_sized_image(self, image: Image.Image) -> Image.Image:
        """The bottom slice of the extended canvas, at strip resolution."""
        slice_height = self._get_strip_canvas_height(image.width)
        strip_slice = image.crop(
            (0, image.height - slice_height, image.width, image.height)
        )
        return strip_slice.resize(self._require_strip_size(), Image.Resampling.HAMMING)

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
