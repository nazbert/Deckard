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
import threading
import time

from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from src.backend.DeckManagement.Subclasses import mp4_tile_cache
from PIL import Image

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import ControllerInput

class InputVideo(SingleKeyAsset):
    def __init__(self, controller_input: "ControllerInput", video_path: str, fps: int = 30, loop: bool = True,
                 natural_speed: bool = False):
        super().__init__(
            controller_input=controller_input,
        )
        self.video_path = video_path
        self.fps = fps
        self.loop = loop
        # natural_speed: play at the SOURCE's fps regardless of `fps` -- the
        # setting then only caps how often the owner re-renders (the
        # touchscreen background uses this). Off: `fps` IS the playback rate
        # (key/dial media semantics -- their fps setting changes speed).
        self.natural_speed = natural_speed

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
        # Serializes close() against an in-flight get_next_frame(): close can
        # be called from load/teardown threads while a render tick is between
        # its video_cache reads (issue #19). get_next_frame holds this for
        # its whole body, so close() waits for the in-flight frame and no
        # frame can start against a released reader (a post-release
        # get_frame() could even resurrect a capture via
        # _maybe_adopt_shared_cache and leak it).
        self._close_lock = threading.Lock()

        self.active_frame: int = -1
        # Wall-clock picking state (mirrors BackgroundVideo.get_next_tiles,
        # DeckController.py -- both branches are load-bearing, see
        # presenter-migration-plan.md §4 M4 / §6 deviation 2).
        self._play_start: float = None  # wall-clock playback start, set on first real-time frame
        self._last_frame_tick: float = None  # last real-time frame pick, for gap clamping

    def get_next_frame(self, now: float = None) -> Image:
        # Check-then-hold: the unlocked peek makes the post-close hot path
        # (stragglers after teardown) free, then the lock is re-checked --
        # close() may have won the race between peek and acquire. While a
        # frame is in flight the lock is held for the whole pick+decode, so
        # close() blocks instead of releasing the reader mid-read.
        if self.video_cache is None:
            return None
        with self._close_lock:
            cache = self.video_cache
            if cache is None:
                return None

            if now is None:
                now = time.time()

            # Degenerate source (corrupt file / bad metadata): 0 frames makes
            # is_cache_complete() trivially true and `frame % 0` would raise.
            if cache.n_frames <= 0:
                return None

            if cache.is_cache_complete():
                # Cache built -> any frame is a free lookup. Pick it by wall-clock
                # so a slow media loop drops frames (stays real-time) instead of
                # playing the video in slow-motion.
                playback_fps = float(self.fps or 30)
                if self.natural_speed:
                    playback_fps = float(cache.get_source_fps() or playback_fps)
                if self._play_start is None:
                    # Seed the timebase from the current position, not zero: the
                    # cache can complete mid-play (sequential decode), and a zero
                    # base would replay a non-looping video / jump a looping one.
                    self._play_start = now - (self.active_frame + 1) / playback_fps
                elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                    # Ticks stop while the page is away; shift the timebase across
                    # the gap so playback resumes in place instead of fast-forwarding.
                    self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
                self._last_frame_tick = now
                elapsed = now - self._play_start
                if self.natural_speed:
                    # `fps` is the owner's render cap: quantize the timebase so
                    # the picked frame advances at most `fps` times per second.
                    # The quantization must live HERE, in the picker -- composites
                    # can be re-triggered at any rate by OTHER animated content
                    # (deck background video, dials), which per-owner tick gates
                    # never see. Within a cap window the pick is identical, so
                    # the owner's hash dedup drops the redundant device write.
                    cap = max(1.0, float(self.fps or 30))
                    elapsed = int(elapsed * cap) / cap
                frame = int(elapsed * playback_fps)
                n_frames = cache.n_frames
                self.active_frame = frame % n_frames if self.loop else min(frame, n_frames - 1)
            else:
                # Still decoding into the cache: advance sequentially so every
                # frame is decoded (wall-clock jumps would leave gaps and force
                # expensive seeks/decode-on-demand under the cache lock -- decode
                # amplification, presenter-migration-plan.md C-F8).
                self.active_frame += 1
                if self.active_frame >= cache.n_frames and self.loop:
                    self.active_frame = 0

            return cache.get_frame(self.active_frame)

    def set_playback(self, fps: int, loop: bool) -> None:
        """Applies new fps/loop to an already-playing video, preserving the
        current position: without natural_speed, wall-clock picking computes
        frame = elapsed * fps, so changing fps without rebasing the start
        time would jump the playback position by the whole elapsed factor.
        With natural_speed the timebase runs on the source's fps and `fps`
        is only the owner's render cap -- no rebase needed."""
        if not self.natural_speed and (self.fps or 30) != (fps or 30) and self._play_start is not None:
            self._play_start = time.time() - (self.active_frame + 1) / float(fps or 30)
        self.fps = fps
        self.loop = loop

    def get_raw_image(self) -> Image.Image:
        return self.get_next_frame()

    def close(self) -> None:
        """Real close() (design doc bug 18/19): SingleKeyAsset's default is a
        no-op, so before this fix nothing ever released video_cache's
        VideoCapture -- ControllerKeyState/ControllerDialState.close_resources()
        called this and silently leaked. Detaches this reader from the
        shared tile-cache registry; idempotent (a second call finds
        video_cache already None). Serialized against get_next_frame via
        _close_lock: waits for an in-flight frame, and every later call
        sees video_cache is None (issue #19)."""
        with self._close_lock:
            if self.video_cache is not None:
                mp4_tile_cache.release(self.video_cache)
                self.video_cache = None
