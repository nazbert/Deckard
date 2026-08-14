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
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

class InputVideo(SingleKeyAsset):
    def __init__(self, controller_input: "ControllerInput", video_path: str, fps: int = 30, loop: bool = True,
                 natural_speed: bool = False):
        super().__init__(
            controller_input=controller_input,
        )
        self.video_path = video_path
        self.fps = fps
        self.loop = loop
        # With natural_speed on, playback runs at the source fps whatever fps
        # says, and fps then only caps how often the owner re-renders. The
        # touchscreen background uses this. With natural_speed off, fps is the
        # playback rate, which is the key and dial media rule where fps
        # changes the speed.
        self.natural_speed = natural_speed

        # Shared-file registry. This instance owns its own reader (a
        # VideoCapture plus decode state), but any other key or dial showing
        # the same source, tile size and saturation shares the cache mp4 and
        # its detached builder thread. release(), called from close(),
        # detaches this reader and need not tear down the shared file.
        self.video_cache: mp4_tile_cache.KeyVideoCache | None = mp4_tile_cache.acquire(
            video_path,
            self.controller_input.get_image_size(),
            self.deck_controller.get_display_saturation(),
        )
        # Serializes close() against a running get_next_frame(). Load and
        # teardown threads call close() while a render tick sits between its
        # video_cache reads. get_next_frame holds this lock for its whole
        # body, so close() waits for the frame in flight and no frame starts
        # against a released reader. A get_frame() after the release can
        # resurrect a capture through _maybe_adopt_shared_cache and leak it.
        self._close_lock = threading.Lock()

        self.active_frame: int = -1
        # Wall-clock picking state. It mirrors BackgroundVideo.get_next_tiles
        # in DeckController.py, and both branches are live (see
        # docs/presenter-migration-plan.md).
        self._play_start: float | None = None  # wall-clock playback start, set on first real-time frame
        self._last_frame_tick: float | None = None  # last real-time frame pick, for gap clamping

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        # Check, then hold. The unlocked peek makes the post-close path free
        # for stragglers after teardown, and the check runs again under the
        # lock, because close() can win the race between peek and acquire.
        # The lock covers the whole pick and decode, so close() waits instead
        # of releasing the reader mid-read.
        if self.video_cache is None:
            return None
        with self._close_lock:
            cache = self.video_cache
            if cache is None:
                return None

            if now is None:
                now = time.time()

            # A degenerate source, e.g. a corrupt file or bad metadata,
            # reports 0 frames. That makes is_cache_complete() trivially true,
            # and frame % 0 raises.
            if cache.n_frames <= 0:
                return None

            if cache.is_cache_complete():
                # The cache is built, so any frame is a free lookup. Pick it by
                # wall clock, so a slow media loop drops frames and stays
                # real-time instead of playing the video in slow motion.
                playback_fps = float(self.fps or 30)
                if self.natural_speed:
                    playback_fps = float(cache.get_source_fps() or playback_fps)
                if self._play_start is None:
                    # Seed the timebase from the current position and not
                    # from zero. The cache can complete mid-play through the
                    # sequential decode, and a zero base replays a non-looping
                    # video or jumps a looping one.
                    self._play_start = now - (self.active_frame + 1) / playback_fps
                elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                    # Ticks stop while the page is away. Shift the timebase
                    # across the gap, so playback resumes in place instead of
                    # fast-forwarding.
                    self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
                self._last_frame_tick = now
                elapsed = now - self._play_start
                if self.natural_speed:
                    # fps is the owner's render cap. Quantize the timebase, so
                    # the picked frame advances at most fps times per second.
                    # The quantization belongs in this picker, because other
                    # animated content, such as a deck background video or a
                    # dial, can re-trigger composites at any rate, and
                    # per-owner tick gates never see that. Inside one cap
                    # window the pick is
                    # identical, so the owner's hash dedup drops the redundant
                    # device write.
                    cap = max(1.0, float(self.fps or 30))
                    elapsed = int(elapsed * cap) / cap
                frame = int(elapsed * playback_fps)
                n_frames = cache.n_frames
                self.active_frame = frame % n_frames if self.loop else min(frame, n_frames - 1)
            else:
                # The decode into the cache is still running. Advance
                # sequentially, so every frame gets decoded. A wall-clock jump
                # leaves gaps and forces expensive seeks and decode-on-demand
                # under the cache lock (docs/presenter-migration-plan.md).
                self.active_frame += 1
                if self.active_frame >= cache.n_frames and self.loop:
                    self.active_frame = 0

            return cache.get_frame(self.active_frame)

    def set_playback(self, fps: int, loop: bool) -> None:
        """Applies a new fps and loop to a playing video, at the same position.

        Without natural_speed, wall-clock picking computes frame = elapsed *
        fps, so a change of fps with no rebase of the start time jumps the
        position by the whole elapsed factor. With natural_speed the timebase
        runs on the source fps, and fps is only the owner's render cap.
        """
        if not self.natural_speed and (self.fps or 30) != (fps or 30) and self._play_start is not None:
            self._play_start = time.time() - (self.active_frame + 1) / float(fps or 30)
        self.fps = fps
        self.loop = loop

    def get_raw_image(self) -> Image.Image | None:
        # None once the reader closes, or when the source is degenerate. See
        # get_next_frame's early returns.
        return self.get_next_frame()

    def close(self) -> None:
        """Detaches this reader from the shared tile-cache registry.

        SingleKeyAsset's default close() does nothing, and this override must
        stay, or nothing releases the VideoCapture that video_cache holds and
        ControllerKeyState and ControllerDialState.close_resources() leak it.
        The method is idempotent, and a second call finds video_cache already
        None. _close_lock serializes it against get_next_frame, so it waits
        for a frame in flight, and every later call sees video_cache is None.
        """
        with self._close_lock:
            if self.video_cache is not None:
                mp4_tile_cache.release(self.video_cache)
                self.video_cache = None
