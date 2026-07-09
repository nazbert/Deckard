"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import bisect
import collections
import gc
import itertools
import os
import statistics
import threading
import time
# Import Python modules
from concurrent.futures import ThreadPoolExecutor, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from copy import copy
from dataclasses import dataclass
from threading import Thread, Timer

import psutil
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageSequence
from StreamDeck.Devices import StreamDeck
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus
from loguru import logger as log

# Import own modules
from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.HelperMethods import *
from src.backend.DeckManagement.ImageHelpers import *
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.backend.DeckManagement.Subclasses.ActionPermissionManager import ActionPermissionManager
from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.ScreenSaver import ScreenSaver
from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.Subclasses.encoded_image_cache import EncodedImageCache
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.mem_telemetry import page_switches
from src.backend import timer_wheel
from src.backend.PageManagement.Page import ActionOutdated, Page, NoActionHolderFound
from src.api import notify_active_page_changed

process = psutil.Process()

from gi.repository import GLib

# Import signals
from src.Signals import Signals

# Import typing
from typing import TYPE_CHECKING, cast

from src.windows.mainWindow.elements.KeyGrid import KeyButton, KeyGrid
from src.backend.PluginManager.ActionCore import ActionCore
if TYPE_CHECKING:
    from src.windows.mainWindow.elements.DeckStackChild import DeckStackChild
    from src.backend.DeckManagement.DeckManager import DeckManager

# Import globals
import globals as gl

import io


def encode_native_key(deck, image: "Image.Image", quality: int = 90) -> bytes:
    """PILHelper.to_native_key_format with tunable JPEG quality (the library
    hardcodes q100): smaller JPEGs mean fewer serial USB HID writes per key."""
    fmt = deck.key_image_format()
    if image.size != fmt["size"]:
        image.thumbnail(fmt["size"])
    if fmt["rotation"]:
        image = image.rotate(fmt["rotation"])
    if fmt["flip"][0]:
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
    if fmt["flip"][1]:
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        save_kwargs = {"quality": quality}
        if fmt["format"] == "JPEG":
            # Below quality 95 Pillow silently switches to 4:2:0 chroma
            # subsampling, which halves color resolution both axes -- a
            # measured ~4% average desaturation plus chroma smear on busy
            # 120px tiles. Force 4:4:4: keeps q90's encode speed, costs
            # ~17% bytes (noise at current USB headroom).
            save_kwargs["subsampling"] = 0
        image.save(buf, fmt["format"], **save_kwargs)
        return buf.getvalue()


def encode_native_touchscreen(deck, image: "Image.Image", quality: int = 90) -> bytes:
    """PILHelper.to_native_touchscreen_format with tunable JPEG quality (the
    library hardcodes q100) and without mutating the caller's image in place
    (the library's `_to_native_format` calls `image.thumbnail()` in place when
    resizing, which corrupts the caller's copy). The touchscreen strip is the
    largest single USB write on the deck, so a smaller JPEG here buys back
    time under the device write mutex -- dial-latency margin. The caller
    (`ControllerTouchScreen.update`) reuses the same image object afterward
    for the UI mirror, so any resize here must operate on a copy."""
    fmt = deck.touchscreen_image_format()
    if image.size != fmt["size"]:
        image = image.copy()
        image.thumbnail(fmt["size"])
    if fmt["rotation"]:
        image = image.rotate(fmt["rotation"])
    if fmt["flip"][0]:
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
    if fmt["flip"][1]:
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        save_kwargs = {"quality": quality}
        if fmt["format"] == "JPEG":
            # Same 4:4:4 rationale as encode_native_key: below q95 Pillow
            # switches to 4:2:0 chroma subsampling, visibly desaturating the
            # strip's icons/text.
            save_kwargs["subsampling"] = 0
        image.save(buf, fmt["format"], **save_kwargs)
        return buf.getvalue()


@dataclass
class MediaPlayerTask:
    deck_controller: "DeckController"
    page: Page
    _callable: callable
    args: tuple
    kwargs: dict

    def run(self):
        self._callable(*self.args, **self.kwargs)

@dataclass
class MediaPlayerSetTouchscreenImageTask:
    deck_controller: "DeckController"
    page: Page
    native_image: bytes
    config_gen: int = None  # generation of the content rendered; dropped at present if stale
    submit_seq: int = None  # writer's monotonic submit-seq stamp; None for pre-M1 construction
    controller_touchscreen: "ControllerTouchScreen" = None  # stamped once this paint is presented
    img_hash: int = None  # hash of the presented image, recorded in run()

    def run(self):
        if not self.deck_controller.deck.is_touch():
            return
        try:
            touchscreen_size = self.deck_controller.get_touchscreen_image_size()
            self.deck_controller.deck.set_touchscreen_image(self.native_image, x_pos=0, y_pos=0, width=touchscreen_size[0], height=touchscreen_size[1]) # Maybe avoid to always merge the dial images before applying it
            # Record the presented image's hash here, not at render time: a paint
            # dropped at the present boundary must not advance the hash, or the
            # correcting render would hash-skip and the touchscreen would bleed
            # forever (mirrors MediaPlayerSetImageTask, plan §3).
            if self.controller_touchscreen is not None:
                self.controller_touchscreen._last_img_hash = self.img_hash
            self.native_image = None
            del self.native_image
            self.deck_controller._on_write_result(True)
        except StreamDeck.TransportError as e:
            log.error(f"Failed to set deck touchscreen image. Error: {e}")
            # Graduated error policy (plan §9.1/§4 M2): always attempt and
            # swallow -- controller removal comes solely from USB disconnect
            # events, not from a write-failure count.
            self.deck_controller._on_write_result(False)

@dataclass
class MediaPlayerSetImageTask:
    deck_controller: "DeckController"
    page: Page
    key_index: int
    native_image: bytes
    config_gen: int = None  # generation of the content rendered; dropped at present if stale
    controller_key: "ControllerKey" = None  # stamped once this paint is presented
    img_hash: int = None  # hash of the presented image, recorded in run()
    submit_seq: int = None  # writer's monotonic submit-seq stamp; None for pre-M1 construction

    def run(self):
        try:
            if media_prof:
                _t0 = time.perf_counter()
            self.deck_controller.deck.set_key_image(self.key_index, self.native_image)
            if media_prof:
                media_prof.add("usb_write", time.perf_counter() - _t0)
            # Record the presented image's hash here, not at render time: a paint
            # dropped at the present boundary must not advance the hash, or the
            # correcting render would hash-skip and the key would bleed forever.
            if self.controller_key is not None:
                self.controller_key._last_img_hash = self.img_hash
            self.native_image = None
            del self.native_image
            self.deck_controller._on_write_result(True)
        except StreamDeck.TransportError as e:
            log.error(f"Failed to set deck key image. Error: {e}")
            # Graduated error policy (plan §9.1/§4 M2): always attempt and
            # swallow -- controller removal comes solely from USB disconnect
            # events, not from a write-failure count.
            self.deck_controller._on_write_result(False)


@dataclass
class SetBrightnessMsg:
    """Control message: set device brightness. Executed on the media thread
    (the sole writer) via MediaPlayerThread.control_q -- see
    docs/presenter-migration-plan.md §2.1."""
    value: float


@dataclass
class ClearMsg:
    """Control message: blank the deck. `seq` is the submitting thread's
    monotonic submit-sequence counter value *at submission time*
    (MediaPlayerThread.next_submit_seq()) -- executing this wipes only
    image/touchscreen tasks stamped with a lower submit_seq, so frames
    submitted after this Clear was requested survive and paint afterward
    (plan §2.1, preserves the caller's clear-then-paint order)."""
    seq: int


@dataclass
class ClearAndCloseMsg:
    """Control message: terminal. Wipes pending image/touchscreen tasks,
    writes blanks, best-effort closes the device, and stops the media
    thread's loop (plan §2.1/§2.4)."""
    pass


@dataclass
class ReleaseStashedInputsMsg:
    """Control message (mem-plan P2.6): closes every stashed input's media
    resources, then empties the dict in place. Used by ScreenSaver.show()
    to release the previous page's input set (design doc bug 8) shortly
    after swapping it out, instead of leaving it pinned for the whole
    screensaver duration.

    A control message rather than a generic add_task() (MediaPlayerTask):
    add_task's tasks are dropped unrun if `task.page is not active_page` by
    the time the batch executes (perform_media_player_tasks) -- correct for
    stale renders, but wrong here, since a hide()-triggered load_page()
    changing active_page before this drains must not cause the release to
    be silently skipped. Control messages have no such page affinity and
    always execute, FIFO, like ClearMsg/SetBrightnessMsg."""
    stashed_inputs: dict


class MediaPlayerThread(threading.Thread):
    # An image batch at or above this size is a bulk repaint (video frame /
    # full-page paint) and gets inter-write yields; below it is interactive
    # (press feedback, plugin set_media) and writes immediately.
    BULK_BATCH_THRESHOLD = 4
    # Within a bulk batch, yield after every N writes (see the comment at
    # the batch loop in perform_media_player_tasks).
    YIELD_STRIDE = 3

    def __init__(self, deck_controller: "DeckController"):
        super().__init__(name="MediaPlayerThread", daemon=True)
        self.deck_controller: DeckController = deck_controller
        self.FPS = 30 # Max refresh rate of the internal displays

        # Cap how often a background video repaints the device. The Stream Deck
        # transport serializes all reads and writes on one mutex; a back-to-back
        # write flood can out-race the 20Hz HID read poll for it (the lock is
        # unfair), so dial encoder events arrive coalesced and lag badly. The
        # inter-write yield below guarantees the reader a mutex slot inside
        # bulk batches. 20 is the measured sweet spot: 30 was validated on
        # dedup-friendly content (mostly-static tiles, ~80 writes/s) but a
        # high-entropy video defeats tile dedup entirely (~270 candidate
        # writes/s) and drags the media loop to ~19fps; 20 sustains 26+ loop
        # fps on that same worst case. 0 disables the cap.
        self._video_write_hz = float(os.environ.get("STREAMCONTROLLER_VIDEO_WRITE_HZ", 20))
        self._last_video_write = 0.0

        # Inter-write yield inside bulk batches (seconds); see the comment in
        # perform_media_player_tasks. This pacing is the mechanism that keeps
        # the HID reader responsive at the 30Hz default above.
        self._inter_write_yield = float(os.environ.get("STREAMCONTROLLER_WRITE_YIELD_MS", 1.5)) / 1000.0

        self.running = False
        self.media_ticks = 0

        self._stop = False

        self.tasks: list[MediaPlayerTask] = []
        self.image_tasks = {}
        self.touchscreen_task = None
        self._wake_event = threading.Event()

        # Control queue (plan §2.1): append/popleft are GIL-atomic, so no
        # extra lock is needed. Drained fully, first, every wake -- before
        # any animation tick or task work -- so control ops (brightness,
        # clear) never wait behind ticker/task work.
        self.control_q: collections.deque = collections.deque()
        # Per-writer monotonic stamp counter: image/touchscreen tasks are
        # stamped with next(self._submit_seq) at submission (add_image_task/
        # add_touchscreen_task), and a Clear captures the counter at its own
        # submission (next_submit_seq()) so it can tell which already-queued
        # frames predate it (plan §2.1/§2.2).
        self._submit_seq = itertools.count()

        # Wall-clock gap detection (plan §4 M2): a gap much larger than the
        # loop's own wait interval means the process was suspended (system
        # sleep) and just resumed -- DetectResumeThread's proven technique,
        # relocated into this loop instead of a separate thread. See
        # check_resume_gap().
        self._last_iter_ts: float = time.time()

        self.fps: list[float] = []
        self.old_warning_state = False

        self.show_fps_warnings = gl.settings_manager.get_app_settings().get("warnings", {}).get("enable-fps-warnings", True)

    # @log.catch
    def run(self):
        self.running = True

        while True:
            start = time.time()

            self.check_resume_gap(start)
            self.deck_controller._run_pending_repaint()

            # 1. Drain the control queue fully, first, every wake (plan
            # §2.2) -- before any animation tick or task work, and before
            # honoring a pending stop. Order matters: stop()'s caller
            # (close_all()) submits a terminal ClearAndCloseMsg and then
            # immediately calls stop() -- if _stop were checked before this
            # drain (or at the bottom of the loop, after a wake that raced
            # stop()'s flag-set against this iteration's work), the just-
            # submitted terminal message could be stranded unprocessed. Every
            # iteration drains first, unconditionally, THEN looks at _stop.
            if not self.drain_control_queue():
                break
            if self._stop:
                break

            # Read by the FPS throttle below even when paused.
            has_bg_video = False

            bg_strip_dirty = False
            video_repaint = False

            if self.deck_controller.background.video is not None:
                if self.deck_controller.background.video.page is self.deck_controller.active_page:
                    has_bg_video = True
                    # Rate-limit the video's device writes so the flood
                    # doesn't starve the HID read thread (see _video_write_hz).
                    min_gap = 1.0 / self._video_write_hz if self._video_write_hz > 0 else 0
                    if start - self._last_video_write >= min_gap:
                        video_repaint = True
                        self._last_video_write = start
                    # Background video: guard the tick divider against fps<=0/None
                    # (would ZeroDivisionError) and >FPS; 0/None plays at loop FPS.
                    video_fps = self.deck_controller.background.video.fps or self.FPS
                    video_each_nth_frame = max(1, self.FPS // min(self.FPS, video_fps))
                    if video_repaint and self.media_ticks % video_each_nth_frame == 0:
                        self.deck_controller.background.update_tiles()
                        # A video extended onto the strip needs the shared
                        # touchscreen re-composited for the new frame.
                        bg_strip_dirty = self.deck_controller.background.get_touchscreen_image() is not None

            # Only iterate keys if there is animated content to update
            if video_repaint or self._needs_key_ticks():
                #TODO: generalize
                for key in self.deck_controller.inputs[Input.Key]:
                    cast("ControllerKey", key).on_media_player_tick()

                # Dials share one touchscreen; render it at most once per
                # frame instead of once per dial.
                dials = self.deck_controller.inputs[Input.Dial]
                touchscreen_dirty = False
                for dial in dials:
                    if cast("ControllerDial", dial).on_media_player_tick():
                        touchscreen_dirty = True
                if (touchscreen_dirty or bg_strip_dirty) and dials:
                    cast("ControllerDial", dials[0]).get_touch_screen().update()

            # Perform media player tasks
            self.perform_media_player_tasks()

            self.media_ticks += 1

            end = time.time()

            if media_prof:
                media_prof.add("tick", end - start)
                media_prof.maybe_report()

            # Use low FPS when idle (no animated content, no pending tasks)
            has_pending = bool(self.tasks or self.image_tasks or self.touchscreen_task)
            if has_pending or has_bg_video or getattr(self, '_cached_needs_ticks', False):
                target_fps = self.FPS
            else:
                target_fps = 2  # Idle: just check for new tasks occasionally

            self.append_fps(1 / (end - start))
            self.update_low_fps_warning()
            wait = max(0, 1/target_fps - (end - start))
            # Event-based wait in both paths (plan §2.2 point 4): a submitted
            # control op or an interactive paint wakes the loop immediately
            # instead of waiting out a full active-FPS tick.
            self._wake_event.wait(wait)
            self._wake_event.clear()

            # No _stop check here (it moved to the top, right after the
            # control-queue drain -- see the comment there): the loop always
            # goes around once more and drains before honoring a stop.

        self.running = False

    def next_submit_seq(self) -> int:
        """Allocates the next value from the writer's monotonic submit-seq
        counter. Used internally by add_image_task/add_touchscreen_task to
        stamp tasks, and externally (DeckController.clear()) to capture the
        counter at a Clear's submission time (plan §2.1)."""
        return next(self._submit_seq)

    def submit_control(self, msg) -> None:
        """Non-blocking: append + wake. Safe from any thread (deque append
        is GIL-atomic; no lock needed) -- plan §2.1.

        Rejects once the writer is stopped/closing (design doc bug 12): the
        loop is gone by then, so nothing would ever drain a message appended
        after this point -- without this guard, control_q would grow
        unbounded for the rest of the process's life if a late plugin/API
        callback keeps calling e.g. set_brightness() on a torn-down deck."""
        if self._stop:
            return
        self.control_q.append(msg)
        self._wake_event.set()

    def drain_control_queue(self) -> bool:
        """Executes every pending control message, FIFO. Returns False if a
        terminal message (ClearAndCloseMsg) was processed -- the caller must
        then stop the loop. Split out from run() so unit-tier scenarios can
        drive the control queue without spinning the thread (the M0 harness's
        stub controller never starts the thread -- see tests/fixtures.py)."""
        while self.control_q:
            msg = self.control_q.popleft()
            if isinstance(msg, SetBrightnessMsg):
                self._exec_set_brightness(msg)
            elif isinstance(msg, ClearMsg):
                self._exec_clear(msg)
            elif isinstance(msg, ClearAndCloseMsg):
                self._exec_clear_and_close()
                return False
            elif isinstance(msg, ReleaseStashedInputsMsg):
                self._exec_release_stashed_inputs(msg)
            else:
                log.error(f"Unknown control message: {msg!r}")
        return True

    def _exec_set_brightness(self, msg: "SetBrightnessMsg") -> None:
        # Direct device write, not DeckController.set_brightness() (which
        # would just re-submit and loop forever). Graduated error policy
        # (plan §9.1/§4 M2): always attempt and swallow, reported to the
        # unified per-controller handler like the task classes.
        try:
            self.deck_controller.deck.set_brightness(int(msg.value))
            self.deck_controller._on_write_result(True)
        except Exception as e:
            log.error(f"Failed to set brightness: {e}")
            self.deck_controller._on_write_result(False)

    def _exec_release_stashed_inputs(self, msg: "ReleaseStashedInputsMsg") -> None:
        """mem-plan P2.6: runs on the media player thread, serialized with
        every render/write it does -- see ReleaseStashedInputsMsg's
        docstring for why this is a control message and not add_task()."""
        stashed_inputs = msg.stashed_inputs
        for inputs in list(stashed_inputs.values()):
            for controller_input in list(inputs):
                try:
                    controller_input.close_resources()
                except Exception:
                    log.opt(exception=True).warning(
                        "Failed to close a stashed screensaver input (ReleaseStashedInputsMsg)"
                    )
        stashed_inputs.clear()

    def check_resume_gap(self, now: float = None) -> bool:
        """Detects a wall-clock gap >=5s between media-loop iterations -- the
        signature of a process suspend/resume cycle (plan §4 M2; the
        technique is DetectResumeThread's, relocated into this loop instead
        of a separate thread). Split out from run() so unit-tier scenarios
        can drive it without spinning the thread (mirrors
        drain_control_queue's rationale). Returns whether a gap was
        detected -- NOT whether a repaint actually fired, since
        _schedule_full_repaint() applies its own rate limit."""
        if now is None:
            now = time.time()
        gap = now - self._last_iter_ts
        self._last_iter_ts = now
        if gap >= 5.0:
            log.info(f"Media loop observed a {gap:.1f}s gap since its last iteration "
                      f"(likely a suspend/resume); scheduling a full repaint.")
            self.deck_controller._schedule_full_repaint()
            return True
        return False

    def _exec_clear(self, msg: "ClearMsg") -> None:
        # Wipe only slots whose frame predates this Clear -- frames submitted
        # after this Clear survive and paint afterward, which is what makes
        # the queued Clear order-preserving against the caller's
        # clear-then-paint sequence (plan §2.1).
        for key in list(self.image_tasks.keys()):
            task = self.image_tasks.get(key)
            if task is not None and task.submit_seq is not None and task.submit_seq < msg.seq:
                del self.image_tasks[key]
        if (self.touchscreen_task is not None and self.touchscreen_task.submit_seq is not None
                and self.touchscreen_task.submit_seq < msg.seq):
            self.touchscreen_task = None
        # Reset dedup state on every current input BEFORE writing the blanks
        # (plan §3): otherwise an identical repaint after this Clear would
        # still match the pre-clear cached hash and get wrongly skipped,
        # leaving the device stuck on blank.
        self.deck_controller._reset_dedup_hashes()
        try:
            self.deck_controller._write_blank_frames()
        except Exception as e:
            log.error(f"Failed to write blank frames for Clear: {e}")

    def _exec_clear_and_close(self) -> None:
        # Set before doing any of the work below (not just relying on the
        # external stop() call that follows submitting this message): the
        # window between this terminal message landing and stop() actually
        # being called is exactly when a late submit_control() would
        # otherwise still be accepted into a queue nothing will ever drain
        # again (design doc bug 12).
        self._stop = True
        self.image_tasks.clear()
        self.touchscreen_task = None
        self.deck_controller._reset_dedup_hashes()
        try:
            self.deck_controller._write_blank_frames()
        except Exception as e:
            log.error(f"Failed to write blank frames during ClearAndClose: {e}")
        try:
            self.deck_controller.deck.close()
        except Exception as e:
            log.error(f"Failed to close deck during ClearAndClose: {e}")

    def _needs_key_ticks(self) -> bool:
        # True if any input has animated content that advances on the media tick:
        # a key/dial video or a scrolling label.
        needs = False
        for key in self.deck_controller.inputs.get(Input.Key, []):
            state = key.get_active_state()
            if state.key_video is not None or state.label_manager.get_has_scroll_labels():
                needs = True
                break
        if not needs:
            for dial in self.deck_controller.inputs.get(Input.Dial, []):
                state = dial.get_active_state()
                if state.video is not None or state.label_manager.get_has_scroll_labels():
                    needs = True
                    break
        self._cached_needs_ticks = needs
        return needs

    def append_fps(self, fps: float) -> None:
        self.fps.append(fps)
        if len(self.fps) > self.FPS *2:
            self.fps.pop(0)

    def get_median_fps(self) -> float:
        return statistics.median(self.fps)
    
    def update_low_fps_warning(self):
        if not self.show_fps_warnings:
            return
        
        show_warning = self.get_median_fps() < self.FPS * 0.8
        if self.old_warning_state == show_warning:
            return
        self.old_warning_state = show_warning

        self.set_banner_revealed(show_warning)


    def set_show_fps_warnings(self, state: bool) -> None:
        self.show_fps_warnings = state
        if state:
            self.old_warning_state = False
        else:
            self.set_banner_revealed(False)

    def set_banner_revealed(self, state: bool) -> None:
        deck_stack_child: "DeckStackChild" = self.deck_controller.get_own_deck_stack_child()
        if deck_stack_child is None:
            return
        
        # deck_stack_child.low_fps_banner.set_revealed(show_warning)
        GLib.idle_add(deck_stack_child.low_fps_banner.set_revealed, state)


    def stop(self, timeout: float = 2.0) -> None:
        self._stop = True
        self._wake_event.set()  # wake an idle loop so it sees _stop promptly
        start = time.time()
        while self.running and time.time() - start < timeout:
            time.sleep(0.05)

    def add_task(self, method: callable, *args, **kwargs):
        self.tasks.append(MediaPlayerTask(
            deck_controller=self.deck_controller,
            page=self.deck_controller.active_page,
            _callable=method,
            args=args,
            kwargs=kwargs
        ))
        self._wake_event.set()

    def add_touchscreen_task(self, native_image: bytes, page=None, config_gen=None, controller_touchscreen=None, img_hash=None):
        self.touchscreen_task = MediaPlayerSetTouchscreenImageTask(
            deck_controller=self.deck_controller,
            page=page if page is not None else self.deck_controller.active_page,
            native_image=native_image,
            config_gen=config_gen,
            submit_seq=self.next_submit_seq(),
            controller_touchscreen=controller_touchscreen,
            img_hash=img_hash
        )
        self._wake_event.set()

    def add_image_task(self, key_index: int, native_image: bytes, page=None, config_gen=None, controller_key=None, img_hash=None):
        self.image_tasks[key_index] = MediaPlayerSetImageTask(
            deck_controller=self.deck_controller,
            page=page if page is not None else self.deck_controller.active_page,
            key_index=key_index,
            native_image=native_image,
            config_gen=config_gen,
            controller_key=controller_key,
            img_hash=img_hash,
            submit_seq=self.next_submit_seq()
        )
        self._wake_event.set()

    def perform_media_player_tasks(self):
        # Drain the queues BEFORE snapshotting page/gen: every drained task then
        # predates the snapshot, so a mismatch genuinely means stale. The reverse
        # order would drop a task just queued for the new page, unrun.
        task_batch = self.tasks.copy()
        for task in task_batch:
            try:
                self.tasks.remove(task)
            except ValueError:
                pass

        image_batch = []
        for key in list(self.image_tasks.keys()):
            try:
                image_batch.append(self.image_tasks.pop(key))
            except KeyError:
                continue

        # clear_media_player_tasks (GTK thread) may null this concurrently.
        touch_task = self.touchscreen_task
        self.touchscreen_task = None

        # Snapshot page + generation as one pair (the assignment in load_page
        # holds the same lock) so the whole batch is judged consistently.
        with self.deck_controller._page_gen_lock:
            active_page = self.deck_controller.active_page
            current_gen = self.deck_controller._page_load_generation

        def _is_current(task):
            # Drop paints for a page we've left or a superseded generation.
            # config_gen is the generation the paint rendered.
            if task.page is not active_page:
                return False
            if task.config_gen is not None and task.config_gen != current_gen:
                return False
            return True

        for task in task_batch:
            if task.page is active_page:
                task.run()

        # Bulk-batch write pacing (plan §9.2 experiment): a video-frame
        # repaint lands as a burst of back-to-back writes, and the transport
        # serializes reads and writes on one mutex -- the writer releasing
        # and immediately re-acquiring can repeatedly out-race the waiting
        # 20Hz HID read poll (the dial-starvation mechanism). A small forced
        # yield between BULK writes guarantees the reader a mutex slot,
        # which is what makes raising STREAMCONTROLLER_VIDEO_WRITE_HZ safe.
        # Interactive paints (small batches) stay unpaced: no added latency.
        # Yield every YIELD_STRIDE-th bulk write, not every write: the HID
        # read poll runs at 20Hz, so it needs ONE mutex window per ~50ms --
        # a slot every few writes (~3ms of holds) is ample, and per-write
        # yields cost ~12ms per video frame on high-entropy content where
        # dedup can't skip anything (measured: loop 19fps on a busy video).
        bulk = len(image_batch) >= self.BULK_BATCH_THRESHOLD
        writes_since_yield = 0
        for task in image_batch:
            if _is_current(task):
                if bulk and writes_since_yield >= self.YIELD_STRIDE and self._inter_write_yield > 0:
                    time.sleep(self._inter_write_yield)
                    writes_since_yield = 0
                task.run()
                writes_since_yield += 1

        if touch_task is not None and _is_current(touch_task):
            if bulk and writes_since_yield >= self.YIELD_STRIDE and self._inter_write_yield > 0:
                time.sleep(self._inter_write_yield)
            touch_task.run()

class DeckController:
    def __init__(self, deck_manager: "DeckManager", deck: StreamDeck.StreamDeck):
        self.deck_manager: DeckManager = deck_manager

        # Per-instance memo for stable deck properties (lru_cache on an instance
        # method would pin every self on the class and never evict).
        self._serial_number: str = None
        self._key_image_size: tuple[int] = None
        self._touchscreen_image_size: tuple[int] = None

        # Open the deck - why store it as self.deck? So that self.get_alive() returns True in get_deck_settings
        self.deck = deck
        # Resume-from-suspend handle reopen is the library's only mode now
        # (plan §9.1, decided 2026-07-04) -- always on.
        self.deck.open(True)

        rotation = self.get_deck_settings().get("rotation", 0)
        self.deck: BetterDeck = BetterDeck(deck, rotation)

        try:
            # Clear the deck. Must be the direct/synchronous body, not the
            # queue-routed clear(): media_player doesn't exist yet, and this
            # is a liveness probe -- its exception must abort construction
            # here rather than get lost in an async queue (plan §2.3).
            self._clear_direct()
        except Exception as e:
            log.error(f"Failed to clear deck, maybe it's already connected to another instance? Skipping... Error: {e}")
            # Release the handle and raise: the caller must not register a
            # half-built controller.
            try:
                self.deck.close()
            except Exception:
                pass
            raise
        
        self.hold_time: float = gl.settings_manager.get_app_settings().get("general", {}).get("hold-time", 0.5)
        
        self.own_deck_stack_child: "DeckStackChild" = None
        self.own_key_grid: "KeyGridChild" = None

        self.screen_saver = ScreenSaver(deck_controller=self)
        self.allow_interaction = True
        self.has_animated_keys = False

        self.key_spacing = (36, 36)

        if isinstance(self.deck, StreamDeckPlus) or (isinstance(self.deck, FakeDeck) and self.deck.key_layout() == [2, 4]):
            log.error("Deck recognized as StreamDeckPlus")
            self.key_spacing = (52, 36)

        # Per-deck saturation boost (PIL ImageEnhance.Color factor, UI range
        # 1.0-1.5). Cached here -- read once at boot and refreshed by
        # set_display_saturation() -- so every per-frame/per-build call site
        # (background video cache, key video cache, static media) can do a
        # cheap attribute read instead of a settings-dict lookup, and can
        # skip all enhancement work with a single float comparison when the
        # factor is the default 1.0 (no-op requirement).
        self.display_saturation: float = self._read_display_saturation()

        # identifier -> True while the main window is hidden/unmapped (mem
        # plan P5.4): a dirty marker, NOT a stashed PIL image -- the device
        # composite already happens every tick regardless of window
        # visibility, so retaining a full copy purely to replay to the UI
        # later just holds a big object alive for no benefit. On map,
        # KeyGrid.load_from_changes/ScreenBar.load_from_changes recomposite
        # the current frame for each dirty identifier via
        # ControllerInput.get_current_image() and push it through the same
        # set-image path a live update would.
        self.ui_image_changes_while_hidden: dict = {}

        # Set once by close() and never cleared (plan P1.3): gates re-entrant
        # producer paths (ScreenSaver.show/hide/on_key_change, load_page)
        # that would otherwise resurrect a controller mid-teardown, and makes
        # close() itself idempotent against a second call.
        self._closing: bool = False

        self.active_page: Page = None

        # Bumped on every load_page so overlapping/concurrent loads can tell
        # whether their queued paints are still current (see _page_is_current).
        self._page_load_generation: int = 0
        self._page_gen_lock = threading.Lock()
        # Serializes load_page's switch body so racing switches can't
        # interleave: an older switch could cancel the newer one's background
        # future or strand its queued work. RLock: a ChangePage handler may
        # nest a load_page.
        self._load_page_lock = threading.RLock()
        # Page recorded by load_page's screensaver guard, consumed by
        # ScreenSaver.hide() via take_pending_screensaver_page().
        self._screensaver_pending_page: "Page" = None
        # Serializes background loads on the pool; a superseded load must not
        # overwrite a newer page's background.
        self._background_load_lock = threading.Lock()
        self._bg_future = None

        self.inputs = {}
        for i in Input.All:
            self.inputs[i] = []
        self.init_inputs()

        self.background = Background(self)

        self.deck.set_key_callback(self.key_event_callback)
        self.deck.set_dial_callback(self.dial_event_callback)
        self.deck.set_touchscreen_callback(self.touchscreen_event_callback)

        # Unified write-error/resume-repaint state (plan §4 M2). Touched only
        # from the media thread (_on_write_result from the task classes'
        # run() and _exec_set_brightness; _run_pending_repaint from the run
        # loop) -- no lock needed, single writer. MUST be initialized before
        # the media thread starts: its very first iteration dereferences
        # _full_repaint_pending, and the loop has no exception guard -- an
        # AttributeError here kills the sole writer silently.
        self._had_write_failure: bool = False
        self._full_repaint_pending: bool = False
        self._last_full_repaint_ts: float = 0.0

        # Start media player thread
        self.media_player = MediaPlayerThread(deck_controller=self)
        self.media_player.start()
        # Register the sole expected device writer for the owner-assertion
        # tooling (STREAMCONTROLLER_ASSERT_DEVICE_OWNER; BetterDeck.py). A
        # no-op unless that env var is set -- harness/dev tooling only.
        self.deck.set_expected_writer(self.media_player)

        # Encoded key images keyed by (composite hash, rotation): repeated
        # frames (looping background video) skip conversion + JPEG encode.
        self.encode_memo = EncodedImageCache(max_bytes=32 * 1024 * 1024)

        # Bounded thread pool for action callbacks (tick/update/ready/event),
        # sized so every input can run its on_tick concurrently.
        total_inputs = sum(len(inputs) for inputs in self.inputs.values())
        self.action_executor = ThreadPoolExecutor(
            max_workers=max(8, total_inputs + 4),
            thread_name_prefix="action_cb",
        )

        # Persistent per-deck loader pool for load_all_inputs (plan P1.5):
        # sized so every input can load concurrently -- a fixed small pool
        # would serialize an XL's 32 inputs several-deep *on the media-player
        # thread* (load_all_inputs runs there via media_player.add_task), so
        # its deadline waits would block the sole writer. Replaced wholesale
        # (see load_all_inputs) on deadline expiry with stuck tasks, instead
        # of being torn down and rebuilt on every single page switch like the
        # throwaway executor this replaces.
        self.load_executor = ThreadPoolExecutor(
            max_workers=max(8, total_inputs),
            thread_name_prefix=f"load_{self.serial_number()}",
        )

        self.keep_actions_ticking = True
        self.TICK_DELAY = 1
        # Lets close() interrupt tick_actions' sleep immediately instead of
        # waiting out up to a full TICK_DELAY before the loop notices
        # keep_actions_ticking went False (plan P1.3 step 4 needs a prompt,
        # bounded join -- see tick_actions).
        self._tick_stop_event = threading.Event()
        self.tick_thread = Thread(target=self.tick_actions, name="tick_actions")
        self.tick_thread.start()

        self.page_auto_loaded: bool = False
        self.last_manual_loaded_page_path: str = None

        deck_settings = self.get_deck_settings()

        self.brightness = 75
        brightness = deck_settings.get("brightness", {}).get("value", self.brightness)
        self.set_brightness(brightness)

        # self.rotation = 270
        # rotation = deck_settings.get("rotation", {}).get("value", self.rotation)
        # self.set_rotation(rotation)


        # If screen is locked start the screensaver - this happens when the deck gets reconnected during the screensaver
        if gl.screen_locked and gl.settings_manager.get_app_settings().get("system", {}).get("lock-on-lock-screen", True):
            self.allow_interaction = False
            self.screen_saver.show()
        else:
            self.load_default_page()

    def init_inputs(self):
        for i in Input.All:
            self.inputs[i] = []
            input_class = getattr(sys.modules[__name__], i.controller_class_name)

            for k in input_class.Available_Identifiers(self.deck):
                controller_input = input_class(self, Input.FromTypeIdentifier(i.input_type, k))
                # Stamp with the current generation so paints from freshly built
                # inputs (e.g. the screensaver's) aren't dropped as stale.
                controller_input.config_gen = self._page_load_generation
                self.inputs[i].append(controller_input)

    def get_inputs(self, identifier: InputIdentifier) -> list["ControllerInput"]:
        input_type = type(identifier)
        if input_type not in self.inputs:
            raise ValueError(f"Unknown input type: {input_type}")
        return self.inputs[input_type]

    def get_input(self, identifier: InputIdentifier) -> "ControllerInput":
        for i in self.get_inputs(identifier):
            if i.identifier == identifier:
                return i
        return None

    def serial_number(self) -> str:
        if self._serial_number is None:
            self._serial_number = self.deck.get_serial_number()
        return self._serial_number
    
    def is_visual(self) -> bool:
        return self.deck.is_visual()

    def update_input(self, identifier: InputIdentifier):
        i = self.get_input(identifier)
        if not i:
            return
        i.update()

    @log.catch
    def update_all_inputs(self, gen=None):
        if not self._page_is_current(gen):
            return
        start = time.time()
        if not self.get_alive(): return
        if self.background.video is not None:
            log.debug("Skipping update_all_inputs (device keys) because there is a background video -- the per-frame video loop already paints the keys on the deck; a full key update() here would double-write and disturb that video. Dials + the in-app previews are still synced below.")

            for i in self.inputs[Input.Dial]:
                i.update()
            # UI-only mirror. The in-app KeyGrid is NOT the video device, so
            # pushing the current composite to it can't disturb the deck video
            # (this is a widget update, never a device write). Keys whose
            # per-frame render the video loop skips (opaque keys, alpha ==
            # 255) are otherwise never repainted in the app after a
            # transition, and the previews diverge from the deck. Bypasses
            # update()'s device-oriented dedup on purpose: the device and UI
            # can be out of sync (device painted, UI missed), and only
            # re-pushing the UI reconciles them.
            for i in self.inputs[Input.Key]:
                try:
                    i.set_ui_key_image(i.get_current_image())
                except Exception:
                    log.exception(f"In-app preview sync failed for {i.identifier}")
            return
        for t in self.inputs:
            for i in self.inputs[t]:
                i.update()
        log.debug(f"Updating all inputs took {time.time() - start} seconds")

    def _update_all_inputs_awaiting_background(self, bg_future, gen=None):
        # Media thread: skip promptly when superseded, then await the background
        # decode (bounded) so keys composite over the new background.
        if not self._page_is_current(gen):
            return
        if bg_future is not None:
            try:
                bg_future.result(timeout=10)
            except Exception:
                log.warning("Background not ready before update_all_inputs; painting anyway")
        self.update_all_inputs(gen=gen)

    def _reset_dedup_hashes(self) -> None:
        """Nulls `_last_img_hash`/`_last_enqueued_hash` on every current key
        and the touchscreen (if present) -- shared by Clear (dedup-coherence
        fix) and full-repaint scheduling (resume-repaint fix), plan §3/§4
        M2. Without this, a repaint of visually-identical content would
        still match the stale cached hash and get wrongly skipped."""
        for key in self.inputs.get(Input.Key, []):
            key._last_img_hash = None
            key._last_enqueued_hash = None
        for touchscreen in self.inputs.get(Input.Touchscreen, []):
            touchscreen._last_img_hash = None
            touchscreen._last_enqueued_hash = None

    def _schedule_full_repaint(self) -> None:
        """Arms a pending full repaint -- fired by the media loop via
        _run_pending_repaint() when the 2s rate window allows. Deferred (not
        dropped) on rate-limit, and re-armed by every write FAILURE: a
        repaint attempted while the library's read thread is still reopening
        the handle after a suspend fails wholesale, and on a fully static
        page no later write would ever re-trigger it -- the pending flag
        makes the loop retry every 2s until a repaint's writes stick
        (plan §4 M2)."""
        self._full_repaint_pending = True

    def _run_pending_repaint(self) -> bool:
        """Media-loop hook: fires an armed repaint if >=2s since the last
        one. Nulls all dedup hashes then update_all_inputs() -- safe on the
        media thread: it only enqueues via add_image_task/
        add_touchscreen_task, the same calls on_media_player_tick already
        makes from this thread. Returns whether a repaint fired."""
        if not self._full_repaint_pending:
            return False
        now = time.time()
        if now - self._last_full_repaint_ts < 2.0:
            return False
        self._full_repaint_pending = False
        self._last_full_repaint_ts = now
        self._reset_dedup_hashes()
        self.update_all_inputs()
        return True

    def _on_write_result(self, success: bool) -> None:
        """Unified write-error handler (plan §4 M2, §9.1): called by both
        image/touchscreen task run() paths and _exec_set_brightness after
        every device write attempt. The graduated error policy is just
        attempt-and-swallow (removal comes solely from USB disconnect
        events) -- the remaining job is recovery: every failure arms the
        pending repaint (content written into that failure window may be
        lost on the device), and the loop's 2s cadence retries until a
        repaint lands cleanly. Media-thread-only, no lock needed (see
        __init__)."""
        if success:
            if self._had_write_failure:
                self._had_write_failure = False
        else:
            self._had_write_failure = True
            self._full_repaint_pending = True

    def event_callback(self, ident: InputIdentifier, *args, **kwargs):
        if not self.allow_interaction:
            return
        i = self.get_input(ident)
        if not i:
            return
        i.event_callback(*args, **kwargs)

    def key_event_callback(self, deck, key, *args, **kwargs):
        coords = ControllerKey.Index_To_Coords(deck, key)
        if self.deck.rotation % 180 != 0:
            coords = (coords[1], coords[0])
        ident = Input.Key(f"{coords[0]}x{coords[1]}")
        self.event_callback(ident,*args, **kwargs)

    def dial_event_callback(self, deck, dial, *args, **kwargs):
        ident = Input.Dial(str(dial))
        self.event_callback(ident, *args, **kwargs)

    def touchscreen_event_callback(self, deck, *args, **kwargs):
        ident = Input.Touchscreen("sd-plus")
        self.event_callback(ident, *args, **kwargs)


    ### Helper methods
    def generate_alpha_key(self) -> Image.Image:
        return Image.new("RGBA", self.get_key_image_size(), (0, 0, 0, 0))
    
    def get_key_image_size(self) -> tuple[int]:
        if self._key_image_size is not None:
            return self._key_image_size
        if not self.get_alive(): return
        size = self.deck.key_image_format()["size"]
        if size is None:
            size = (72, 72)
        else:
            size = max(size[0], 72), max(size[1], 72)
        self._key_image_size = size
        return size

    def get_touchscreen_image_size(self) -> tuple[int]:
        if self._touchscreen_image_size is not None:
            return self._touchscreen_image_size
        if not self.get_alive(): return
        size = self.deck.touchscreen_image_format()["size"]
        if size is None:
            size = (800, 100)
        else:
            size = max(size[0], 800), max(size[1], 100)
        self._touchscreen_image_size = size
        return size

    # ------------ #
    # Page Loading #
    # ------------ #

    def load_default_page(self):
        if not self.get_alive(): return

        api_page_path = None
        if self.serial_number() in gl.api_page_requests:
            # Pop, don't just read (design doc bug 13): a `--change-page`
            # request is one-shot -- left in place, it silently re-applied
            # itself on every future load_default_page() call for this
            # serial (every unplug/replug, every "no page found" fallback).
            api_page_path = gl.api_page_requests.pop(self.serial_number())
            api_page_path = gl.page_manager.find_matching_page_path(api_page_path)

        if api_page_path is None:
            default_page_path = gl.page_manager.get_default_page(self.deck.get_serial_number())
        else:
            default_page_path = api_page_path

        if default_page_path is not None:
            if not os.path.isfile(default_page_path):
                default_page_path = None
            
        if default_page_path is None:
            # Use the first page
            pages = gl.page_manager.get_pages()
            if len(pages) == 0:
                return
            default_page_path = gl.page_manager.get_pages()[0]

        if default_page_path is None:
            return
        
        page = gl.page_manager.get_page(default_page_path, self)
        self.load_page(page)

        # Handle state change requests
        if self.serial_number() in gl.api_state_requests:
            state_request = gl.api_state_requests[self.serial_number()]
            page_name = state_request["page_name"]
            coords = state_request["coords"]
            state = state_request["state"]
            
            # Get the page path for the specified page
            requested_page_path = gl.page_manager.find_matching_page_path(page_name)
            
            if requested_page_path is None:
                # Page not found - log available pages
                available_pages = [os.path.splitext(os.path.basename(p))[0] for p in gl.page_manager.get_pages()]
                log.error(f"State change failed: Page '{page_name}' not found for device {self.serial_number()}. Available pages: {', '.join(available_pages)}")
            else:
                # Load the requested page if it's different from the current one
                if os.path.abspath(requested_page_path) != os.path.abspath(self.active_page.json_path):
                    requested_page = gl.page_manager.get_page(requested_page_path, self)
                    self.load_page(requested_page)
                
                # Parse coordinates and change state with enhanced error handling
                try:
                    x, y = map(int, coords.split(','))
                    
                    # Validate coordinates are within bounds
                    rows, cols = self.deck.key_layout()
                    if x < 0 or x >= cols or y < 0 or y >= rows:
                        log.error(f"State change failed: Coordinates ({x},{y}) out of bounds for device {self.serial_number()}. Valid range: x=0-{cols-1}, y=0-{rows-1}")
                    else:
                        identifier = Input.Key(f"{x}x{y}")
                        c_input = self.get_input(identifier)
                        
                        if c_input is None:
                            log.error(f"State change failed: No input found at coordinates ({x},{y}) on device {self.serial_number()}")
                        elif state < 0 or state >= len(c_input.states):
                            max_state = len(c_input.states) - 1
                            if max_state == 0:
                                log.error(f"State change failed: Position ({x},{y}) on device {self.serial_number()} only has 1 state (state 0). Requested state {state} does not exist")
                            else:
                                log.error(f"State change failed: Position ({x},{y}) on device {self.serial_number()} has states 0-{max_state}. Requested state {state} does not exist")
                        else:
                            # Successfully change state
                            c_input.set_state(state)
                            log.info(f"Successfully changed state of position ({x},{y}) to state {state} on device {self.serial_number()}")
                            
                except (ValueError, AttributeError) as e:
                    log.error(f"State change failed: Invalid coordinate format '{coords}' for device {self.serial_number()}. Expected format: 'x,y' (e.g., '0,0'). Exception: {e}")
                except Exception as e:
                    log.error(f"State change failed: Unexpected error for device {self.serial_number()}: {e}")
            
            # Remove the request after processing
            del gl.api_state_requests[self.serial_number()]

    @log.catch
    def load_background(self, page: Page, update: bool = True, gen=None):
        deck_settings = self.get_deck_settings()

        deck_background_settings = deck_settings.get("background", {})
        page_background_settings = page.dict.get("settings", {}).get("background", {})

        log.info(f"Loading background in thread: {threading.get_ident()}")
        if deck_background_settings.get("enable", False) and not page_background_settings.get("overwrite", False):
            config = deck_background_settings
        elif page_background_settings.get("overwrite", False) and page_background_settings.get("show", False):
            config = page_background_settings
        else:
            config = {}

        # Serialize concurrent loads and drop superseded ones so an older switch
        # can't overwrite the newer page's background.
        with self._background_load_lock:
            if not self._page_is_current(gen):
                return
            # Set the flag first (without repainting) so set_from_path renders
            # tiles and the touchscreen slice with the correct geometry.
            self.background.set_extend_to_touchscreen(
                config.get("extend-to-touchscreen", False), update=False
            )
            self.background.set_from_path(
                path=config.get("media-path"),
                update=update,
                loop=config.get("loop", False),
                fps=config.get("fps", 30),
            )

    @log.catch
    def load_brightness(self, page: Page):
        if not self.get_alive():
            return

        deck_brightness = self.get_deck_settings().get("brightness", {})
        page_brightness = page.dict.get("settings",{}).get("brightness", {})

        if page_brightness.get("overwrite", False):
            value = page_brightness.get("value", 75)
        else:
            value = deck_brightness.get("value", 75)

        log.info(value)

        self.set_brightness(value)

    @log.catch
    def load_screensaver(self, page: Page):
        deck_settings = self.get_deck_settings()
        deck_screensaver_settings = deck_settings.get("screensaver", {})
        page_screensaver_settings = page.dict.get("settings", {}).get("screensaver", {})

        log.info(f"Loading screensaver in thread: {threading.get_ident()}")
        if deck_screensaver_settings.get("enable", False) and not page_screensaver_settings.get("overwrite", False):
            config = deck_screensaver_settings
        elif page_screensaver_settings.get("overwrite", False) and page_screensaver_settings.get("enable", False):
            config = page_screensaver_settings
        else:
            config = {}

        self.screen_saver.set_media_path(config.get("media-path"))
        self.screen_saver.set_enable(config.get("enable", False))
        self.screen_saver.set_time(config.get("time-delay", 5))
        self.screen_saver.set_loop(config.get("loop", False))
        self.screen_saver.set_fps(config.get("fps", 30))
        self.screen_saver.set_brightness(config.get("brightness", 30))

    def _page_is_current(self, gen) -> bool:
        # gen is None for callers outside the page-load path (always run). For a
        # load_page-issued paint, it's stale once a newer load_page bumped the
        # generation.
        return gen is None or gen == self._page_load_generation

    # Deadline for load_all_inputs: input loads run plugin callbacks that can
    # block forever, and the media-player thread must never be wedged by one.
    LOAD_INPUTS_TIMEOUT = 10.0

    @log.catch
    def load_all_inputs(self, page: Page, update: bool = True, gen=None):
        if not self._page_is_current(gen):
            return
        start = time.time()
        # Persistent per-deck pool (plan P1.5), not a throwaway ThreadPoolExecutor()
        # per call: this runs on the media-player thread (via
        # media_player.add_task), so constructing/tearing down a pool here on
        # every single page switch was pure churn on the sole writer's path.
        executor = self.load_executor
        pending = []
        for t in self.inputs:
            for controller_input in self.inputs[t]:
                try:
                    future = executor.submit(self._load_input_if_current, controller_input, page, update, gen)
                except RuntimeError:
                    # Pool already shut down (deck closing concurrently).
                    continue
                pending.append((controller_input, future))
        deadline = time.monotonic() + self.LOAD_INPUTS_TIMEOUT
        stuck = []
        for controller_input, future in pending:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeoutError:
                stuck.append(str(controller_input.identifier))
        if stuck:
            log.warning(
                f"Loading inputs [{', '.join(stuck)}] did not finish within "
                f"{self.LOAD_INPUTS_TIMEOUT}s; continuing without them (a plugin "
                f"callback is likely blocked). Replacing this deck's loader pool "
                f"so the stuck task(s) leak their pool's thread(s) once, instead "
                f"of wedging every future page load behind them (plan P1.5).")
            old_executor = executor
            total_inputs = sum(len(inputs) for inputs in self.inputs.values())
            self.load_executor = ThreadPoolExecutor(
                max_workers=max(8, total_inputs),
                thread_name_prefix=f"load_{self.serial_number()}",
            )
            # No wait: the stuck task(s) may never return; cancel what we can
            # and abandon the rest to this one pool's leaked thread(s).
            old_executor.shutdown(wait=False, cancel_futures=True)
        log.info(f"Loading all inputs took {time.time() - start} seconds")

    def _load_input_if_current(self, controller_input: "ControllerInput", page: Page, update: bool = True, gen=None):
        # A slower in-flight page load must not paint the previous page's images
        # onto the current page's keys; skip if a newer load superseded this one.
        # config_gen is NOT stamped here: load_page stamps all inputs under
        # _page_gen_lock, and a second stamp on this pool could interleave with
        # a newer load's stamp and regress an input to an older generation.
        if not self._page_is_current(gen):
            return
        self.load_input(controller_input, page, update)

    def take_pending_screensaver_page(self) -> "Page":
        """Pops the page recorded by load_page's screensaver guard; None when
        no page change arrived while the screensaver was showing."""
        pending = self._screensaver_pending_page
        self._screensaver_pending_page = None
        return pending

    def load_input_from_identifier(self, identifier: str, page: Page, update: bool = True):
        controller_input = self.get_input(identifier)
        if controller_input is not None:
            self.load_input(controller_input, page, update)

    def load_input(self, controller_input: "ControllerInput", page: Page, update: bool = True):
        input_dict = controller_input.identifier.get_config(page)
        controller_input.load_from_input_dict(input_dict, update)

    def update_ui_on_page_change(self):
        # Update ui
        if recursive_hasattr(gl, "app.main_win.sidebar"):
            try:
                # gl.app.main_win.header_bar.page_selector.update_selected()
                settings_page = gl.app.main_win.leftArea.deck_stack.get_visible_child().page_settings.settings_page
                settings_group = settings_page.settings_group
                background_group = settings_page.background_group

                # Update ui
                settings_group.brightness.load_defaults_from_page()
                settings_group.screensaver.load_defaults_from_page()
                background_group.media_row.load_defaults_from_page()

                gl.app.main_win.sidebar.update()
            except AttributeError as e:
                log.error(f"{e} -> This is okay if you just activated your first deck.")

    def close_image_ressources(self):
        """Releases every input's media (key/dial images+videos) plus the
        background image/video. Called from close() step 7 (plan P1.3).

        Was dead code with zero callers until this fix, and broken besides
        (design doc bug 19): ControllerInput had no close_resources() at all
        (AttributeError below) and BackgroundImage had no close() (same
        below) -- both are added alongside this comment."""
        for t in self.inputs:
            for i in self.inputs[t]:
                i.close_resources()

        if self.background.video is not None:
            self.background.video.close()
            self.background.video = None
        if self.background.image is not None:
            self.background.image.close()
            self.background.image = None

    @log.catch
    def load_page(self, page: Page, load_brightness: bool = True, load_screensaver: bool = True, load_background: bool = True, load_inputs: bool = True, allow_reload: bool = True):
        if not self.get_alive(): return
        if self._closing:
            # A straggling caller (screensaver follow-up, plugin hook, DBus
            # request) raced close() -- don't resurrect the deck mid-
            # teardown (plan P1.3).
            return

        start = time.time()

        # Serialize the whole switch body (see _load_page_lock). The plugin-facing
        # tail (ChangePage signal, DBus) stays outside so a slow handler can't
        # block other callers on this lock.
        with self._load_page_lock:
            if not allow_reload:
                if self.active_page is page:
                    return

            # A page change requested while the screensaver owns the deck must
            # NOT load or paint the new page now: that would replace the
            # screensaver on the device and leak the page's icons onto the deck
            # AND into the app previews. Record it as PENDING and return; hide()
            # loads it when the screensaver is dismissed.
            #
            # Deliberately DON'T touch active_page here: the media player gates
            # the screensaver's own background-video animation on
            # `background.video.page is active_page` (see MediaPlayerThread.run
            # ~360), so changing active_page mid-screensaver freezes the
            # screensaver video (it resumes only when active_page is switched
            # back to the screensaver's page). Leaving active_page on the
            # screensaver's page keeps that gate open and the video playing.
            if self.screen_saver.showing:
                if page is not None:
                    self._screensaver_pending_page = page
                # A clear request (page=None) is dropped, not deferred: the
                # pending slot has no "clear" representation (None means "no
                # pending"), and letting it through would clear the deck out
                # from under the showing screensaver.
                return

            # Cheap monotonic counter read by mem_telemetry's idle/trim gate
            # (docs/memory-footprint-plan.md Phase 0) -- bump once we know
            # this call is an actual switch, not the no-op reload above.
            page_switches.bump()

            old_path = self.active_page.json_path if self.active_page is not None else None

            # Set active_page and bump the generation atomically: a concurrent switch
            # must never leave active_page on one page while the newest generation
            # belongs to another (stale paints would then match both checks and bleed).
            with self._page_gen_lock:
                self.active_page = page
                self._page_load_generation += 1
                gen = self._page_load_generation

                # Stamp every input with the new generation SYNCHRONOUSLY,
                # under the same lock as the bump. Paints are triggered from
                # threads outside the load pool (the action pool via on_ready
                # -> update, the tick loop, update_all_inputs) and read
                # controller_input.config_gen directly (update(), ~3312); any
                # window between the bump and the stamp lets such a paint
                # carry the previous generation and be dropped at the present
                # boundary as stale -- blanking the newly loaded page's own
                # keys. Stale cross-page content is still caught by the
                # separate page-identity check. This must stay the ONLY stamp
                # on the load path (see _load_input_if_current).
                for input_type in self.inputs:
                    for controller_input in self.inputs[input_type]:
                        controller_input.config_gen = gen

            if page is None:
                # Clear deck
                self.clear()
                return

            log.info(f"Loading page {page.get_name()} on deck {self.deck.get_serial_number()}")

            # Stop queued tasks (skipped if a newer switch already superseded this one)
            self.clear_media_player_tasks(gen)

            # Update ui
            GLib.idle_add(self.update_ui_on_page_change) #TODO: Use new signal manager instead

            bg_future = None
            if load_background:
                # Decode the background off the media thread so it overlaps input
                # loading; the update task below awaits it before keys composite.
                from GtkHelper.GtkHelper import run_in_background
                if self._bg_future is not None:
                    self._bg_future.cancel()
                bg_future = run_in_background(self.load_background, page, update=False, gen=gen)
                self._bg_future = bg_future
            if load_brightness:
                self.load_brightness(page)
            if load_screensaver:
                self.load_screensaver(page)
            if load_inputs:
                self.media_player.add_task(self.load_all_inputs, page, update=False, gen=gen)
            else:
                # Not reloading content, but the generation bumped: advance each
                # input's config_gen so its unchanged content isn't dropped as stale.
                for input_type in self.inputs:
                    for controller_input in self.inputs[input_type]:
                        controller_input.config_gen = gen

            # Load page onto deck, awaiting the background decode first.
            self.media_player.add_task(self._update_all_inputs_awaiting_background, bg_future, gen)

        # Must stay outside _load_page_lock: initialize_actions can block on a
        # run_on_main marshal, deadlocking against a main-thread load_page.
        # `page`, not active_page: a newer switch may already own active_page;
        # initializing a superseded page is harmless (on_ready_called de-dupes).
        page.initialize_actions()

        # Notify plugin actions
        gl.signal_manager.trigger_signal(Signals.ChangePage, self, old_path, self.active_page.json_path)

        # Notify DBus API of the page change
        notify_active_page_changed(self.serial_number(), page.get_name())

        log.info(f"Loaded page {page.get_name()} on deck {self.deck.get_serial_number()}")
        gc.collect()

    def reload_page(self):
        self.load_page(
            page=self.active_page,
            allow_reload=True
        )

    def set_brightness(self, value):
        value = min(100, max(0, value))
        if not self.get_alive(): return
        # Routed through the media thread's control queue (plan §2.1) so the
        # device write happens on the sole writer, not the calling (GTK/
        # Timer/switch) thread. self.brightness is the last-commanded value,
        # not a hardware-confirmed one -- same caveat as before this change
        # (the old direct write had no error handling around it either).
        self.brightness = value
        self.media_player.submit_control(SetBrightnessMsg(value))

    def set_rotation(self, value):
        self.deck.set_rotation(value)

        self.own_key_grid = None


        if recursive_hasattr(gl, "app.main_win"):
            # self.get_own_key_grid().regenerate_buttons()

            # Re-generate key grid
            deck_stack_child = self.get_own_deck_stack_child()
            deck_config = deck_stack_child.page_settings.deck_config
            key_grid = deck_config.grid
            deck_config.remove(key_grid)

            deck_config.grid = KeyGrid(self, key_grid.page_settings_page)
            deck_config.prepend(deck_config.grid)

        if not self.get_alive(): return
        self.load_page(self.active_page)
        # self.update_all_inputs()


    def tick_actions(self) -> None:
        # Event-based wait (mirrors MediaPlayerThread's _wake_event): close()
        # sets _tick_stop_event alongside keep_actions_ticking=False so its
        # bounded join actually returns promptly instead of waiting out
        # whatever fraction of TICK_DELAY this loop happened to be sleeping.
        self._tick_stop_event.wait(self.TICK_DELAY)
        while self.keep_actions_ticking:
            start = time.time()
            self.mark_page_ready_to_clear(False)
            if not self.screen_saver.showing and True:
                for t in self.inputs:
                    for i in self.inputs[t]:
                        i.get_active_state().own_actions_tick_threaded()
            else:
                for t in self.inputs:
                    for i in self.inputs[t]:
                        i.update()

            self.mark_page_ready_to_clear(True)

            end = time.time()
            wait = max(0.1, self.TICK_DELAY - (end - start))
            self._tick_stop_event.wait(wait)

    # -------------- #
    # Helper methods #
    # -------------- #

    def coords_to_index(self, coords: tuple) -> int:
        return ControllerKey.Coords_To_Index(self.deck, coords)
    
    def index_to_coords(self, index: int) -> tuple:
        return ControllerKey.Index_To_Coords(self.deck, index)
    
    def get_key_by_coords(self, coords: tuple) -> "ControllerKey":
        index = self.coords_to_index(coords)
        return self.get_key_by_index(index)
    
    def get_key_by_index(self, index: int) -> "ControllerKey":
        keys = self.inputs.get(Input.Key, [])
        if index < 0 or index >= len(keys):
            return
        return keys[index]

    def mark_page_ready_to_clear(self, ready_to_clear: bool):
        if self.active_page is not None:
            self.active_page.ready_to_clear = ready_to_clear
    
    def get_deck_settings(self):
        if not self.get_alive():
            return {}
        return gl.settings_manager.get_deck_settings(self.deck.get_serial_number())

    # --- display saturation ----------------------------------------------
    # DEFAULT_DISPLAY_SATURATION (1.0) is a strict no-op: every application
    # site below compares against it before doing any ImageEnhance work or
    # touching a cache filename, so the on-disk/behavioral footprint at the
    # default is byte-identical to a build without this feature.
    DEFAULT_DISPLAY_SATURATION = 1.0

    def _read_display_saturation(self) -> float:
        try:
            value = float(
                self.get_deck_settings().get("display", {}).get(
                    "saturation", self.DEFAULT_DISPLAY_SATURATION
                )
            )
        except (TypeError, ValueError):
            value = self.DEFAULT_DISPLAY_SATURATION
        return value

    def get_display_saturation(self) -> float:
        return self.display_saturation

    def set_display_saturation(self, value: float) -> None:
        """Persist the saturation factor to deck settings, refresh the cached
        value, and reload the active page so static media (background image,
        key/dial icons) re-enhances immediately. A currently-playing
        background/key *video* keeps showing its already-baked cache until
        the reload constructs a fresh cache object under the new factor's
        cache filename (see BackgroundVideoCache/KeyVideoCache) -- video
        content upgrades to the new factor on its first playthrough after
        that, not instantaneously."""
        value = round(float(value), 2)
        deck_settings = self.get_deck_settings()
        deck_settings.setdefault("display", {})["saturation"] = value
        gl.settings_manager.save_deck_settings(self.deck.get_serial_number(), deck_settings)

        self.display_saturation = value

        if self.active_page is not None:
            self.load_page(self.active_page, allow_reload=True)
    
    def get_own_deck_stack_child(self) -> "DeckStackChild":
        # Why not just lru_cache this? Because this would also cache the None that gets returned while the ui is still loading
        if self.own_deck_stack_child is not None:
            return self.own_deck_stack_child
        
        if not recursive_hasattr(gl, "app.main_win.leftArea.deck_stack"): return
        serial_number = self.deck.get_serial_number()
        deck_stack = gl.app.main_win.leftArea.deck_stack
        deck_stack_child = deck_stack.get_child_by_name(serial_number)
        if deck_stack_child == None:
            return
        
        self.own_deck_stack_child = deck_stack_child
        return deck_stack_child
    
    def _write_blank_frames(self) -> None:
        """Writes blank key images (+ touchscreen) directly to the device.
        Shared body for _clear_direct() and the media thread's Clear/
        ClearAndClose control-message handling -- this is the "existing
        clear body logic" the control messages reuse (plan §2.1)."""
        if not self.is_visual():
            return
        alpha_image = self.generate_alpha_key()
        native_image = PILHelper.to_native_key_format(self.deck, alpha_image.convert("RGB"))
        for i in range(self.deck.key_count()):
            self.deck.set_key_image(i, native_image)

        if self.deck.is_touch():
            touchscreen_size = self.get_touchscreen_image_size()
            empty = Image.new("RGB", touchscreen_size, (0, 0, 0))
            native_image = PILHelper.to_native_touchscreen_format(self.deck, empty)

            self.deck.set_touchscreen_image(native_image, x_pos=0, y_pos=0, width=touchscreen_size[0], height=touchscreen_size[1])

    def _clear_direct(self) -> None:
        """Synchronous, direct clear -- ONLY for the bootstrap liveness probe
        in __init__: at that point media_player doesn't exist yet, and the
        probe's exception must abort construction synchronously rather than
        get lost in an async queue. Not owner-assertion safe by design: the
        assertion is registered after the media thread starts, strictly
        after this runs (plan §2.3). Do not call this from anywhere else."""
        self._write_blank_frames()

    def clear(self) -> None:
        """Gen-agnostic async clear: submits a seq-stamped ClearMsg to the
        media thread's control queue instead of writing directly (plan
        §2.1). The seq stamp orders this against in-flight/future frame
        submissions: tasks already queued with a lower submit_seq are wiped,
        tasks submitted after this call (even same tick) survive and paint
        afterward -- preserving the caller's clear-then-paint order as
        blank-then-content on the device."""
        seq = self.media_player.next_submit_seq()
        self.media_player.submit_control(ClearMsg(seq=seq))

    def get_own_key_grid(self) -> KeyGrid:
        # Why not just lru_cache this? Because this would also cache the None that gets returned while the ui is still loading
        if self.own_key_grid is not None:
            return self.own_key_grid
        
        deck_stack_child = self.get_own_deck_stack_child()
        if deck_stack_child == None:
            return
        
        self.own_key_grid = deck_stack_child.page_settings.deck_config.grid
        return deck_stack_child.page_settings.deck_config.grid
    
    def clear_media_player_tasks(self, gen=None):
        # Skip the clear when a newer page load has superseded this one, so a late
        # clear can't wipe the newer load's freshly-queued tasks (stranding). The
        # lock spans check AND clear so a generation bump can't land mid-clear.
        with self._page_gen_lock:
            if gen is not None and gen != self._page_load_generation:
                return
            self.media_player.tasks.clear()
            self.media_player.image_tasks.clear()
            self.media_player.touchscreen_task = None

    def close(self, remove_media: bool, app_quit: bool = False) -> None:
        """One deterministic teardown sweep (docs/memory-footprint-impl-plan.md
        P1.3; design doc §3.3 item 1 / bug appendix A.1-A.3). Every unplug/
        replug (DeckManager.remove_controller), fake-deck removal, and
        app-quit path funnels through here -- delete() is a thin alias kept
        for existing callers.

        Idempotent: a second call (from any thread) is a no-op, guarded by
        `_closing`.

        Threading contract: when `app_quit` is False this is expected to run
        off the main thread -- a wedged plugin teardown hook (step 6) must
        not freeze the UI. DeckManager.remove_controller dispatches it on a
        dedicated daemon thread (not the shared GtkHelper pool, which quit's
        shutdown_background_pool() would cancel mid-close). `app_quit=True`
        is the one case that's expected to run synchronously on main: it
        skips step 6 entirely (no plugin hooks to block on), and on_quit's
        6s force-quit timer is the backstop for everything else here.

        `remove_media` gates step 7's resource sweep (background/input media
        + caches); the rest of the sequence (device/thread/registration
        teardown) always runs.
        """
        if self._closing:
            return
        self._closing = True

        if not app_quit and threading.current_thread() is threading.main_thread():
            # Soft guard, not a hard failure: the test harness's teardown()
            # helper calls delete()/close() from what is, in that process,
            # the "main thread" (no GTK main loop actually runs there), and
            # that must keep working. In the real app this path should never
            # be hit -- DeckManager.remove_controller always dispatches onto
            # a dedicated thread -- so a warning here is a real signal.
            log.warning(
                f"DeckController.close() for "
                f"{getattr(self, '_serial_number', None) or '<unknown>'} called "
                "from the main thread with app_quit=False -- a wedged plugin "
                "teardown hook (step 6) would freeze the UI. Callers should "
                "dispatch this on its own thread."
            )

        # Step 2: defuse the screensaver directly. NEVER set_enable(False)/
        # hide() here: hide() takes _load_page_lock and runs a full
        # load_page() (ScreenSaver.py), which would resurrect the deck
        # mid-close -- deterministically, whenever the screensaver happens
        # to be showing at unplug.
        screen_saver = getattr(self, "screen_saver", None)
        if screen_saver is not None:
            if screen_saver.timer:
                screen_saver.timer.cancel()
            screen_saver.enable = False
            screen_saver.showing = False

        # Step 3: stop the library's read thread before anything else so a
        # stray input callback can't fire into the teardown below, and so
        # the resume-from-suspend loop can't reopen the device behind us.
        if getattr(self, "deck", None) is not None:
            try:
                self.deck.stop_read_thread()
            except Exception:
                log.opt(exception=True).warning("Failed to stop the deck's read thread during close()")

        # Step 4: stop AND join the tick thread before any action teardown:
        # its body iterates every input's active state unguarded, and a
        # concurrent clear_action_objects() mid-iteration could kill the
        # loop or recomposite an input being swept out from under it.
        self.keep_actions_ticking = False
        tick_stop_event = getattr(self, "_tick_stop_event", None)
        if tick_stop_event is not None:
            tick_stop_event.set()
        tick_thread = getattr(self, "tick_thread", None)
        if tick_thread is not None and tick_thread is not threading.current_thread():
            tick_thread.join(2.0)

        # Step 5: terminal clear+close through the sole writer, bounded.
        # If close_all() already drove this controller through
        # ClearAndCloseMsg (the app-quit path), the loop already exited and
        # this is a fast no-op: submit_control rejects post-stop (bug 12)
        # and stop()'s poll on an already-dead thread returns immediately.
        media_player = getattr(self, "media_player", None)
        if media_player is not None:
            try:
                media_player.submit_control(ClearAndCloseMsg())
            except Exception:
                log.opt(exception=True).warning("Failed to submit ClearAndClose during close()")
            media_player.stop(timeout=2.0)

        # Step 6: action teardown -- skipped at app-quit. on_quit runs
        # synchronously on main against a 6s force-quit deadline; hooks that
        # run_on_main here would block it. Device hygiene (steps 1-5, 7-9)
        # is what matters at quit, not plugin notification.
        if not app_quit:
            self._teardown_actions()

        # Step 7: resource sweep. The writer is stopped, so nothing races a
        # paint touching these caches/objects concurrently.
        if remove_media:
            try:
                self.close_image_ressources()
            except Exception:
                log.opt(exception=True).warning("Failed to close image resources during close()")
            encode_memo = getattr(self, "encode_memo", None)
            if encode_memo is not None:
                encode_memo.clear()
            if media_player is not None:
                media_player.image_tasks.clear()
                media_player.tasks.clear()
                media_player.touchscreen_task = None
                media_player.control_q.clear()
        # Fallback close: normally the writer already closed the device in
        # step 5's ClearAndCloseMsg -- this only matters if that writer was
        # wedged and never got to process it.
        if getattr(self, "deck", None) is not None:
            try:
                self.deck.close()
            except Exception:
                pass

        # Step 8: deregistration. The dead controller's active_page was
        # otherwise permanently unevictable (design doc bug 1) and kept
        # distorting clear_old_cached_pages()'s budget for every other live
        # controller.
        if gl.page_manager is not None:
            gl.page_manager.discard_controller(self)
        self.active_page = None

        # Step 9: shut down the per-deck thread pools. The object graph here
        # is cyclic (actions <-> pages <-> controller), so an explicit
        # collect actually reclaims it now instead of waiting on the next
        # generational GC pass.
        action_executor = getattr(self, "action_executor", None)
        if action_executor is not None:
            # Don't wait: a misbehaving plugin callback could block a worker
            # forever; the app's force_quit timer is the backstop.
            action_executor.shutdown(wait=False, cancel_futures=True)
            self.action_executor = None
        load_executor = getattr(self, "load_executor", None)
        if load_executor is not None:
            load_executor.shutdown(wait=False, cancel_futures=True)
            self.load_executor = None
        gc.collect()

    def _teardown_actions(self) -> None:
        """Step 6 of close(): tears down every action this controller ever
        cached a page for -- not just active_page, matching D1's "framework
        calls clean_up() at every drop site" -- plus the screensaver's
        stashed input set/background if the deck is closed mid-screensaver
        (design doc §3.3 item 8): that's where the real page's 50-150MB of
        media actually lives then, not active_page. Never called under
        _load_page_lock, never from app_quit (see close()'s docstring)."""
        cached_pages = gl.page_manager.pages_for_controller(self) if gl.page_manager is not None else []
        for page in cached_pages:
            try:
                page.clear_action_objects()
            except Exception:
                log.opt(exception=True).warning(f"Failed to clear action objects for {page} during close()")

        screen_saver = getattr(self, "screen_saver", None)
        if screen_saver is None:
            return

        original_inputs = screen_saver.original_inputs
        if original_inputs:
            for inputs in list(original_inputs.values()):
                for controller_input in list(inputs):
                    try:
                        controller_input.close_resources()
                    except Exception:
                        log.opt(exception=True).warning("Failed to close a stashed screensaver input during close()")
            original_inputs.clear()

        original_background = screen_saver.original_background
        if original_background is not None:
            try:
                if getattr(original_background, "video", None) is not None:
                    original_background.video.close()
                if getattr(original_background, "image", None) is not None:
                    original_background.image.close()
            except Exception:
                log.opt(exception=True).warning("Failed to close the stashed screensaver background during close()")
            screen_saver.original_background = None

    def delete(self) -> None:
        """Thin alias for close() (plan P1.3), kept for existing callers
        (the harness's teardown() helper, and any code that predates the
        close() sweep)."""
        self.close(remove_media=True, app_quit=False)

    def get_alive(self) -> bool:
        try:
            return self.deck.is_open()
        except Exception as e:
            log.debug(f"Cougth dead deck error. Error: {e}")
            return False

class Background:
    def __init__(self, deck_controller: DeckController):
        self.deck_controller = deck_controller

        self.image = None
        self.video = None

        # Extend the background onto the touchscreen strip (SD+). For static
        # images the slice is memoized because the strip re-composites on
        # every dial label change; for videos update_tiles() refreshes
        # _video_strip once per frame.
        self.extend_to_touchscreen: bool = False
        self._touchscreen_slice: Image.Image = None
        self._video_strip: Image.Image = None

        self.tiles: list[Image.Image] = [None] * deck_controller.deck.key_count()

    def set_image(self, image: "BackgroundImage", update: bool = True) -> None:
        self.image = image
        if self.video is not None:
            self.video.close()
        self.video = None
        self._touchscreen_slice = None
        self._video_strip = None
        # mem-plan P2.5: a content change orphans the whole encode memo --
        # every entry was keyed against the OLD background's composited
        # pixels/hashes, none of which can ever hit again. Left uncleared,
        # a full (32MB) memo from the previous background would simply sit
        # there dead until LRU eviction happened to churn through it.
        encode_memo = getattr(self.deck_controller, "encode_memo", None)
        if encode_memo is not None:
            encode_memo.clear()
        gc.collect()

        self.update_tiles()
        if update:
            self.deck_controller.update_all_inputs()

    def set_video(self, video: "BackgroundVideo", update: bool = True) -> None:
        if self.video is not None:
            self.video.close()
        self.image = None
        self.video = video
        self._touchscreen_slice = None
        self._video_strip = None
        # mem-plan P2.5: see set_image()'s comment -- same reasoning applies
        # to a video-to-video (or image-to-video) content change.
        encode_memo = getattr(self.deck_controller, "encode_memo", None)
        if encode_memo is not None:
            encode_memo.clear()
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

    def get_touchscreen_image(self) -> Image.Image:
        """The strip-sized slice of the current background (image or video
        frame), or None if the background does not extend to the touchscreen."""
        if self.video is not None:
            # Refreshed by update_tiles() once per video frame; None unless
            # the video was built with extend_touchscreen.
            return self._video_strip
        if not self._extend_effective():
            return None
        if self._touchscreen_slice is None:
            self._touchscreen_slice = self.image.get_touchscreen_image()
        return self._touchscreen_slice

    def prebuild_from_path(self, path: str, fps: int = 30, loop: bool = True, allow_keep: bool = True):
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
                    return ("keep", path)
            return ("video", BackgroundVideo(self.deck_controller, path, loop=loop, fps=fps, extend_touchscreen=extend))
        if not os.path.isfile(path):
            return ("noop", None)
        with Image.open(path) as image:
            return ("image", BackgroundImage(self.deck_controller, image.copy(), path=path))

    def apply_prebuilt(self, kind: str, payload, fps: int = 30, loop: bool = True, update: bool = True) -> None:
        """Phase-2 counterpart to prebuild_from_path(): performs the actual
        swap. Callers that need the lock-free/locked split (the screensaver
        transition, plan §4 M3) call this under _background_load_lock with a
        generation re-check already done; no file I/O happens here, only
        object assignment + the same update_all_inputs() fan-out set_video/
        set_image already trigger."""
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

    def set_from_path(self, path: str, fps: int = 30, loop: bool = True, update: bool = True, allow_keep: bool = True) -> None:
        """Synchronous convenience wrapper (prebuild + apply in one call) for
        callers that don't need the lock-free/locked split -- load_background
        (already under _background_load_lock itself) and ScreenSaver's
        setters that act while already showing (plan §4 M3)."""
        kind, payload = self.prebuild_from_path(path, fps=fps, loop=loop, allow_keep=allow_keep)
        self.apply_prebuilt(kind, payload, fps=fps, loop=loop, update=update)

    def update_tiles(self) -> None:
        # Old tiles are reclaimed by refcounting once unreferenced; closing them
        # here would race a concurrent composite still holding one.
        try:
            if self.image is not None:
                self.tiles = self.image.get_tiles(extend_touchscreen=self._extend_effective())
            elif self.video is not None:
                # An extended video frame carries the strip slice as one extra
                # entry after the key tiles (see BackgroundVideoCache).
                entries = self.video.get_next_tiles()
                key_count = self.deck_controller.deck.key_count()
                if self.video.extend_touchscreen and len(entries) > key_count:
                    self._video_strip = entries[key_count]
                    entries = entries[:key_count]
                self.tiles = entries
            else:
                self.tiles = [self.deck_controller.generate_alpha_key() for _ in range(self.deck_controller.deck.key_count())]
        except Exception:
            # A tile error must not kill the media thread; keep the old tiles.
            # Rate-limited: a broken video would otherwise log every frame.
            now = time.time()
            if now - getattr(self, "_last_tile_error_log", 0) > 10:
                self._last_tile_error_log = now
                log.opt(exception=True).error("Failed to update background tiles; keeping previous")

class BackgroundImage:
    def __init__(self, deck_controller: DeckController, image: Image, path: str = None) -> None:
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
        self.image = self._fit_to_canvas(image, self._extend_effective())

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

    def create_full_deck_sized_image(self, extend_touchscreen: bool = False) -> Image:
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

        # Convert to RGBA first to preserve transparency, then resize
        img_rgba = self.image.convert("RGBA")
        return ImageOps.fit(img_rgba, (canvas_width, canvas_height), Image.LANCZOS)

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
        return strip_slice.resize((strip_width, strip_height), Image.LANCZOS)
    
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
    def __init__(self, deck_controller: DeckController, video_path: str, loop: bool = True, fps: int = 30, extend_touchscreen: bool = False) -> None:
        self.deck_controller = deck_controller
        self.video_path = video_path
        self.loop = loop
        self.fps = fps

        self.page: Page = self.deck_controller.active_page

        self.active_frame: int = -1
        self._play_start: float = None  # wall-clock playback start, set on first real-time frame
        self._last_frame_tick: float = None  # last real-time frame pick, for gap clamping

        super().__init__(video_path, deck_controller=deck_controller, extend_touchscreen=extend_touchscreen)

    def get_next_tiles(self) -> list[Image.Image]:
        if self.is_cache_complete():
            # Cache built -> any frame is a free lookup. Pick it by wall-clock so a
            # slow media loop drops frames (stays real-time) instead of playing the
            # video in slow-motion.
            now = time.time()
            if self._play_start is None:
                # Seed the timebase from the current position, not zero: the cache
                # completes mid-play (sequential decode or async disk load), and a
                # zero base would replay a non-looping video / jump a looping one.
                self._play_start = now - (self.active_frame + 1) / float(self.fps or 30)
            elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                # Ticks stop while the page is away; shift the timebase across the
                # gap so playback resumes in place instead of fast-forwarding.
                self._play_start += (now - self._last_frame_tick) - 1.0 / float(self.fps or 30)
            self._last_frame_tick = now
            frame = int((now - self._play_start) * (self.fps or 30))
            self.active_frame = frame % self.n_frames if self.loop else min(frame, self.n_frames - 1)
        else:
            # Still decoding into the cache: advance sequentially so every frame is
            # decoded (wall-clock jumps would leave gaps and force expensive seeks).
            self.active_frame += 1
            if self.active_frame >= self.n_frames and self.loop:
                self.active_frame = 0

        tiles =  self.get_tiles(self.active_frame)
        try:
            copied_tiles = [tile.copy() for tile in tiles]
        except:
            copied_tiles = [None for _ in range(len(tiles))]
        return copied_tiles

class KeyGIF(SingleKeyAsset):
    def __init__(self, controller_key: "ControllerKey", gif_path: str, fps: int = 30, loop: bool = True):
        super().__init__(controller_key)
        self.gif_path = gif_path
        self.fps = fps
        self.loop = loop

        self.active_frame: int = -1
        # Wall-clock timeline state (presenter-migration-plan.md §4 M4):
        # mirrors BackgroundVideo/InputVideo's wall-clock picking, but keyed
        # against a cumulative-delay timeline instead of a fixed fps, since
        # GIF frame durations are per-frame and often irregular.
        self._play_start: float = None
        self._last_frame_tick: float = None

        self.frames = []
        self.frame_delays = []

        # mem-plan P2.3: cap retained frame size at 2x the key tile instead of
        # keeping every frame at source resolution -- a 500px/200-frame GIF is
        # ~200MB at source res vs ~46MB fitted. Composited size is decided per
        # tick by add_image_to_background/get_composed_layout (UI max is 200%,
        # ImageEditor.py), so 2x tile is the largest a frame is ever displayed
        # at; ImageOps.contain preserves aspect ratio and RGBA alpha (cv2's gif
        # demuxer drops alpha, which is why this stays a PIL frame list instead
        # of routing through Mp4FrameCache -- opaque-GIF routing there is a
        # deferred follow-up, not built here).
        tile_w, tile_h = self.deck_controller.get_key_image_size()
        fit_size = (max(1, tile_w * 2), max(1, tile_h * 2))

        # Extract frames and their delays. The source file is only needed for
        # the duration of this loop -- close it immediately after so the app
        # doesn't hold a dangling fd + full-res frame cache alive underneath
        # the fitted copies we keep.
        gif = Image.open(self.gif_path)
        try:
            for frame in ImageSequence.Iterator(gif):
                fitted = frame.convert("RGBA")
                # Shrink-only: contain() would also UPSCALE a small GIF to the
                # 2x budget (a 50px/200-frame GIF would go ~2MB -> ~46MB); a
                # source already within budget composites fine as-is.
                if fitted.width > fit_size[0] or fitted.height > fit_size[1]:
                    fitted = ImageOps.contain(fitted, fit_size)
                self.frames.append(fitted)
                # Get frame delay from GIF metadata (in milliseconds)
                # Default to 100ms (10fps) if no delay specified
                delay = gif.info.get('duration', 100)
                # Some GIFs use delay in centiseconds, convert to milliseconds
                if delay < 50:
                    delay *= 10
                self.frame_delays.append(delay)
        finally:
            gif.close()

        # Cumulative delay timeline in seconds: _cum_delays[i] is the
        # wall-clock time at which frame i's display window ENDS. Picking a
        # frame for elapsed time t is then a single bisect (see
        # get_next_frame) instead of a per-tick increment-and-compare loop.
        self._cum_delays: list[float] = list(
            itertools.accumulate(d / 1000.0 for d in self.frame_delays)
        )
        self._total_delay: float = self._cum_delays[-1] if self._cum_delays else 0.0

    def get_next_frame(self, now: float = None) -> Image.Image:
        n = len(self.frames)
        if n == 0:
            return None
        if n == 1 or self._total_delay <= 0:
            # Single-frame GIF, or no usable timing info: nothing to pick.
            self.active_frame = 0
            return self.frames[0]

        if now is None:
            now = time.time()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            # Ticks stopped while the page/key was away (screensaver, page
            # switch, suspend): shift the timebase across the gap so playback
            # resumes near where it left off instead of fast-forwarding
            # through the whole gap (mirrors BackgroundVideo's gap clamp).
            frame_period = self._cum_delays[0] if self._cum_delays else self._total_delay / n
            self._play_start += (now - self._last_frame_tick) - frame_period
        self._last_frame_tick = now

        elapsed = now - self._play_start
        t = elapsed % self._total_delay if self.loop else min(elapsed, self._total_delay)

        frame = bisect.bisect_right(self._cum_delays, t)
        if frame >= n:
            frame = n - 1  # guard the end: float-edge / non-loop clamp landing on t == total
        self.active_frame = frame

        return self.frames[self.active_frame]

    def get_frame_delay(self) -> float:
        """Get delay for current frame in seconds"""
        if self.active_frame < 0 or self.active_frame >= len(self.frame_delays):
            return 1.0 / self.fps  # Fallback to fps-based timing
        return self.frame_delays[self.active_frame] / 1000.0  # Convert ms to seconds
    
    def get_raw_image(self) -> Image.Image:
        return self.get_next_frame()
    
    def close(self) -> None:
        self.frames = None
        self.frame_delays = None
        del self.frames
        del self.frame_delays

class LabelManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input
        
        self.page_labels = {}
        self.action_labels = {}
        self.scroll_wait = 25
        self._has_scroll_labels_cache: bool = None
        self._has_visible_labels_cache: bool = None

        self.init_labels()
        self.frames: dict[str, dict[str, int]] = {
            "top": {
                "position": 0,
                "wait": self.scroll_wait
            },
            "center": {
                "position": 0,
                "wait": self.scroll_wait
            },
            "bottom": {
                "position": 0,
                "wait": self.scroll_wait
            },
        }

    def init_labels(self):
        for position in ["top", "center", "bottom"]:
            self.page_labels[position] = KeyLabel(self.controller_input)
            self.action_labels[position] = KeyLabel(self.controller_input)
 
    def clear_labels(self):
        self.init_labels()
        self._has_scroll_labels_cache = None
        self._has_visible_labels_cache = None

    def set_page_label(self, position: str, label: "KeyLabel", update: bool = True):
        if label is None:
            label = self.page_labels[position]
            label.clear_values()
        else:
            self.page_labels[position] = label

        self._has_scroll_labels_cache = None
        self._has_visible_labels_cache = None
        if update:
            self.update_label(position)

    @staticmethod
    def _label_equals(a: "KeyLabel", b: "KeyLabel") -> bool:
        return (a.text == b.text and a.font_size == b.font_size
                and a.font_name == b.font_name and a.color == b.color
                and a.font_weight == b.font_weight and a.style == b.style
                and a.outline_width == b.outline_width
                and a.outline_color == b.outline_color
                and a.alignment == b.alignment)

    def set_action_label(self, position: str, label: "KeyLabel", update: bool = True):
        if label is None:
            label = self.action_labels[position]
            label.clear_values()
        else:
            old = self.action_labels.get(position)
            if old is not None and self._label_equals(old, label):
                return
            self.action_labels[position] = label

        self._has_scroll_labels_cache = None
        self._has_visible_labels_cache = None
        GLib.idle_add(self.update_label_editor)
        if update:
            self.update_label(position)

    def update_label_editor(self):
        if not recursive_hasattr(gl, "app.main_win.sidebar.active_identifier"):
            return
        
        if gl.app.main_win.sidebar.active_identifier != self.controller_input.identifier:
            return
        
        controller = gl.app.main_win.get_active_controller()
        if controller is not self.controller_input.deck_controller:
            return

        gl.app.main_win.sidebar.key_editor.label_editor.load_for_identifier(self.controller_input.identifier, self.controller_input.state)
        

    def get_use_page_label_properties(self, position: str) -> dict:
        if self.page_labels.get(position) is None:
            return {
                "text": False,
                "color": False,
                "font-family": False,
                "font-size": False,
                "font-weight": False,
                "font-style": False,
                "outline_width": False,
                "outline_color": False,
                "alignment": False,
            }
        return {
            "text": self.page_labels[position].text is not None,
            "color": self.page_labels[position].color is not None,
            "font-family": self.page_labels[position].font_name is not None,
            "font-size": self.page_labels[position].font_size is not None,
            "font-weight": self.page_labels[position].font_weight is not None,
            "font-style": self.page_labels[position].style is not None,
            "outline_width": self.page_labels[position].outline_width is not None,
            "outline_color": self.page_labels[position].outline_color is not None,
            "alignment": self.page_labels[position].alignment is not None,
        }

    def get_composed_label(self, position: str) -> str:
        use_page_label_properties = self.get_use_page_label_properties(position)
        
        label = copy(self.action_labels.get(position)) or KeyLabel(self.controller_input)

        # Set to page values
        page_label = self.page_labels.get(position)
        if page_label is not None:
            if use_page_label_properties["text"]:
                label.text = page_label.text
            if use_page_label_properties["color"]:
                label.color = page_label.color
            if use_page_label_properties["font-family"]:
                label.font_name = page_label.font_name
            if use_page_label_properties["font-size"]:
                label.font_size = page_label.font_size
            if use_page_label_properties["font-weight"]:
                label.font_weight = page_label.font_weight
            if use_page_label_properties["font-style"]:
                label.style = page_label.style
            if use_page_label_properties["outline_width"]:
                label.outline_width = page_label.outline_width
            if use_page_label_properties["outline_color"]:
                label.outline_color = page_label.outline_color
            if use_page_label_properties["alignment"]:
                label.alignment = page_label.alignment

        injected = self.inject_defaults(label)
        return self.fix_invalid(injected)
    
    def get_composed_labels(self) -> dict[str, "KeyLabel"]:
        composed_labels = {}
        for position in ["top", "center", "bottom"]:
            composed_labels[position] = self.get_composed_label(position)
        return composed_labels

    
    def inject_defaults(self, label: "KeyLabel"):
        if label.text is None:
            label.text = ""
        if label.color is None:
            label.color = gl.settings_manager.font_defaults.get("font-color") or (255, 255, 255, 255)
        if label.font_name is None:
            label.font_name = gl.settings_manager.font_defaults.get("font-family") or gl.fallback_font
        if label.font_size is None:
            label.font_size = round(gl.settings_manager.font_defaults.get("font-size") or 15)
        if label.font_weight is None:
            label.font_weight = round(gl.settings_manager.font_defaults.get("font-weight") or 400)
        if label.style is None:
            label.style = gl.settings_manager.font_defaults.get("font-style") or "normal"
        if label.outline_width is None:
            label.outline_width = round(gl.settings_manager.font_defaults.get("outline-width") or 2)
        if label.outline_color is None:
            label.outline_color = gl.settings_manager.font_defaults.get("outline-color") or (0, 0, 0, 255)
        if label.alignment is None:
            label.alignment = gl.settings_manager.font_defaults.get("alignment") or "center"

        return label
    
    def fix_invalid(self, label: "KeyLabel"):
        if not isinstance(label.text, str):
            label.text = str(label.text)

        return label

    def update_label(self, position: str):
        self.controller_input.update()

    def get_available_width(self) -> int:
        return self.controller_input.get_image_size()[0]

    def get_has_visible_labels(self) -> bool:
        # A label is drawn iff its text is non-empty (see add_labels_to_image).
        if self._has_visible_labels_cache is None:
            labels = self.get_composed_labels()
            self._has_visible_labels_cache = any(
                label.text not in (None, "") for label in labels.values())
        return self._has_visible_labels_cache

    def get_has_scroll_labels(self) -> bool:
        if self._has_scroll_labels_cache is not None:
            return self._has_scroll_labels_cache

        labels = self.get_composed_labels()
        for label in labels:
            if labels[label].text is not None and labels[label].text != "":
                _, _, w, _ = labels[label].get_font().getbbox(labels[label].text)
                if w > self.get_available_width():
                    self._has_scroll_labels_cache = True
                    return True
        self._has_scroll_labels_cache = False
        return False

    def add_labels_to_image(self, image: Image.Image) -> Image.Image:
        # image = image.rotate(self.deck.get_rotation()*-1)
        draw = ImageDraw.Draw(image)

        labels = self.get_composed_labels()
        for label in labels:
            text = labels[label].text
            if text in [None, ""]:
                continue

            color = tuple(labels[label].color)
            font = labels[label].get_font()
            outline_width = labels[label].outline_width
            outline_color = tuple(labels[label].outline_color)
            alignment = labels[label].alignment

            _, _, w, h = draw.textbbox((0, 0), text, font=font)

            # Calculate x position based on alignment
            padding = 3
            if alignment == "left":
                x_position = padding
                anchor_x = "l"
            elif alignment == "right":
                x_position = image.width - padding
                anchor_x = "r"
            else:  # center (default)
                x_position = image.width / 2
                anchor_x = "m"

            rolling_labels_enabled = gl.settings_manager.get_app_settings().get("general", {}).get("rolling-labels", True)
            if rolling_labels_enabled and image.width < w:
                # Need to scroll - always use center anchor for scrolling
                start = image.width / 2 - (image.width - w) / 2 + 10
                stop = image.width / 2 + (image.width - w) / 2 - 10

                x_position = start - self.frames[label]["position"]
                anchor_x = "m"
                if x_position < stop:
                    if self.frames[label]["wait"] == 0:
                        x_position = start
                        self.frames[label]["position"] = 0
                        self.frames[label]["wait"] = self.scroll_wait
                    else:
                        self.frames[label]["wait"] -= 1
                elif self.controller_input.media_ticks % 2 == 0:
                    if self.frames[label]["wait"] == 0:
                        if x_position == stop:
                            self.frames[label]["wait"] = self.scroll_wait

                        self.frames[label]["position"] += 1
                    else:
                        self.frames[label]["wait"] -= 1


            if label == "top":
                position = (x_position, h/2 + 3)
            elif label == "bottom":
                position = (x_position, image.height - h/2 - 3)
            else:
                position = (x_position, (image.height - 0) / 2)

            # Use appropriate anchor based on alignment (x-anchor + "m" for vertical middle)
            anchor = anchor_x + "m"

            draw.text(position,
                      text=text, font=font, anchor=anchor, align=alignment,
                      fill=color, stroke_width=outline_width,
                      stroke_fill=outline_color)

        del draw

        return image.copy()
        # return image.copy().rotate(self.deck.get_rotation())


class LayoutManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input

        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()

        # (token, layout key, resized image): the resized foreground for a
        # static asset, valid while the caller passes the same asset object
        # and the layout/geometry is unchanged. Single tuple so concurrent
        # updates swap it atomically.
        self._fg_cache: tuple = None

    def clear(self):
        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()
        self._fg_cache = None

    def get_use_page_layout_properties(self) -> dict:
        return {
            "valign": self.page_layout.valign is not None,
            "halign": self.page_layout.halign is not None,
            "fill-mode": self.page_layout.fill_mode is not None,
            "size": self.page_layout.size is not None
        }
    
    def get_composed_layout(self) -> ImageLayout:
        use_page_layout_properties = self.get_use_page_layout_properties()
        
        layout = copy(self.action_layout) or ImageLayout()

        # Set to page values
        page_layout = self.page_layout
        if use_page_layout_properties["valign"]:
            layout.valign = page_layout.valign
        if use_page_layout_properties["halign"]:
            layout.halign = page_layout.halign
        if use_page_layout_properties["fill-mode"]:
            layout.fill_mode = page_layout.fill_mode
        if use_page_layout_properties["size"]:
            layout.size = page_layout.size

        return self.inject_defaults(layout)
    
    def inject_defaults(self, layout: ImageLayout):
        if layout.valign is None:
            layout.valign = 0
        if layout.halign is None:
            layout.halign = 0
        if layout.fill_mode is None:
            if isinstance(self.controller_input.identifier, Input.Key):
                layout.fill_mode = "cover"
            else:
                layout.fill_mode = "contain"
        if layout.size is None:
            layout.size = 1

        return layout
    
    def set_page_layout(self, layout: ImageLayout, update: bool = True):
        self.page_layout = layout

        if update:
            self.update()

    def set_action_layout(self, layout: ImageLayout, update: bool = True):
        self.action_layout = layout

        if update:
            self.update()

    def update(self):
        self.controller_input.update()
        GLib.idle_add(self.update_layout_editor)

    def update_layout_editor(self):
        if not recursive_hasattr(gl, "app.main_win.leftArea.deck_stack"):
            return
        
        if gl.app.main_win.sidebar.active_identifier != self.controller_input.identifier:
            return

        controller = gl.app.main_win.get_active_controller()
        if controller is not self.controller_input.deck_controller:
            return

        gl.app.main_win.sidebar.key_editor.image_editor.load_for_identifier(self.controller_input.identifier, self.controller_input.state)

    def add_image_to_background(self, image: Image.Image, background: Image.Image, cache_token=None) -> Image.Image:
        if image is None:
            return background
        layout = self.get_composed_layout()

        width, height = background.size
        image_size = (int(width * layout.size), int(height * layout.size))

        if 0 in image_size:
            return background.copy()

        # The resized foreground depends only on the source asset and layout,
        # not on the (possibly animated) background. cache_token is the asset
        # object itself: assets are replaced, never mutated, so a held
        # reference can't go stale (and can't collide, unlike a freed id()).
        fg_key = (layout.fill_mode, layout.halign, layout.valign, image_size)
        image_resized = None
        if cache_token is not None:
            cached = self._fg_cache
            if cached is not None and cached[0] is cache_token and cached[1] == fg_key:
                image_resized = cached[2]
                if media_prof:
                    media_prof.count("fg_cache_hit")

        if image_resized is None:
            if layout.fill_mode == "stretch":
                image_resized = image.resize(image_size, Image.Resampling.HAMMING)
            elif layout.fill_mode == "cover":
                image_resized = ImageOps.cover(image, image_size, Image.Resampling.HAMMING)
            else:
                image_resized = ImageOps.contain(image, image_size, Image.Resampling.HAMMING)
            if cache_token is not None:
                self._fg_cache = (cache_token, fg_key, image_resized)
                if media_prof:
                    media_prof.count("fg_cache_miss")

        halign = layout.halign
        valign = layout.valign

        left_margin = int((background.width - image_resized.width) * (halign + 1) / 2)
        top_margin = int((background.height - image_resized.height) * (valign + 1) / 2)

        # Create an image copy for the result
        final_image = background.copy()

        # Paste the resized foreground onto the composite image at the calculated position
        if image_resized.has_transparency_data:
            final_image.paste(image_resized, (left_margin, top_margin), image_resized)
        else:
            final_image.paste(image_resized, (left_margin, top_margin))

        return final_image
    

class BackgroundManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input
        
        self.action_color: list[int] = None
        self.page_color: list[int] = None

    def set_action_color(self, color: list[int], update: bool = True) -> None:
        self.action_color = color
        if isinstance(color, list) and len(color) == 3:
            self.action_color.append(255)

        if update:
            self.update()

    def set_page_color(self, color: list[int], update: bool = True, update_ui: bool = True) -> None:
        self.page_color = color
        if isinstance(color, list) and len(color) == 3:
            self.page_color.append(255)

        if update:
            self.update(ui=update_ui)

    def update(self, ui: bool = True):
        self.controller_input.update()
        if ui:
            GLib.idle_add(self.update_background_editor)

    def update_background_editor(self):
        if not recursive_hasattr(gl, "app.main_win.leftArea.deck_stack"):
            return
        
        if gl.app.main_win.sidebar.active_identifier != self.controller_input.identifier:
            return

        controller = gl.app.main_win.get_active_controller()
        if controller is not self.controller_input.deck_controller:
            return

        gl.app.main_win.sidebar.key_editor.background_editor.load_for_identifier(self.controller_input.identifier, self.controller_input.state)

    def get_color_is_set(self, color: list[int]) -> bool:
        return color not in [None, [None]*3, [None]*4]

    def get_use_page_background(self) -> dict:
        return self.get_color_is_set(self.page_color)
    
    def get_composed_color(self) -> list[int]:
        if self.get_use_page_background() and self.get_color_is_set(self.page_color):
            return self.page_color
        elif self.get_color_is_set(self.action_color):
            return self.action_color
        else:
            return [0] * 4


class ControllerInputState:
    def __init__(self, controller_input: "ControllerInput", state: int):
        self.controller_input = controller_input
        self.deck_controller = controller_input.deck_controller
        self.state = state
        self._overlay: Image.Image = None
        self.hide_overlay_timer: "timer_wheel.TimerHandle" = None

        # True while this state's on_tick is still running; the next tick is
        # dropped, not queued (see own_actions_tick_threaded).
        self._tick_running: bool = False
        self._tick_started_at: float = 0.0
        self._tick_stuck_warned: bool = False

        # managers
        self.layout_manager = LayoutManager(self.controller_input)
        self.label_manager = LabelManager(self.controller_input)
        self.background_manager = BackgroundManager(self.controller_input)

        self.action_permission_manager = ActionPermissionManager(self)

    def __int__(self):
        return self.state
    
    def ready(self):
        pass

    def stop_overlay_timer(self):
        if self.hide_overlay_timer is not None:
            self.hide_overlay_timer.cancel()
            self.hide_overlay_timer = None

    def show_overlay(self, image: Image.Image, duration: int = -1):
        """
        duration: -1 for infinite
        """
        if duration == 0:
            self.stop_overlay_timer()
            self._overlay = None
            self.update()
        elif duration > 0:
            # Cancel any in-flight hide timer first so repeated overlays don't
            # orphan its thread.
            self.stop_overlay_timer()
            self._overlay = image
            self.update()
            self.hide_overlay_timer = timer_wheel.schedule(duration, self.hide_error, name="OverlayHideTimer")
        else:
            self._overlay = image
            self.update()

    def hide_overlay(self):
        # Must be None, not False: the tile-passthrough fast path in
        # ControllerKey.get_current_image tests `state._overlay is None`.
        self._overlay = None
        self.update()

    def show_error(self, duration: int = -1):
        error_img = Image.open(os.path.join("Assets", "images", "error.png"))
        self.show_overlay(error_img, duration=duration)

    def hide_error(self):
        self.hide_overlay()

    def close_resources(self) -> None:
        pass

    def get_own_actions(self) -> list["ActionCore"]:
        if not self.deck_controller.get_alive(): return []
        active_page = self.deck_controller.active_page
        active_page = self.controller_input.deck_controller.active_page
        if active_page is None:
            return []
        if active_page.action_objects is None:
            return []
        actions = self.deck_controller.active_page.get_all_actions_for_input(self.controller_input.identifier, self.state)

        return actions

    def update(self) -> None:
        if self.controller_input.state == self.state:
            self.controller_input.update()
    
    def own_actions_update(self) -> None:
        for action in self.get_own_actions():
            if not isinstance(action, ActionCore):
                continue
            if not action.on_ready_called:
                continue
            action.on_update()

    @log.catch
    def own_actions_tick(self) -> None:
        for action in self.get_own_actions():
            if not isinstance(action, ActionCore):
                continue
            if not action.on_ready_called:
                continue
            action.on_tick()

    @log.catch
    def own_actions_event_callback(self, event: InputEvent, data: dict = None, show_notifications: bool = False) -> None:
        for action in self.get_own_actions():
            if isinstance(action, ActionOutdated):
                if show_notifications:
                    plugin_id = gl.plugin_manager.get_plugin_id_from_action_id(action.id)
                    gl.app.send_outdated_plugin_notification(plugin_id)
                continue
            if isinstance(action, NoActionHolderFound):
                if show_notifications:
                    plugin_id = gl.plugin_manager.get_plugin_id_from_action_id(action.id)
                    gl.app.send_missing_plugin_notification(plugin_id)
                continue

            # parsed_event = event
            # if action.allow_event_configuration:
                # parsed_event = action.event_manager.get_event_assigner_for_event(event)

            if event is None:
                continue

            if not isinstance(action, ActionCore):
                continue

            action._raw_event_callback(event, data)

    def _submit_action_callback(self, fn, *args) -> "Future | None":
        """Route an action callback through the deck's bounded thread pool.

        Returns the Future, or None if the executor is unavailable (deck being
        torn down).
        """
        executor = getattr(self.deck_controller, "action_executor", None)
        if executor is None:
            return None
        try:
            future = executor.submit(fn, *args)
        except RuntimeError:
            # Executor already shut down (deck disconnected mid-call)
            return None
        future.add_done_callback(self._log_callback_exception)
        return future

    def own_actions_ready_threaded(self) -> None:
        self._submit_action_callback(self.own_actions_ready)

    def own_actions_update_threaded(self) -> None:
        self._submit_action_callback(self.own_actions_update)

    def own_actions_tick_threaded(self) -> None:
        # Drop (don't queue) this tick while the previous one is still running,
        # so a slow plugin on_tick() can't pile up unbounded callbacks.
        if self._tick_running:
            if not self._tick_stuck_warned and time.monotonic() - self._tick_started_at > 10.0:
                self._tick_stuck_warned = True
                log.warning(f"on_tick for {self.controller_input.identifier} has been running >10s; this input's updates are paused until it returns")
            return
        self._tick_running = True
        self._tick_stuck_warned = False
        self._tick_started_at = time.monotonic()
        future = self._submit_action_callback(self.own_actions_tick)
        if future is None:
            self._tick_running = False
        else:
            future.add_done_callback(self._on_tick_done)

    def _on_tick_done(self, _future: "Future") -> None:
        self._tick_running = False

    def _log_callback_exception(self, future: "Future") -> None:
        try:
            exc = future.exception()
        except Exception:
            return
        if exc is not None:
            log.opt(exception=exc).error(f"Action callback for {self.controller_input.identifier} raised")

    def own_actions_event_callback_threaded(self, event: InputEvent, data: dict = None, show_notifications: bool = False) -> None:
        self._submit_action_callback(self.own_actions_event_callback, event, data, show_notifications)

    def remove_media(self) -> None:
        page = self.controller_input.deck_controller.active_page
        if page is None:
            return

        page.set_media_path(identifier=self.controller_input.identifier, state=self.state, path=None)

        self.update()


class ControllerInput:
    def __init__(self, deck_controller: DeckController, state_class: ControllerInputState, identifier: InputIdentifier):
        self.deck_controller = deck_controller
        self.state = 0
        self.hide_error_timer: Timer = None
        self.hold_start_timer: "timer_wheel.TimerHandle" = None
        self.ControllerStateClass = state_class
        self.identifier: InputIdentifier = identifier
        self.media_ticks: int = 0
        # Generation of the content this input holds; paints tag it at render
        # start and are dropped at the present boundary once it's superseded.
        self.config_gen: int = 0

        self.is_visual: bool = True

        self.enable_states: bool = True

        self.states: dict[int, ControllerInputState] = {
            0: self.ControllerStateClass(self, 0),
        }

        self.states[self.state].ready()

    @staticmethod
    def Available_Identifiers(deck):
        raise AttributeError

    def update(self) -> None:
        pass

    def event_callback(self) -> None:
        pass

    def start_hold_timer(self):
        self.stop_hold_timer()

        self.hold_start_timer = timer_wheel.schedule(self.deck_controller.hold_time, self.on_hold_timer_end, name="HoldTimer")

    def stop_hold_timer(self):
        if self.hold_start_timer is None:
            return
        
        self.hold_start_timer.cancel()
        self.hold_start_timer = None

    def create_n_states(self, n: int):
        if not self.enable_states:
            n = 1

        for state in self.states.values():
            state.close_resources()
        self.states.clear()

        for i in range(n):
            self.states[i] = self.ControllerStateClass(self, i)

    def load_from_page(self, page: Page):
        input_dict = self.identifier.get_config(page)
        self.load_from_input_dict(input_dict)

    def load_from_input_dict(self, page_dict, update: bool = True):
        pass

    def add_new_state(self, switch: bool = True):
        if not self.enable_states:
            if len(self.states) >= 1:
                return
            
        d = self.identifier.get_config(self.deck_controller.active_page)

        # Add new state
        self.states[len(self.states)] = self.ControllerStateClass(self, len(self.states))
        # Write to json
        for state in self.states.keys():
            d["states"].setdefault(str(state), {})

        self.deck_controller.active_page.save()
        gl.page_manager.update_dict_of_pages_with_path(self.deck_controller.active_page.json_path)

        self.update_state_switcher()

        if switch:
            log.info(f"Switching to state: {len(self.states)-1}")
            self.set_state(len(self.states)-1)

    def remove_state(self, state: int):
        d = self.identifier.get_config(self.deck_controller.active_page)

        if str(state) in d["states"]:
            d["states"].pop(str(state))

        old_loaded_state = int(self.state)

        state_to_remove = self.states.get(state)
        if state_to_remove:
            state_to_remove.close_resources()
            self.states.pop(state)

        # Fill gaps in self.states
        sorted_state_keys = sorted(self.states.keys())

        new_states = {}
        state_map = {}
        for new_key, old_key in enumerate(sorted_state_keys):
            state_map[old_key] = new_key
            self.states[old_key].state = new_key

            if self.get_active_state() is self.states[old_key]:
                self.state = new_key

            new_states[new_key] = self.states[old_key]

        self.states = new_states

        new_states_dict = {}
        for new_key, old_key in enumerate(d["states"].keys()):
            new_states_dict[str(new_key)] = d["states"][old_key]

        d["states"] = new_states_dict


        self.deck_controller.active_page.save()
        gl.page_manager.update_dict_of_pages_with_path(self.deck_controller.active_page.json_path)

        self.update_state_switcher()

        # Update - TODO: test
        if state == self.state:
            sort = sorted(list(self.states.keys()))
            sort.reverse()
            for s in sort:
                if s <= state:
                    self.set_state(s, allow_reload=True)
                    break

        gl.signal_manager.trigger_signal(Signals.RemoveState, state, state_map)

    def update_state_switcher(self):
        if gl.app.main_win.sidebar.active_identifier != self.identifier:
            return

        gl.app.main_win.sidebar.key_editor.state_switcher.set_n_states(len(self.states))

    def get_active_state(self) -> "ControllerInputState":
        state = self.states.get(self.state)
        return state if state is not None else self.ControllerStateClass(self, -1)

    def set_state(self, state: int, update_sidebar: bool = True, allow_reload: bool = False) -> None:
        if state == self.state and not allow_reload:
            return
        
        if state not in self.states:
            log.error(f"Invalid state: {state}, must be one of {list(self.states.keys())}")
            return
        self.state = state

        self.get_active_state().update()

        if update_sidebar:
            self.reload_sidebar()

    def reload_sidebar(self) -> None:
        visible_child = gl.app.main_win.leftArea.deck_stack.get_visible_child()
        if visible_child is None:
            return
        controller = visible_child.deck_controller
        if controller is None:
            return
        
        if controller is not self.deck_controller:
            return
        if self.identifier != gl.app.main_win.sidebar.active_identifier:
            return
        
        gl.app.main_win.sidebar.active_state = self.state
        GLib.idle_add(gl.app.main_win.sidebar.update)

    def load_from_config(self, config, update: bool = True):
        n_states = len(config.get("states", {}))
        self.create_n_states(max(1, n_states))

        old_state_index = self.state

        self.state = 0

        #TODO: Reset states
        for state in config.get("states", {}):
            state: ControllerKeyState = self.states.get(int(state))
            if state is None:
                continue

            state_dict = config["states"][str(state.state)]

            self.get_active_state().own_actions_ready()
            # state.own_actions_ready() # Why not threaded? Because this would mean that some image changing calls might get executed after the next lines which blocks custom assets

            if update:
                self.set_state(old_state_index)
                self.update()

    def clear(self, update: bool = True):
        active_state = self.get_active_state()
        active_state.clear()
        if update:
            self.update()

    def close_resources(self) -> None:
        """Framework teardown hook (plan P1.3 step 7/design doc bug 19):
        releases every state's media resources. Unlike clear(), this is for
        the input's own end of life (deck close, screensaver-stash sweep),
        not a fresh page load -- it never triggers a repaint."""
        for state in self.states.values():
            state.close_resources()

    def has_unavailable_action(self) -> bool:
        for action in self.get_active_state().get_own_actions():
            if isinstance(action, ActionOutdated):
                return True
            if isinstance(action, NoActionHolderFound):
                return True
            
        return False
    
    def get_empty_background(self) -> Image.Image:
        pass

    def get_image_size(self) -> tuple[int, int]:
        pass

class ControllerKey(ControllerInput):
    def __init__(self, deck_controller: DeckController, ident: Input.Key):
        super().__init__(deck_controller, ControllerKeyState, ident)
        self.index = ident.get_index(deck_controller)
        # Keep track of the current state of the key because self.deck_controller.deck.key_states seams to give inverted values in get_current_deck_image
        self.press_state: bool = self.deck_controller.deck.key_states()[self.index]

        self.down_start_time: float = None

    def on_hold_timer_end(self):
        state = self.get_active_state()
        state.own_actions_event_callback_threaded(
            event=Input.Key.Events.HOLD_START
        )

    @staticmethod
    def Available_Identifiers(deck):
        return map(lambda x: f"{x[0]}x{x[1]}", map(lambda x: ControllerKey.Index_To_Coords(deck, x), range(deck.key_count())))

    @staticmethod
    def Index_To_Coords(deck, index):
        rows, cols = deck.key_layout()    
        y = index // cols
        x = index % cols
        return x, y
    
    @staticmethod
    def Coords_To_Index(deck, coords):
        if type(coords) == str:
            coords = coords.split("x")
        x, y = map(int, coords)
        rows, cols = deck.key_layout()
        return y * cols + x

    def update(self, force: bool = False):
        # Capture page/generation before rendering, so a switch mid-render
        # invalidates this paint at the present boundary.
        page = self.deck_controller.active_page
        config_gen = self.config_gen
        if media_prof:
            _t0 = time.perf_counter()
        image = self.get_current_image()
        if media_prof:
            _t1 = time.perf_counter()
            media_prof.add("composite", _t1 - _t0)

        # Quick hash check - skip expensive conversion only if the image matches
        # BOTH the last presented hash (_last_img_hash, set in the task's run())
        # and the last enqueued hash: either alone can be stale (dropped paint /
        # in-flight revert) and would wrongly skip the correcting repaint.
        img_hash = hash(image.tobytes())
        if media_prof:
            _t2 = time.perf_counter()
            media_prof.add("hash", _t2 - _t1)
        if (not force and img_hash == getattr(self, '_last_img_hash', None)
                and img_hash == getattr(self, '_last_enqueued_hash', None)):
            if media_prof:
                media_prof.count("hash_skip")
            image.close()
            return

        if self.deck_controller.is_visual():
            memo_key = (img_hash, self.deck_controller.deck.get_rotation())
            native_image = self.deck_controller.encode_memo.get(memo_key)
            if native_image is None:
                # Handle transparency properly - composite RGBA onto RGB to preserve smooth edges
                if image.mode == "RGBA":
                    rgb_background = Image.new("RGB", image.size, (0, 0, 0))
                    rgb_background.paste(image, (0, 0), image)
                    rgb_image = rgb_background.rotate(self.deck_controller.deck.get_rotation())
                else:
                    rgb_image = image.convert("RGB").rotate(self.deck_controller.deck.get_rotation())
                native_image = encode_native_key(self.deck_controller.deck, rgb_image)
                rgb_image.close()
                self.deck_controller.encode_memo.put(memo_key, native_image)
                if media_prof:
                    media_prof.add("encode", time.perf_counter() - _t2)
                    media_prof.count("memo_miss")
            elif media_prof:
                media_prof.count("memo_hit")
            self._last_enqueued_hash = img_hash
            self.deck_controller.media_player.add_image_task(self.index, native_image, page=page, config_gen=config_gen, controller_key=self, img_hash=img_hash)

        self.set_ui_key_image(image)

    def get_active_state(self) -> "ControllerKeyState":
        return super().get_active_state()

    def on_media_player_tick(self) -> None:
        self.media_ticks += 1

        state = self.get_active_state()
        needs_update = False

        # Check if we need to update based on content type
        if state.key_video is not None:
            # Both InputVideo and KeyGIF now pick their current frame from
            # their own wall-clock timeline (presenter-migration-plan.md §4
            # M4); the tick just asks for whatever frame is current -- it no
            # longer needs to pre-compute whether the GIF's frame delay has
            # elapsed. This also matches how non-GIF videos were already
            # handled here (unconditional needs_update).
            needs_update = True
        elif state.label_manager.get_has_scroll_labels():
            needs_update = True
        elif self.deck_controller.background.video is not None:
            # An opaque background color hides the video tile (see
            # get_current_image), so that key can't change frame-to-frame.
            if state.background_manager.get_composed_color()[-1] < 255:
                needs_update = True

        if needs_update:
            self.update()

    def event_callback(self, press_state):
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if press_state:
            # Only on key down this allows plugins to control screen saver without directly deactivating it
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            return
        
        self.deck_controller.mark_page_ready_to_clear(False)
        self.press_state = press_state

        self.update()

        active_state = self.get_active_state()
        if press_state: # Key down
            self.down_start_time = time.time()
            self.start_hold_timer()
            active_state.own_actions_event_callback_threaded(
                event=Input.Key.Events.DOWN,
                show_notifications=True
            )

        elif self.down_start_time is not None: # Key up
            if time.time() - self.down_start_time >= self.deck_controller.hold_time:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Key.Events.HOLD_STOP
                )
            else:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Key.Events.SHORT_UP
                )
            self.down_start_time = None
            self.stop_hold_timer()
            active_state.own_actions_event_callback_threaded(
                event=Input.Key.Events.UP,
                show_notifications=False
            )
        self.deck_controller.mark_page_ready_to_clear(True)

    def get_current_image(self) -> Image.Image:
        state = self.get_active_state()

        background_color = self.get_active_state().background_manager.get_composed_color()

        # A key with no color layer, media, labels, or markers composites to
        # exactly the shared background tile; return a copy of it directly
        # (matters per-frame over an animated background).
        if (background_color[-1] == 0
                and state._overlay is None
                and state.key_image is None
                and state.key_video is None
                and not state.label_manager.get_has_visible_labels()
                and not self.is_pressed()
                and not (self.has_unavailable_action() and not self.deck_controller.screen_saver.showing)):
            tile = self.deck_controller.background.tiles[self.index]
            if tile is not None:
                if media_prof:
                    media_prof.count("tile_passthrough")
                return copy(tile)

        if media_prof:
            _t0 = time.perf_counter()

        background: Image.Image = None
        # Only load the background image if it's not gonna be hidden by the background color
        if background_color[-1] < 255:
            background = copy(self.deck_controller.background.tiles[self.index])

        if background_color[-1] > 0:
            background_color_img = Image.new("RGBA", self.deck_controller.get_key_image_size(), color=tuple(background_color))
            
            if background is None:
                # Use the color as the only background - happens if background color alpha is 255
                background = background_color_img
            else:
                background.paste(background_color_img, (0, 0), background_color_img)


        if background is None:
            background = self.deck_controller.generate_alpha_key().copy()

        if media_prof:
            _t1 = time.perf_counter()
            media_prof.add("c_tile", _t1 - _t0)

        if state._overlay:
            height = round(self.deck_controller.get_key_image_size()[1]*0.75)
            img = state._overlay.resize((height, height))
            background.paste(img, (int((self.deck_controller.get_key_image_size()[0] - height) // 2), int((self.deck_controller.get_key_image_size()[1] - height) // 2)), img)
            return background


        key_image: Image.Image = None
        # rotation = self.deck_controller.get_deck_settings().get("rotation", {}).get("value", 0)
        if state.key_image is not None:
            image = state.key_image.get_raw_image()
            key_image = state.layout_manager.add_image_to_background(
                image=image,
                background=background,
                # Static asset: the resize is cacheable (video/GIF is not).
                cache_token=state.key_image
            )
        elif state.key_video is not None:
            image = state.key_video.get_raw_image()
            key_image = state.layout_manager.add_image_to_background(
                image=image,
                background=background)
        else:
            key_image = background

        if media_prof:
            _t2 = time.perf_counter()
            media_prof.add("c_layout", _t2 - _t1)

        labeled_image = state.label_manager.add_labels_to_image(key_image)

        if media_prof:
            media_prof.add("c_labels", time.perf_counter() - _t2)

        if self.is_pressed():
            labeled_image = self.shrink_image(labeled_image)

        if self.has_unavailable_action() and not self.deck_controller.screen_saver.showing:
            labeled_image = self.add_warning_point(labeled_image)

        if background is not None:
            background.close()

        key_image.close()

        return labeled_image
    
    def add_warning_point(self, image: Image.Image, margin: int = 10, size: int = 10, color: tuple = (255, 150, 80)) -> Image.Image:
        draw = ImageDraw.Draw(image)

        # Calculate the coordinates of the top right circle
        width, height = image.size
        top_right_x = width - margin - size
        top_right_y = margin

        # Draw the circle
        draw.ellipse((top_right_x, top_right_y, top_right_x + size, top_right_y + size), fill=color, outline=(0, 0, 0), width=2)

        del draw
        return image
    

    def is_pressed(self) -> bool:
        return self.press_state
    
    def add_border(self, image: Image.Image) -> Image.Image:
        image = image.copy()
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((-1, -1, image.width, image.height), fill=None, outline=(255, 105, 0), width=8, radius=8)

        return image

    def shrink_image(self, image: Image.Image, factor: float = 0.7) -> Image.Image:
        image = image.copy()
        width = int(image.width * factor)
        height = int(image.height * factor)
        image = image.resize((width, height))

        background = Image.new("RGBA", self.deck_controller.get_key_image_size(), (0, 0, 0, 0))

        if image.has_transparency_data:
            background.paste(image, (int((self.deck_controller.get_key_image_size()[0] - width) / 2), int((self.deck_controller.get_key_image_size()[1] - height) / 2)), image)
        else:
            background.paste(image, (int((self.deck_controller.get_key_image_size()[0] - width) / 2), int((self.deck_controller.get_key_image_size()[1] - height) / 2)))

        image.close()

        return background
    
    def load_from_input_dict(self, input_dict, update: bool = True, load_labels: bool = True, load_media: bool = True, load_background_color: bool = True):
        """
        Attention: Disabling load_media might result into disabling custom user assets
        """
        n_states = len(input_dict.get("states", {}))
        self.create_n_states(max(1, n_states))

        old_state_index = self.state

        self.state = 0

        #TODO: Reset states
        for state in input_dict.get("states", {}):
            state: ControllerKeyState = self.states.get(int(state))
            if state is None:
                continue

            state_dict = input_dict["states"][str(state.state)]

            ## Load media - why here? so that it doesn't overwrite the images chosen by the actions
            if load_media:
                state.key_image = None
                state.key_video = None
            
            if load_labels:
                state.label_manager.clear_labels()

            # Reset action layout
            layout = ImageLayout()
            state.layout_manager.set_action_layout(layout, update=False)

            state.own_actions_update() # Why not threaded? Because this would mean that some image changing calls might get executed after the next lines which blocks custom assets

            ## Load labels
            if load_labels:
                for label in state_dict.get("labels", []):
                    key_label = KeyLabel(
                        controller_input=self,
                        text=state_dict["labels"][label].get("text"),
                        font_size=state_dict["labels"][label].get("font-size"),
                        font_name=state_dict["labels"][label].get("font-family"),
                        font_weight=state_dict["labels"][label].get("font-weight"),
                        style=state_dict["labels"][label].get("style"),
                        color=state_dict["labels"][label].get("color"),
                        outline_width=state_dict["labels"][label].get("outline_width"),
                        outline_color=state_dict["labels"][label].get("outline_color"),
                        alignment=state_dict["labels"][label].get("alignment")
                    )
                    # self.add_label(key_label, position=label, update=False)
                    state.label_manager.set_page_label(label, key_label, update=False)

            ## Load media
            if load_media:
                path = state_dict.get("media", {}).get("path", None)
                if path not in ["", None]:
                    if is_image(path):
                        with Image.open(path) as image:
                            state.set_image(InputImage(
                                controller_input=self,
                                image=image.copy(),
                                path=path,
                            ), update=False)
                            
                    elif is_svg(path):
                        img = svg_to_pil(path, 192)
                        state.set_image(InputImage(
                            controller_input=self,
                            image=img
                        ), update=False)

                    elif is_video(path):
                        if os.path.splitext(path)[1].lower() == ".gif":
                            state.set_video(KeyGIF(
                                controller_key=self,
                                gif_path=path,
                                loop=state_dict.get("media", {}).get("loop", True),
                                fps=state_dict.get("media", {}).get("fps", 30)
                            )) # GIFs always update
                        else:
                            state.set_video(InputVideo(
                                controller_input=self,
                                video_path=path,
                                loop = state_dict.get("media", {}).get("loop", True),
                                fps = state_dict.get("media", {}).get("fps", 30),
                            )) # Videos always update

                layout = ImageLayout(
                    fill_mode=state_dict.get("media", {}).get("fill-mode"),
                    size=state_dict.get("media", {}).get("size"),
                    valign=state_dict.get("media", {}).get("valign"),
                    halign=state_dict.get("media", {}).get("halign"),
                )
                state.layout_manager.set_page_layout(layout, update=False)

            elif len(state.get_own_actions()) > 1 and False: # Disabled for now - we might reuse it later
                if state_dict.get("image-control-action") is None:
                    with Image.open(os.path.join("Assets", "images", "multi_action.png")) as image:
                        self.set_key_image(InputImage(
                            controller_input=self,
                            image=image.copy(),
                        ), update=False)
            
            elif len(state.get_own_actions()) == 1:
                if state_dict.get("image-control-action") is None:
                    self.set_key_image(None, update=False)
                # action = self.get_own_actions()[0]
                # if action.has_image_control()

            if load_background_color:
                state.background_manager.set_page_color(state_dict.get("background", {}).get("color"), update=False)

        if update:
            self.set_state(old_state_index)
            self.update()

    def set_state(self, state: int, update_sidebar: bool = True, allow_reload: bool = False) -> None:
        old_state = self.state
        if state == old_state and not allow_reload:
            return
        super().set_state(state, False, allow_reload)
        if update_sidebar:
            self.reload_sidebar()

    def set_ui_key_image(self, image: Image.Image) -> None:
        if image is None:
            return
        
        x, y = ControllerKey.Index_To_Coords(self.deck_controller.deck, self.index)

        if self.deck_controller.get_own_key_grid() is None or not gl.app.main_win.get_mapped():
            # Mark dirty only (P5.4) -- KeyGrid.load_from_changes
            # recomposites a fresh image on map instead of replaying `image`.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True
        else:
            try:
                GLib.idle_add(self.deck_controller.get_own_key_grid().buttons[x][y].set_image, image)
            except:
                print(f"Failed to set ui key image for {self.identifier}")
        
    def get_own_ui_key(self) -> KeyButton:
        x, y = ControllerKey.Index_To_Coords(self.deck_controller.deck, self.index)
        buttons = self.deck_controller.get_own_key_grid().buttons # The ui key coords are in reverse order
        return buttons[x][y]
    
    def get_image_size(self) -> tuple[int, int]:
        return self.deck_controller.get_key_image_size()

class ControllerTouchScreen(ControllerInput):
    def __init__(self, deck_controller: DeckController, ident: InputIdentifier):
        super().__init__(deck_controller, ControllerTouchScreenState, ident)

        self.enable_states = False

    @staticmethod
    def Available_Identifiers(deck):
        if deck.is_touch():
            return ["sd-plus"]
        return []

    def update(self) -> None:
        page = self.deck_controller.active_page  # capture at render start (see ControllerKey.update)
        config_gen = self.config_gen
        image = self.get_current_image()

        # Quick hash check - skip expensive encode+enqueue only if the image matches
        # BOTH the last presented hash (_last_img_hash, set in the task's run())
        # and the last enqueued hash: either alone can be stale (dropped paint /
        # in-flight revert) and would wrongly skip the correcting repaint. Mirrors
        # ControllerKey.update's dual-hash guard (plan §3) -- saves redundant
        # 800x100 JPEG writes on unchanged composites.
        img_hash = hash(image.tobytes())
        if (img_hash == getattr(self, '_last_img_hash', None)
                and img_hash == getattr(self, '_last_enqueued_hash', None)):
            image.close()
            return

        # Finish device work with `image` before handing it to the UI mirror, so
        # the media thread isn't reading it while GTK copies it.
        # Touchscreen only supports JPEG, so composite RGBA onto black.
        if image.mode == "RGBA":
            device_image = Image.new("RGB", image.size, (0, 0, 0))
            device_image.paste(image, (0, 0), image)
        else:
            device_image = image

        native_image = encode_native_touchscreen(self.deck_controller.deck, device_image)
        self._last_enqueued_hash = img_hash
        self.deck_controller.media_player.add_touchscreen_task(native_image, page=page, config_gen=config_gen, controller_touchscreen=self, img_hash=img_hash)

        self.set_ui_image(image)

    def generate_empty_image(self) -> Image.Image:
        return Image.new("RGBA", self.get_screen_dimensions(), (0, 0, 0, 0))
    
    def get_dial_image_area(self, identifier: Input.Dial) -> tuple[int, int, int, int]:
        width, height = self.get_screen_dimensions()

        n_dials = len(self.deck_controller.inputs[Input.Dial])
        dial_index = identifier.index

        start_x = int((dial_index / n_dials) * width)
        start_y = 0
        end_x = int(((dial_index + 1) / n_dials) * width)
        end_y = height

        return start_x, start_y, end_x, end_y
    
    def get_dial_image_area_size(self) -> tuple[int, int]:
        width, height = self.get_screen_dimensions()

        n_dials = len(self.deck_controller.inputs[Input.Dial])

        return int(width / n_dials), height
    
    def get_empty_dial_image(self) -> Image.Image:
        screen_width, screen_height = self.get_screen_dimensions()

        n_dials = len(self.deck_controller.inputs[Input.Dial])

        return Image.new("RGBA", (screen_width // n_dials, screen_height), (0, 0, 0, 0))

    def set_ui_image(self, image: Image.Image) -> None:
        if recursive_hasattr(self, "deck_controller.own_deck_stack_child.page_settings.deck_config.screenbar.image") and gl.app.main_win.get_mapped():
            # Throttle the on-screen preview to a few FPS; the physical
            # touchscreen still gets every frame.
            now = time.time()
            if now - getattr(self, "_last_ui_image_time", 0) < 0.1:
                # Within the throttle window: keep the latest frame and flush it
                # after the window, so the final frame (when a scroll stops) isn't lost.
                self._pending_ui_image = image
                if not getattr(self, "_ui_flush_scheduled", False):
                    self._ui_flush_scheduled = True
                    GLib.timeout_add(100, self._flush_pending_ui_image)
                return
            self._last_ui_image_time = now
            self._pending_ui_image = None
            screenbar = self.deck_controller.own_deck_stack_child.page_settings.deck_config.screenbar
            GLib.idle_add(screenbar.image.set_image, image)
        else:
            # Mark dirty only (P5.4) -- ScreenBar.load_from_changes
            # recomposites a fresh image on map instead of replaying `image`.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True

    def _flush_pending_ui_image(self) -> bool:
        # Runs on the GTK main loop; pushes the last throttled frame so the preview
        # doesn't freeze mid-scroll. Skipped if a fresh frame already superseded it.
        self._ui_flush_scheduled = False
        image = getattr(self, "_pending_ui_image", None)
        self._pending_ui_image = None
        if image is None:
            return False
        if recursive_hasattr(self, "deck_controller.own_deck_stack_child.page_settings.deck_config.screenbar.image") and gl.app.main_win.get_mapped():
            self._last_ui_image_time = time.time()
            screenbar = self.deck_controller.own_deck_stack_child.page_settings.deck_config.screenbar
            screenbar.image.set_image(image)
        else:
            # Window unmapped mid-throttle: mark dirty (P5.4) instead of
            # keeping this frame -- the remap restore recomposites fresh.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True
        return False

    def get_current_image(self) -> Image.Image:
        active_state = self.get_active_state()
        return active_state.get_current_image()

    def event_callback(self, event_type, value):
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if event_type in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG, TouchscreenEventType.DRAG):
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            return
        
        active_state = self.get_active_state()
        if event_type == TouchscreenEventType.DRAG:
            # Check if from left to right or the other way
            if value['x'] > value['x_out']:
                active_state.own_actions_event_callback_threaded(
                    Input.Touchscreen.Events.DRAG_LEFT
                )
            else:
                active_state.own_actions_event_callback_threaded(
                    Input.Touchscreen.Events.DRAG_RIGHT
                )


        #TODO get matching actions from the dials
        elif event_type in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG):
            dial = self.get_dial_for_touch_x(value['x'])
            if dial is not None:
                dial_active_state = dial.get_active_state()
                if dial_active_state is not None:

                    event = Input.Dial.Events.SHORT_TOUCH_PRESS
                    if event_type == TouchscreenEventType.LONG:
                        event = Input.Dial.Events.LONG_TOUCH_PRESS

                    dial_active_state.own_actions_event_callback_threaded(
                        event,
                        data={"x": value['x'], "y": value['y']},
                        show_notifications=True
                    )

    def get_dial_for_touch_x(self, touch_x: float) -> "ControllerDial":
        screen_width = self.deck_controller.get_touchscreen_image_size()[0]
        n_dials = len(self.deck_controller.inputs[Input.Dial])
        dial_index = int((touch_x / screen_width) * n_dials)

        return self.deck_controller.get_input(Input.Dial(str(dial_index)))
    
    def get_screen_dimensions(self) -> tuple[int, int]:
        return self.deck_controller.get_touchscreen_image_size()

class ControllerDial(ControllerInput):
    def __init__(self, deck_controller: DeckController, ident: InputIdentifier):
        super().__init__(deck_controller, ControllerDialState, ident)

        self.down_start_time: float = None

    def on_hold_timer_end(self):
        state = self.get_active_state()
        state.own_actions_event_callback_threaded(
            event=Input.Dial.Events.HOLD_START
        )

    def get_touch_screen(self) -> ControllerTouchScreen:
        return self.deck_controller.get_input(Input.Touchscreen("sd-plus"))

    @staticmethod
    def Available_Identifiers(deck):
        return map(str, range(deck.dial_count()))

    def event_callback(self, event_type, value):
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if event_type == DialEventType.TURN:
            self.deck_controller.screen_saver.on_key_change()
        if event_type == DialEventType.PUSH and value:
            # Only on push, not on hold to allow actions to enable the screensaver without directly causing it to wake up again
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            return
        
        active_state = self.get_active_state()
        if event_type == DialEventType.PUSH:
            if value:
                self.down_start_time = time.time()
                self.start_hold_timer()
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.DOWN,
                    show_notifications=True
                )
            elif self.down_start_time is not None:
                self.stop_hold_timer()
                if time.time() >= self.down_start_time + self.deck_controller.hold_time:
                    active_state.own_actions_event_callback_threaded(
                        event=Input.Dial.Events.HOLD_STOP
                    )
                else:
                    active_state.own_actions_event_callback_threaded(
                        event=Input.Dial.Events.SHORT_UP
                    )
                self.down_start_time = None
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.UP
                )
        
        elif event_type == DialEventType.TURN:
            # value is the HID report's signed detent count — fast rotation
            # coalesces several detents into one report, so forward the
            # magnitude instead of collapsing it to a single event.
            if value < 0:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.TURN_CCW,
                    data={"ticks": -value}
                )
            else:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.TURN_CW,
                    data={"ticks": value}
                )

    def load_from_input_dict(self, page_dict, update: bool = True):
        n_states = len(page_dict.get("states", {}))
        self.create_n_states(max(1, n_states))

        old_state_index = self.state

        self.state = 0

        for state in page_dict.get("states", {}):
            state: ControllerDialState = self.states.get(int(state))
            if state is None:
                continue

            state_dict = page_dict["states"][str(state.state)]

            # Reset action layout
            layout = ImageLayout()
            state.layout_manager.set_action_layout(layout, update=False)

            state.own_actions_update() # Why not threaded? Because this would mean that some image changing calls might get executed after the next lines which blocks custom assets

            ## Load labels
            for label in state_dict.get("labels", []):
                key_label = KeyLabel(
                    controller_input=self,
                    text=state_dict["labels"][label].get("text"),
                    font_size=state_dict["labels"][label].get("font-size"),
                    font_name=state_dict["labels"][label].get("font-family"),
                    font_weight=state_dict["labels"][label].get("font-weight"),
                    style=state_dict["labels"][label].get("style"),
                    color=state_dict["labels"][label].get("color"),
                    alignment=state_dict["labels"][label].get("alignment"),
                )
                state.label_manager.set_page_label(label, key_label, update=False)

            ## Load media
            path = state_dict.get("media", {}).get("path")
            if path not in ["", None]:
                if is_image(path):
                    image = InputImage(
                        controller_input=self,
                        image=Image.open(path),
                        path=path,
                    )
                    state.set_image(image, update=False)
                elif is_svg(path):
                    img = svg_to_pil(path, 192)
                    state.set_image(InputImage(
                        controller_input=self,
                        image=img
                    ), update=False)

                elif is_video(path):
                    if os.path.splitext(path)[1].lower() == ".gif":
                        raise NotImplementedError("TODO") #TODO
                        state.set_video(KeyGIF(
                            controller_key=self,
                            gif_path=path,
                            loop=state_dict.get("media", {}).get("loop", True),
                            fps=state_dict.get("media", {}).get("fps", 30)
                        )) # GIFs always update
                    else:
                        state.set_video(InputVideo(
                            controller_input=self,
                            video_path=path,
                            loop = state_dict.get("media", {}).get("loop", True),
                            fps = state_dict.get("media", {}).get("fps", 30),
                        )) # Videos always update

            layout = ImageLayout(
                fill_mode=state_dict.get("media", {}).get("fill-mode"),
                size=state_dict.get("media", {}).get("size"),
                valign=state_dict.get("media", {}).get("valign"),
                halign=state_dict.get("media", {}).get("halign"),
            )
            state.layout_manager.set_page_layout(layout, update=False)

            state.background_manager.set_page_color(state_dict.get("background", {}).get("color", [0, 0, 0, 0]), update=False)

        if update:
            self.set_state(old_state_index)
            self.update()

    def update(self):
        if self.deck_controller.deck.is_touch():
            self.get_touch_screen().update()

    def get_active_state(self) -> "ControllerDialState":
        return super().get_active_state()

    def on_media_player_tick(self) -> bool:
        # Advance the animation clock and report whether a redraw is needed;
        # the caller renders the shared touchscreen once per frame.
        self.media_ticks += 1

        state = self.get_active_state()
        if state is None:
            return False
        return state.video is not None or state.label_manager.get_has_scroll_labels()

    def get_image_size(self) -> tuple[int, int]:
        if self.deck_controller.deck.is_touch():
            return self.get_touch_screen().get_dial_image_area_size()
        return (0, 0)
    

class ControllerTouchScreenState(ControllerInputState):
    def __init__(self, controller_touch: "ControllerTouchScreen", state: int):
        super().__init__(controller_touch, state)

        self.controller_touch = controller_touch

    def set_current_image(self, image: Image.Image):
        self.current_image = image

        self.update()

    def get_current_image(self) -> Image.Image:
        screen_width, screen_height = self.controller_touch.get_screen_dimensions()
        
        # Start with background image if set
        background: Image.Image = None
        active_page = self.controller_touch.deck_controller.active_page
        background_image_path = active_page.get_background_image(
            identifier=self.controller_touch.identifier, 
            state=self.state
        )
        
        if background_image_path and os.path.isfile(background_image_path):
            try:
                with Image.open(background_image_path) as img:
                    # Resize to exact touchscreen dimensions (KISS - exact dimensions)
                    background = ImageOps.fit(img, (screen_width, screen_height), Image.Resampling.LANCZOS).convert("RGBA")
            except Exception as e:
                log.error(f"Error loading background image: {e}")
                background = None

        # Deck background extended onto the strip is the bottom-most layer; an
        # explicit per-touchscreen background image takes precedence over it.
        if background is None:
            deck_background = self.controller_touch.deck_controller.background.get_touchscreen_image()
            if deck_background is not None:
                # convert() copies (the slice is shared and dial images get
                # pasted onto the returned image in place) and normalizes
                # video-frame slices (RGB) for the alpha_composite below.
                background = deck_background.convert("RGBA")

        # Get background color from touchscreen state's background_manager
        background_color = self.background_manager.get_composed_color()
        
        # If no background image, start with empty or colored background
        if background is None:
            # If background color has transparency (alpha < 255), start with transparent
            if background_color[-1] < 255:
                background = self.controller_touch.generate_empty_image()
            
            # If background color is set (alpha > 0), create colored background
            if background_color[-1] > 0:
                background_color_img = Image.new("RGBA", (screen_width, screen_height), color=tuple(background_color))
                
                if background is None:
                    # Use the color as the only background - happens if background color alpha is 255
                    background = background_color_img
                else:
                    # Paste color on top of transparent background
                    background.paste(background_color_img, (0, 0), background_color_img)
            
            # If no background color was set, use empty image
            if background is None:
                background = self.controller_touch.generate_empty_image()
        else:
            # Background image exists - apply color overlay if set
            if background_color[-1] > 0:
                background_color_img = Image.new("RGBA", (screen_width, screen_height), color=tuple(background_color))
                # Blend color over image
                background = Image.alpha_composite(background, background_color_img)

        # Paste dial images on top of the background
        for dial in self.controller_touch.deck_controller.inputs[Input.Dial]:
            state = dial.get_active_state()
            image_area = self.controller_touch.get_dial_image_area(dial.identifier)
            dial_image = state.get_rendered_touch_image()

            background.paste(dial_image, image_area, dial_image)

        return background


    def update(self):
        if self.controller_touch.get_active_state() is self:
            self.controller_touch.update()

    

    def set_dial_image(self, identifier: Input.Dial, image: Image.Image, update: bool = True):
        return
        assert isinstance(identifier, Input.Dial)

        area = self.get_dial_image_area(identifier)
        width, height = area[2] - area[0], area[3] - area[1]

        # Clear underground
        empty_dial = self.get_empty_dial_image()
        # Use alpha mask if empty_dial has transparency to prevent edge artifacts
        if empty_dial.has_transparency_data:
            self.current_image.paste(empty_dial, area, empty_dial)
        else:
            self.current_image.paste(empty_dial, area)

        # Contain image into the area
        image = ImageOps.contain(image, (width, height), Image.Resampling.HAMMING)

        # Get x, y for centered position
        x = area[0] + int((width - image.width) / 2)
        y = area[1] + int((height - image.height) / 2)

        self.current_image.paste(image, (x, y), image)

        self.current_image.save("sd.png")

        if update:
            self.update()


    def clear(self):
        self.set_current_image(self.controller_touch.generate_empty_image())

    def close_resources(self) -> None:
        # current_image is only ever set via set_current_image(); a
        # touchscreen state closed before its first render (e.g. a
        # screensaver-stash sweep of a page that never painted, or a fresh
        # ControllerDialState-style state right after create_n_states())
        # never gets one, and dereferencing it unconditionally raised
        # AttributeError (design doc bug 20). getattr + None-guard makes
        # this safe to call any number of times.
        current_image = getattr(self, "current_image", None)
        if current_image is not None:
            current_image.close()
        self.current_image = None

class ControllerDialState(ControllerInputState):
    def __init__(self, dial: "ControllerDial", state: int):
        self.dial = dial

        self.image: InputImage = None
        self.video: InputVideo = None

        self.touch_image: Image.Image = None

        super().__init__(dial, state)

    def set_image(self, image: "InputImage", update: bool = True) -> None:
        if self.image is not None:
            self.image.close()

        self.image = image

        if update:
            self.update()

    def set_video(self, video: "InputVideo") -> None:
        if self.video is not None:
            self.video.close()

        self.video = video

    def close_resources(self) -> None:
        # The base class default is a no-op `pass` -- without this override
        # (missing until this fix), a dial's InputImage/InputVideo were never
        # released by ControllerInput.close_resources(), unlike its key
        # sibling (ControllerKeyState.close_resources already does this).
        if self.image is not None:
            self.image.close()
            self.image = None
        if self.video is not None:
            self.video.close()
            self.video = None


    def get_rendered_touch_image(self) -> Image.Image:
        touch_screen = self.dial.get_touch_screen()

        background: Image.Image = None

        background_color = self.background_manager.get_composed_color()

        if background_color[-1] < 255:
            background = touch_screen.get_empty_dial_image()
        if background_color[-1] > 0:
            background_color_img = Image.new("RGBA", self.dial.get_image_size(), color=tuple(background_color))

            if background is None:
                # Use the color as the only background - happens if background color alpha is 255
                background = background_color_img
            else:
                background.paste(background_color_img, (0, 0), background_color_img)
        

        image = None
        if self.video is not None:
            image = self.video.get_next_frame()
        elif self.image is not None:
            image = self.image.image

        # rotation = self.deck_controller.get_deck_settings().get("rotation", {}).get("value", 0)

        image = self.layout_manager.add_image_to_background(image, background)
        image = self.label_manager.add_labels_to_image(image)

        return image

class ControllerKeyState(ControllerInputState):
    def __init__(self, controller_key: "ControllerKey", state: int):
        super().__init__(controller_key, state)

        self.key_image: InputImage = None
        self.key_video: InputVideo = None

    def close_resources(self) -> None:
        if self.key_image is not None:
            self.key_image.close()
            self.key_image = None
        if self.key_video is not None:
            self.key_video.close()
            self.key_video = None

    def set_image(self, key_image: "InputImage", update: bool = True) -> None:
        if self.key_image is not None:
            self.key_image.close()
        if self.key_video is not None:
            # Design doc bug 18: dropping key_video here without closing it
            # leaked its tile-cache registry attachment/VideoCapture on every
            # image<-video switch (InputVideo.close() is now real -- see
            # KeyVideo.py).
            self.key_video.close()

        self.key_image = key_image
        self.key_video = None

        if update:
            self.update()

    def set_video(self, key_video: "InputVideo") -> None:
        if self.key_video is not None:
            # Design doc bug 18: the previous video was never closed before
            # being overwritten (InputVideo.close() is now real).
            self.key_video.close()
        self.key_video = key_video
        if self.key_image is not None:
            self.key_image.close()
        self.key_image = None

    def clear(self):
        if self.key_video is not None:
            # Design doc bug 18: clear() dropped key_video without closing
            # it (InputVideo.close() is now real).
            self.key_video.close()
        self.key_image = None
        self.key_video = None
        self.label_manager.clear_labels()
        self.layout_manager.clear()
        self.background_manager.set_page_color(None)