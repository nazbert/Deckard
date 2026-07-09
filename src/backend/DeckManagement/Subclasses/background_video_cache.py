import hashlib
import os
import threading

import cv2
import numpy as np
from PIL import Image, ImageOps
from loguru import logger as log

import globals as gl

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")
os.makedirs(VID_CACHE, exist_ok=True)

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import DeckController

class BackgroundVideoCache:
    """Background video, cached as a re-encoded video at deck-canvas resolution.

    The source video is decoded once (during the first playthrough); each
    canvas-fitted frame is appended to a small mp4 in the cache directory.
    From then on frames are decoded on demand from that file, so no frame
    data is held in RAM beyond the decoder's own buffers. Key tiles and the
    touchscreen strip slice are cropped out of the canvas frame per request.
    """

    # Forward jumps up to this many frames are bridged by decoding and
    # discarding (cheaper than a container seek at canvas resolution);
    # anything larger, or backward, is a real seek.
    MAX_DECODE_AHEAD = 30

    def __init__(self, video_path, deck_controller: "DeckController", extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller
        self.lock = threading.Lock()

        self.video_path = video_path
        self.video_md5 = self.get_video_hash()

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

        cache_dir = os.path.join(VID_CACHE, self.key_layout_str)
        os.makedirs(cache_dir, exist_ok=True)
        self.cache_path = os.path.join(cache_dir, f"{self.video_md5}.mp4")
        self._legacy_cache_path = os.path.join(cache_dir, f"{self.video_md5}.cache")
        # Unique per instance: two decks building the same video concurrently
        # must not write the same temp file (os.replace makes last-wins safe).
        self._writer_tmp_path = os.path.join(cache_dir, f"{self.video_md5}.{os.getpid()}-{id(self):x}.tmp.mp4")

        self._complete = False
        self._cache_cap: cv2.VideoCapture = None
        self._cache_pos = 0  # index of the next frame _cache_cap will return
        self._last_entry: tuple[int, list[Image.Image]] = None
        self.last_tiles: list[Image.Image] = []

        self.cap: cv2.VideoCapture = None
        self._writer: cv2.VideoWriter = None
        self._frames_written = 0
        self.last_frame_index = -1  # source decode position while building

        self.n_frames = 0
        self.do_caching = gl.settings_manager.get_app_settings().get("performance", {}).get("cache-videos", True)

        if not self._open_existing_cache():
            self._open_source()

    # --- setup -----------------------------------------------------------

    def _open_cache_capture(self) -> cv2.VideoCapture:
        # A canvas-resolution stream decodes at thousands of fps single
        # threaded; the default lets FFmpeg spawn a 16-thread frame pool
        # per capture.
        return cv2.VideoCapture(self.cache_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])

    def _open_existing_cache(self) -> bool:
        if not os.path.isfile(self.cache_path):
            return False
        cap = self._open_cache_capture()
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if cap.isOpened() else 0
        if n_frames <= 0:
            cap.release()
            log.warning(f"Removing unreadable video cache {self.cache_path}")
            try:
                os.remove(self.cache_path)
            except OSError:
                pass
            return False
        self._cache_cap = cap
        self._cache_pos = 0
        self.n_frames = n_frames
        self._complete = True
        self._remove_legacy_cache()
        log.info(f"Using cached canvas video ({n_frames} frames): {self.cache_path}")
        return True

    def _open_source(self) -> None:
        self.cap = cv2.VideoCapture(self.video_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 4])
        self.n_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not self.do_caching:
            return
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        size = self._canvas_size()
        writer = cv2.VideoWriter(self._writer_tmp_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
        if writer.isOpened():
            self._writer = writer
        else:
            log.warning(f"Could not open canvas cache writer for {self.video_path}; playing uncached")

    def _remove_legacy_cache(self) -> None:
        # Pre-rewrite caches were bz2'd pickles of raw frame tiles — large
        # and no longer readable by this code.
        if os.path.isfile(self._legacy_cache_path):
            try:
                os.remove(self._legacy_cache_path)
                log.info(f"Removed legacy pickle video cache {self._legacy_cache_path}")
            except OSError:
                pass

    # --- frame access ----------------------------------------------------

    def _generate_alpha_frame(self) -> list:
        """Fallback frame: transparent key tiles (and strip slice if extended)."""
        entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
        if self.extend_touchscreen:
            entries.append(Image.new("RGBA", self.strip_size, (0, 0, 0, 0)))
        return entries

    def get_tiles(self, n: int) -> list[Image.Image]:
        with self.lock:
            if self._complete:
                entries = self._get_cached_entries(n)
            else:
                entries = self._decode_source_entries(n)
        if entries is not None:
            self.last_tiles = entries
            return entries
        # Keep showing the last good frame over a transient decode failure.
        if len(self.last_tiles) > 0:
            return self.last_tiles
        return self._generate_alpha_frame()

    def _get_cached_entries(self, n: int) -> list[Image.Image]:
        n = max(0, min(n, self.n_frames - 1))
        if self._last_entry is not None and self._last_entry[0] == n:
            return self._last_entry[1]
        cap = self._cache_cap
        if cap is None:
            return None
        if n < self._cache_pos or n > self._cache_pos + self.MAX_DECODE_AHEAD:
            cap.set(cv2.CAP_PROP_POS_FRAMES, n)
            self._cache_pos = n
        frame = None
        while self._cache_pos <= n:
            success, frame = cap.read()
            if not success:
                # Container metadata overcounted; clamp to what is readable.
                self.n_frames = max(1, self._cache_pos)
                return None
            self._cache_pos += 1
        entries = self._entries_from_bgr(frame)
        self._last_entry = (n, entries)
        return entries

    def _decode_source_entries(self, n: int) -> list[Image.Image]:
        if self.cap is None:
            return None
        if self.n_frames > 0:
            n = max(0, min(n, self.n_frames - 1))
        if self._last_entry is not None and self._last_entry[0] == n:
            return self._last_entry[1]

        # A backward request while building would append frames out of order,
        # so the partial cache is dropped and playback continues uncached
        # (the next instance rebuilds from scratch).
        if n < self.last_frame_index:
            self._abort_writer()
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, n)
            self.last_frame_index = n - 1

        entries = None
        while self.last_frame_index < n:
            success, frame = self.cap.read()
            if not success:
                self._end_of_source()
                if self._complete:
                    return self._get_cached_entries(n)
                return None
            self.last_frame_index += 1
            canvas_bgr = self._canvas_from_source_bgr(frame)
            if self._writer is not None:
                self._writer.write(canvas_bgr)
                self._frames_written += 1
            if self.last_frame_index == n:
                entries = self._entries_from_bgr(canvas_bgr)

        # The frame-count metadata is usually exact, so the last read
        # succeeds and never trips the end-of-stream branch above; promote
        # the cache as soon as every promised frame has been written.
        if self.n_frames > 0 and self.last_frame_index >= self.n_frames - 1:
            self._end_of_source()

        if entries is not None:
            self._last_entry = (n, entries)
        return entries

    def _end_of_source(self) -> None:
        """Source exhausted: promote the built cache, or clamp n_frames if
        the source's metadata promised more frames than it delivered."""
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            if self._frames_written > 0:
                try:
                    os.replace(self._writer_tmp_path, self.cache_path)
                except OSError:
                    log.opt(exception=True).error("Failed to store canvas video cache")
                else:
                    cap = self._open_cache_capture()
                    if cap.isOpened():
                        self._cache_cap = cap
                        self._cache_pos = 0
                        self.n_frames = self._frames_written
                        self._complete = True
                        self._remove_legacy_cache()
                        log.success(
                            f"Cached canvas video ({self._frames_written} frames, "
                            f"{os.path.getsize(self.cache_path) / 1e6:.1f} MB): {self.cache_path}"
                        )
            else:
                self._remove_writer_tmp()

        if self._complete:
            self.cap.release()
        elif self.last_frame_index >= 0:
            self.n_frames = self.last_frame_index + 1

    # --- geometry --------------------------------------------------------

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

    def _canvas_from_source_bgr(self, frame: np.ndarray) -> np.ndarray:
        """Fit a source frame (BGR) to the deck canvas, preserving aspect ratio."""
        pil_image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        canvas = ImageOps.fit(pil_image, self._canvas_size(), Image.Resampling.HAMMING)
        return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)

    def _entries_from_bgr(self, frame: np.ndarray) -> list[Image.Image]:
        canvas = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
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

    def get_video_hash(self) -> str:
        sha1sum = hashlib.md5()
        with open(self.video_path, 'rb') as video:
            block = video.read(2**16)
            while len(block) != 0:
                sha1sum.update(block)
                block = video.read(2**16)
            return sha1sum.hexdigest()

    def is_cache_complete(self) -> bool:
        return self._complete

    # --- teardown --------------------------------------------------------

    def _abort_writer(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        self._remove_writer_tmp()

    def _remove_writer_tmp(self) -> None:
        try:
            if os.path.isfile(self._writer_tmp_path):
                os.remove(self._writer_tmp_path)
        except OSError:
            pass

    def close(self) -> None:
        with self.lock:
            if self.cap is not None:
                self.cap.release()
            if self._cache_cap is not None:
                self._cache_cap.release()
                self._cache_cap = None
            self._abort_writer()
            self._complete = False
            self._last_entry = None
            self.last_tiles = []
