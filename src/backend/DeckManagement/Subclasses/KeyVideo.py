"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import time

from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from src.backend.DeckManagement.Subclasses import mp4_tile_cache
from PIL import Image

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import ControllerInput

class InputVideo(SingleKeyAsset):
    def __init__(self, controller_input: "ControllerInput", video_path: str, fps: int = 30, loop: bool = True):
        super().__init__(
            controller_input=controller_input,
        )
        self.video_path = video_path
        self.fps = fps
        self.loop = loop

        # Shared-file registry (docs/memory-footprint-impl-plan.md P2.1/P2.2):
        # this instance owns its own reader (VideoCapture + decode state),
        # but the underlying cache mp4 -- and its detached builder thread --
        # are shared with any other key/dial showing the same
        # (source, tile size, saturation). release() (see close()) detaches
        # this reader; it does not necessarily tear down the shared file.
        self.video_cache = mp4_tile_cache.acquire(
            video_path,
            self.controller_input.get_image_size(),
            self.deck_controller.get_display_saturation(),
        )

        self.active_frame: int = -1
        # Wall-clock picking state (mirrors BackgroundVideo.get_next_tiles,
        # DeckController.py -- both branches are load-bearing, see
        # presenter-migration-plan.md §4 M4 / §6 deviation 2).
        self._play_start: float = None  # wall-clock playback start, set on first real-time frame
        self._last_frame_tick: float = None  # last real-time frame pick, for gap clamping

    def get_next_frame(self, now: float = None) -> Image:
        if now is None:
            now = time.time()

        # Degenerate source (corrupt file / bad metadata): 0 frames makes
        # is_cache_complete() trivially true and `frame % 0` would raise.
        if self.video_cache.n_frames <= 0:
            return None

        if self.video_cache.is_cache_complete():
            # Cache built -> any frame is a free lookup. Pick it by wall-clock
            # so a slow media loop drops frames (stays real-time) instead of
            # playing the video in slow-motion.
            if self._play_start is None:
                # Seed the timebase from the current position, not zero: the
                # cache can complete mid-play (sequential decode), and a zero
                # base would replay a non-looping video / jump a looping one.
                self._play_start = now - (self.active_frame + 1) / float(self.fps or 30)
            elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                # Ticks stop while the page is away; shift the timebase across
                # the gap so playback resumes in place instead of fast-forwarding.
                self._play_start += (now - self._last_frame_tick) - 1.0 / float(self.fps or 30)
            self._last_frame_tick = now
            frame = int((now - self._play_start) * (self.fps or 30))
            n_frames = self.video_cache.n_frames
            self.active_frame = frame % n_frames if self.loop else min(frame, n_frames - 1)
        else:
            # Still decoding into the cache: advance sequentially so every
            # frame is decoded (wall-clock jumps would leave gaps and force
            # expensive seeks/decode-on-demand under the cache lock -- decode
            # amplification, presenter-migration-plan.md C-F8).
            self.active_frame += 1
            if self.active_frame >= self.video_cache.n_frames and self.loop:
                self.active_frame = 0

        return self.video_cache.get_frame(self.active_frame)

    def get_raw_image(self) -> Image.Image:
        return self.get_next_frame()

    def close(self) -> None:
        """Real close() (design doc bug 18/19): SingleKeyAsset's default is a
        no-op, so before this fix nothing ever released video_cache's
        VideoCapture -- ControllerKeyState/ControllerDialState.close_resources()
        called this and silently leaked. Detaches this reader from the
        shared tile-cache registry; idempotent (a second call finds
        video_cache already None)."""
        if self.video_cache is not None:
            mp4_tile_cache.release(self.video_cache)
            self.video_cache = None
