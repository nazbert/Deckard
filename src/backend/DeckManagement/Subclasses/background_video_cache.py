import bz2
import hashlib
import os
import pickle
import sys
import threading
import time
from PIL import Image, ImageOps
import cv2
import indexed_bzip2 as ibz2
from loguru import logger as log

import globals as gl

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")
os.makedirs(VID_CACHE, exist_ok=True)

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import DeckController

class BackgroundVideoCache:
    def __init__(self, video_path, deck_controller: "DeckController", extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller
        self.lock = threading.Lock()

        self.video_path = video_path
        self.cap = cv2.VideoCapture(video_path)
        self.n_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.cache = {}
        self.last_decoded_frame = None
        self.last_frame_index = -1

        self.video_md5 = self.get_video_hash()

        self.key_layout = self.deck_controller.deck.key_layout()
        self.key_count = self.deck_controller.deck.key_count()
        self.key_size = self.deck_controller.deck.key_image_format()['size']
        self.spacing = self.deck_controller.key_spacing

        # When extending onto the touchscreen strip, each cached frame carries
        # the strip slice as one extra entry after the key tiles, and the
        # canvas the frame is fitted to is taller — so extended caches are
        # incompatible with plain ones and live in their own directory.
        self.extend_touchscreen = extend_touchscreen and self.deck_controller.deck.is_touch()
        self.strip_size = self.deck_controller.get_touchscreen_image_size() if self.extend_touchscreen else None
        self.entries_per_frame = self.key_count + (1 if self.extend_touchscreen else 0)

        self.key_layout_str = f"{self.key_layout[0]}x{self.key_layout[1]}"
        if self.extend_touchscreen:
            self.key_layout_str += "+strip"

        self.cache_stored = False
        self._complete = False  # set once the frame cache is fully populated
        self._save_thread: threading.Thread = None

        thread = threading.Thread(target=self.load_cache, name="load_video_cache")
        thread.start()

        if self.is_cache_complete():
            log.info("Cache is complete. Closing the video capture.")
            self.cap.release()
        else:
            log.info("Cache is not complete. Continuing with video capture.")

        self.last_tiles: list[Image.Image] = []

        self.do_caching = gl.settings_manager.get_app_settings().get("performance", {}).get("cache-videos", True)

    def _generate_alpha_frame(self) -> list:
        """Fallback frame: transparent key tiles (and strip slice if extended)."""
        entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
        if self.extend_touchscreen:
            entries.append(Image.new("RGBA", self.strip_size, (0, 0, 0, 0)))
        return entries

    def get_tiles(self, n):
        # Check if cache is available (video may have been closed)
        if not hasattr(self, 'cache') or self.cache is None:
            return self._generate_alpha_frame()
        
        n = min(n, self.n_frames - 1)
        tiles = None
        with self.lock:
            if self.is_cache_complete():
                self.cap.release()
                return self.cache.get(n, None)
            
            # Otherwise, continue with video capture
            # Check if the frame is already decoded
            if n in self.cache:
                return self.cache[n]
            
            # If the requested frame is before the last decoded one, reset the capture
            if n < self.last_frame_index:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, n)
                self.last_frame_index = n - 1

            # Decode frames until the nth frame
            while self.last_frame_index < n:
                success, frame = self.cap.read()
                if not success:
                    break  # Reached the end of the video
                self.last_frame_index += 1
                
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  
                pil_image = Image.fromarray(frame_rgb)

                # Resize the image
                full_sized = self.create_full_deck_sized_image(pil_image)

                tiles: list[Image.Image] = []
                for key in range(self.key_count):
                    current_tiles = self.crop_key_image_from_deck_sized_image(full_sized, key)
                    tiles.append(current_tiles)

                if self.extend_touchscreen:
                    tiles.append(self.crop_strip_from_deck_sized_image(full_sized))

                if self.do_caching and self.cache is not None:
                    self.cache[self.last_frame_index] = tiles
                    # Persist only once every frame is stored — triggering
                    # mid-decode used to save a cache missing the tail frames.
                    if not self.cache_stored and self.is_cache_complete():
                        self.save_cache_threaded()
                self.last_tiles = tiles
                

                full_sized.close()
                pil_image.close()


        # Return the last decoded frame if the nth frame is not available
        if len(self.last_tiles) > 0:
            tiles = self.last_tiles
        if tiles is None:
            tiles = self._generate_alpha_frame()
        
        if self.cache is not None:
            return self.cache.get(n, tiles)
        return tiles
    
    def create_full_deck_sized_image(self, frame: Image.Image) -> Image.Image:
        key_rows, key_cols = self.key_layout
        key_width, key_height = self.key_size
        spacing_x, spacing_y = self.spacing

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

        # Extend the canvas below the key grid so the frame continues onto the
        # touchscreen strip: one bezel gap plus the strip mapped into canvas
        # coordinates (same geometry as BackgroundImage).
        if self.extend_touchscreen:
            canvas_height += spacing_y + self._get_strip_canvas_height(canvas_width)

        # Resize the image to suit the StreamDeck's full image size. We use the
        # helper function in Pillow's ImageOps module so that the image's aspect
        # ratio is preserved.
        return ImageOps.fit(frame, (canvas_width, canvas_height), Image.Resampling.HAMMING)

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
        # deck = self.deck_controller.deck
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
        
    def save_cache_threaded(self):
        t = threading.Thread(target=self.save_cache, name="save_video_cache")
        self._save_thread = t
        t.start()

    @log.catch
    def save_cache(self):
        """
        Store cache using pickle
        """
        if self.cache_stored:
            return
        self.cache_stored = True

        # Snapshot the dict: close() may drop self.cache while we pickle; the
        # snapshot keeps the frame images alive until the dump finishes.
        cache = self.cache
        if cache is None:
            return

        start = time.time()
        cache_path = os.path.join(VID_CACHE, self.key_layout_str, f"{self.video_md5}.cache")
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        data = cache.copy()

        with bz2.open(cache_path, "wb") as f:
            pickle.dump(data, f)

        log.success(f"Saved cache in {time.time() - start:.2f} seconds")
        self.last_save = time.time()
        del data


    @log.catch
    def load_cache(self, key_index: int = None):
        cache_path = os.path.join(VID_CACHE, self.key_layout_str, f"{self.video_md5}.cache")
        if not os.path.exists(cache_path):
            return

        _time = time.time()
        try:
            with ibz2.open(cache_path, parallelization=os.cpu_count()) as f:
                self.cache = pickle.load(f)
            log.success(f"Loaded cache in {time.time() - _time:.2f} seconds")
        except Exception as e:
            os.remove(cache_path)
            log.error(f"Failed to load cache: {e}")
            return

    def is_cache_complete(self) -> bool:
        # Fast path: the cache only grows until full (then is cleared on close),
        # so once complete skip the O(n_frames) rescan.
        if self._complete:
            return True
        if not hasattr(self, 'cache') or self.cache is None:
            return False
        if self.n_frames != len(self.cache):
            return False

        for key in self.cache:
            if len(self.cache[key]) != self.entries_per_frame:
                return False

        self._complete = True
        return True
    
    def close(self) -> None:
        import gc
        with self.lock:
            self.cap.release()

        save_thread = getattr(self, "_save_thread", None)
        if save_thread is not None and save_thread.is_alive():
            # A cache save is still pickling these images — closing them now
            # would corrupt the cache file ("Operation on closed image").
            # Leave the dict to the save thread (this instance is being
            # discarded); refcounting reclaims the frames once the save
            # finishes and both references are gone.
            self._complete = False
            return

        if hasattr(self, 'cache') and self.cache is not None:
            for n in self.cache:
                for f in self.cache[n]:
                    if f is not None:
                        f.close()

            self.cache.clear()
        self._complete = False
        self.cache = None
        gc.collect()