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

The media writer runs one MediaPlayerThread per deck, plus the units it
executes. That thread is the sole writer to its device. Every key image,
touchscreen image, brightness change and blank reaches the hardware from
inside its loop, so this module decides the ordering between them. A paint
carries the page and generation it was rendered for, and the write boundary
judges it stale. A control message, for brightness, clear, clear-and-close
or a stashed-input release, has no page affinity, drains first on every
wake, and always executes, FIFO.

This module also holds the native JPEG encoders that every paint funnels
through, and the FIFO transport lock that stops a write burst from starving
the device's HID read poll. It imports nothing from its sibling modules in
the deck_controller package.
"""
import collections
import io
import itertools
import os
import statistics
import threading
import time
from dataclasses import dataclass

from PIL import Image
from StreamDeck.Devices import StreamDeck
from loguru import logger as log

from src.backend.DeckManagement.fair_lock import FairLock
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.PageManagement.Page import Page
from src.backend import ui_port

import globals as gl

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import (
        ControllerDial,
        ControllerKey,
        ControllerTouchScreen,
    )


# JPEG quality every key native is encoded at. It is part of the native
# tile cache key, so bytes encoded at one quality must never be served for
# another.
KEY_ENCODE_QUALITY = 90


def encode_native_key(deck, image: "Image.Image", quality: int = KEY_ENCODE_QUALITY) -> bytes:
    """PILHelper.to_native_key_format with a tunable JPEG quality, where the
    library hardcodes q100. A smaller JPEG means fewer serial USB HID writes
    per key."""
    fmt = deck.key_image_format()
    if image.size != fmt["size"]:
        image.thumbnail(fmt["size"])
    if fmt["rotation"]:
        image = image.rotate(fmt["rotation"])
    if fmt["flip"][0]:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if fmt["flip"][1]:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        save_kwargs = {"quality": quality}
        if fmt["format"] == "JPEG":
            # Below quality 95 Pillow switches to 4:2:0 chroma subsampling,
            # which halves the color resolution on both axes and gives a
            # measured ~4% average desaturation plus chroma smear on busy
            # 120px tiles. Force 4:4:4. It keeps the q90 encode speed and
            # costs ~17% more bytes.
            save_kwargs["subsampling"] = 0
        image.save(buf, fmt["format"], **save_kwargs)
        return buf.getvalue()


def encode_native_touchscreen(deck, image: "Image.Image", quality: int = 90) -> bytes:
    """PILHelper.to_native_touchscreen_format with a tunable JPEG quality,
    and with no mutation of the caller's image. The library hardcodes q100,
    and its _to_native_format calls image.thumbnail() in place when it
    resizes, which corrupts the caller's copy. The touchscreen strip is the
    largest single USB write on the deck, so a smaller JPEG here buys back
    time under the device write mutex, which is dial-latency margin.
    ControllerTouchScreen.update reuses the same image object afterward for
    the UI mirror, so any resize here must work on a copy."""
    fmt = deck.touchscreen_image_format()
    if image.size != fmt["size"]:
        image = image.copy()
        image.thumbnail(fmt["size"])
    if fmt["rotation"]:
        image = image.rotate(fmt["rotation"])
    if fmt["flip"][0]:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if fmt["flip"][1]:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    with io.BytesIO() as buf:
        save_kwargs = {"quality": quality}
        if fmt["format"] == "JPEG":
            # Force 4:4:4 as encode_native_key does. Below q95 Pillow
            # switches to 4:2:0 chroma subsampling, which visibly desaturates
            # the strip's icons and text.
            save_kwargs["subsampling"] = 0
        image.save(buf, fmt["format"], **save_kwargs)
        return buf.getvalue()


@dataclass
class MediaPlayerTask:
    deck_controller: "DeckController"
    # None when the deck has no active page, at boot or during teardown. The
    # write boundary only identity-compares it against active_page and never
    # dereferences it, so a page-less paint is judged stale, not crashed on.
    page: Page | None
    _callable: Callable[..., Any]
    args: tuple
    kwargs: dict

    def run(self):
        self._callable(*self.args, **self.kwargs)

@dataclass
class MediaPlayerSetTouchscreenImageTask:
    deck_controller: "DeckController"
    # None when the deck has no active page, at boot or during teardown. The
    # write boundary only identity-compares it against active_page and never
    # dereferences it, so a page-less paint is judged stale, not crashed on.
    page: Page | None
    native_image: bytes
    config_gen: int | None = None  # generation of the content rendered; dropped at present if stale
    submit_seq: int | None = None  # writer's monotonic submit-seq stamp; None when unstamped
    controller_touchscreen: "ControllerTouchScreen | None" = None  # stamped once this paint is presented
    img_hash: int | None = None  # hash of the presented image, recorded in run()

    def run(self):
        if not self.deck_controller.deck.is_touch():
            return
        try:
            touchscreen_size = self.deck_controller.get_touchscreen_image_size()
            self.deck_controller.deck.set_touchscreen_image(self.native_image, x_pos=0, y_pos=0, width=touchscreen_size[0], height=touchscreen_size[1])  # maybe avoid merging the dial images before every apply
            # Record the presented image's hash here, not at render time. A
            # paint dropped at the write boundary must not advance the hash,
            # or the correcting render hash-skips and the touchscreen bleeds
            # forever. MediaPlayerSetImageTask does the same.
            if self.controller_touchscreen is not None:
                self.controller_touchscreen._last_img_hash = self.img_hash
            self.native_image = None
            del self.native_image
            self.deck_controller._on_write_result(True)
        except StreamDeck.TransportError as e:
            log.error(f"Failed to set deck touchscreen image. Error: {e}")
            # The error policy is attempt and swallow. Only a USB disconnect
            # event removes a controller, never a write-failure count.
            self.deck_controller._on_write_result(False)

@dataclass
class MediaPlayerSetImageTask:
    deck_controller: "DeckController"
    # None when the deck has no active page, at boot or during teardown. The
    # write boundary only identity-compares it against active_page and never
    # dereferences it, so a page-less paint is judged stale, not crashed on.
    page: Page | None
    key_index: int
    native_image: bytes
    config_gen: int | None = None  # generation of the content rendered; dropped at present if stale
    controller_key: "ControllerKey | None" = None  # stamped once this paint is presented
    img_hash: int | None = None  # hash of the presented image, recorded in run()
    submit_seq: int | None = None  # writer's monotonic submit-seq stamp; None when unstamped

    def run(self):
        try:
            if media_prof:
                _t0 = time.perf_counter()
            self.deck_controller.deck.set_key_image(self.key_index, self.native_image)
            if media_prof:
                media_prof.add("usb_write", time.perf_counter() - _t0)
            # Record the presented image's hash here, not at render time. A
            # paint dropped at the write boundary must not advance the hash,
            # or the correcting render hash-skips and the key bleeds forever.
            if self.controller_key is not None:
                self.controller_key._last_img_hash = self.img_hash
            self.native_image = None
            del self.native_image
            self.deck_controller._on_write_result(True)
        except StreamDeck.TransportError as e:
            log.error(f"Failed to set deck key image. Error: {e}")
            # The error policy is attempt and swallow. Only a USB disconnect
            # event removes a controller, never a write-failure count.
            self.deck_controller._on_write_result(False)


@dataclass
class SetBrightnessMsg:
    """Control message that sets the device brightness. It executes on the
    media thread, the sole writer, through MediaPlayerThread.control_q. See
    docs/presenter-migration-plan.md."""
    value: float


@dataclass
class ClearMsg:
    """Control message that blanks the deck. seq holds the submitting
    thread's monotonic submit-sequence counter value at submission time, from
    MediaPlayerThread.next_submit_seq(). Executing this wipes only image and
    touchscreen tasks stamped with a lower submit_seq, so a frame submitted
    after this Clear was requested survives and paints afterward, which
    preserves the caller's clear-then-paint order.

    expects_repaint records the submitter's intent, which the writer cannot
    infer once the message is on the queue. True means the submitter is about
    to repaint this deck and these blanks are a transition, as at screensaver
    entry and exit. False means the blank deck is the result, as in
    load_page's page-is-None branch. Only the first may be recovered from
    when it executes late; see _exec_clear. It is stamped at submission and
    not read off live state at execution, because live state has moved on by
    then."""
    seq: int
    expects_repaint: bool = False


@dataclass
class ClearAndCloseMsg:
    """Terminal control message. It wipes the pending image and touchscreen
    tasks, writes blanks, closes the device where it can, and stops the media
    thread's loop."""
    pass


@dataclass
class ReleaseStashedInputsMsg:
    """Control message that closes every stashed input's media resources,
    then empties the dict in place. ScreenSaver.show() uses it to release the
    previous page's input set shortly after it swaps the set out, instead of
    pinning it for the whole screensaver duration.

    This is a control message and not a generic add_task(). An add_task task
    is dropped unrun when task.page is not active_page by the time the batch
    executes, which is correct for a stale render and wrong here. A
    hide()-triggered load_page() that changes active_page before this drains
    must not skip the release. A control message has no page affinity and
    always executes, FIFO, like ClearMsg and SetBrightnessMsg."""
    stashed_inputs: dict


def _env_float(name: str, default: float) -> float:
    """Read a float tuning knob from the environment, and fall back to
    default on a malformed value. A typo in an env var must degrade to the
    built-in default with a warning. It must never raise out of
    MediaPlayerThread init, where DeckManager would report a failed deck and
    skip the whole device."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"Ignoring malformed {name}={raw!r}; using the default {default}")
        return default


def _install_fair_transport_lock(deck) -> bool:
    """Swap the transport's per-device mutex for a FIFO one.

    The library guards every read, write and feature report of a device with
    deck.device.mutex, a stock threading.Lock. Unfair ordering there lets a
    write burst out-race the HID read poll, which is what dial input
    starvation is. The swap is one attribute assignment on an object this
    process owns. The library is neither vendored nor patched, so an upstream
    rename degrades to the unfair lock with the env knobs still available,
    and every guard below returns False instead of raising.

    This must run before deck.open(), which starts the reader thread. Before
    that, no thread can be inside the old lock, so the swap cannot leave two
    threads in hidapi at once. The reopen path on resume from suspend reuses
    the same Device instance, so the FIFO lock survives a suspend cycle with
    no reinstall. Returns whether the FIFO lock is installed."""
    device = getattr(deck, "device", None)
    if device is None:
        # A FakeDeck or a RemoteDeck has no HID transport to order.
        log.info(f"Deck {type(deck).__name__} has no transport device; "
                 f"keeping the stock lock")
        return False

    mutex = getattr(device, "mutex", None)
    if mutex is None:
        log.warning(f"Transport {type(device).__name__} has no mutex attribute; "
                    f"skipping the fair transport lock (library drift?)")
        return False

    if isinstance(mutex, FairLock):
        return True

    locked = getattr(mutex, "locked", None)
    if callable(locked) and locked():
        # Only reachable if this call ever moves after open(). A swap of a
        # held lock lets the holder and a new acquirer into hidapi together.
        log.warning("Transport mutex is held; skipping the fair transport lock")
        return False

    device.mutex = FairLock()
    log.info(f"Installed the fair (FIFO) transport lock on "
             f"{type(device).__name__}")
    return True


class MediaPlayerThread(threading.Thread):
    # An image batch at or above this size is a bulk repaint, from a video
    # frame or a full-page paint, and gets inter-write yields. A smaller
    # batch is interactive, from press feedback or a plugin set_media, and
    # writes at once.
    BULK_BATCH_THRESHOLD = 4
    # Within a bulk batch, yield after every N writes. See the batch loop in
    # perform_media_player_tasks.
    YIELD_STRIDE = 3
    # Quiet render ticks the page-generation watch owes a new generation
    # before the quiescence gate re-engages. It is small because these run at
    # full FPS, so the window costs ~100ms of frames per page change made
    # while the user is away.
    GATE_SETTLE_TICKS = 3
    # Hard wall-clock bound on that window. The countdown above re-arms
    # whenever the task queues are non-empty, which means page-load work is
    # still landing. That predicate is also permanently true under any
    # producer at 10Hz or more, such as a plugin that loops set_media into
    # add_image_task, or the touchscreen latest-wins re-queue under a low
    # DECKARD_VIDEO_WRITE_HZ. Unbounded, such a producer pins the loop
    # un-gated at full FPS for the whole away window, and the logs say
    # nothing. So the window also closes on this deadline, whatever the
    # queues say. It is generous next to what it bounds, because a settle is
    # GATE_SETTLE_TICKS quiet ticks, about 100ms, plus the time the page-load
    # tasks take to drain. If it truncates a genuinely slow page load, the
    # cost is one away window of stale imagery on that page's transparent
    # keys, for one page instead of for the whole gate.
    GATE_WINDOW_MAX_S = 0.5

    def __init__(self, deck_controller: "DeckController"):
        # Suffix the thread name with the deck serial, so the journal's
        # thread_name attributes a writer to its deck when two decks run at
        # once. serial_number() is a cheap attribute read that never raises on
        # any real or fake deck.
        try:
            _serial = deck_controller.serial_number()
        except Exception:
            _serial = "unknown"
        super().__init__(name=f"MediaPlayerThread-{_serial}", daemon=True)
        self.deck_controller: DeckController = deck_controller
        self.FPS = 30 # Max refresh rate of the internal displays

        # Cap how often a background video repaints the device. The FIFO
        # transport lock keeps the HID read poll from starving, because the
        # reader waits at most for the chunk in flight, so this cap is a rate
        # alignment. The same gate drives the repaint decision at
        # now - _last_video_write < min_gap below, so it governs render cost
        # as well as write cost, and a render above the loop rate buys
        # nothing. Hence 30, not unlimited. Render cost on high-entropy
        # content, where tile dedup skips nothing at ~270 candidate writes per
        # second, belongs to the native tile cache and not to this knob. A
        # value of 0 disables the cap, and the env var is a field bisection
        # tool; 20 restores the older pacing.
        self._video_write_hz = _env_float("DECKARD_VIDEO_WRITE_HZ", 30.0)
        self._last_video_write = 0.0
        # The same budget caps every touchscreen write, at the write point in
        # perform_media_player_tasks. Dial-state videos and scrolling labels
        # otherwise rewrite the strip at loop FPS, which is the same
        # HID-starvation vector through a different content type.
        self._last_touch_write = 0.0

        # Inter-write yield inside a bulk batch, in seconds. See
        # perform_media_player_tasks. It is off by default, because the yield
        # only hands the reader a mutex slot and FIFO ordering grants that
        # anyway. The machinery stays so the env var restores the older
        # pacing, 1.5, in the field with no rebuild.
        self._inter_write_yield = _env_float("DECKARD_WRITE_YIELD_MS", 0.0) / 1000.0

        self.running = False
        self.media_ticks = 0
        # Ticks that skipped the animation section because the user is away.
        # The quiescence scenarios and the hardware driver assert on it, and
        # media_ticks minus gated_ticks is the number of ticks that rendered.
        self.gated_ticks = 0
        # Ticks the settle window rendered instead of gating. It sits next to
        # gated_ticks so an open render window is observable from outside the
        # loop. A window that never closes is this gate's worst failure mode,
        # and gated_ticks merely stops advancing, which also describes a user
        # who is not yet quiescent.
        self.gate_window_ticks = 0
        # Page-generation watch state; see _run_one_tick. It holds the
        # generation last observed while gated, the render ticks the settle
        # window still owes it, and the monotonic instant the window closes
        # whatever happens, from GATE_WINDOW_MAX_S.
        self._gated_generation: int | None = None
        self._gate_render_ticks = 0
        self._gate_window_deadline = 0.0

        self._stop = False

        self.tasks: list[MediaPlayerTask] = []
        self.image_tasks: dict[int, MediaPlayerSetImageTask] = {}
        self.touchscreen_task: MediaPlayerSetTouchscreenImageTask | None = None
        # Guards the single-slot task stores against a producer and consumer
        # interleave. The drain's read-then-null on touchscreen_task and the
        # Clear's get-then-del on image_tasks can both discard a task assigned
        # in between, and the producer already stamped _last_enqueued_hash, so
        # static content stays stale forever with no tick to re-enqueue it.
        # The critical sections are a few instructions.
        self._slot_lock = threading.Lock()
        self._wake_event = threading.Event()

        # Control queue. append and popleft are GIL-atomic, so it needs no
        # extra lock. The loop drains it fully and first on every wake, ahead
        # of any animation tick or task work, so a brightness or clear op
        # never waits behind them.
        self.control_q: collections.deque = collections.deque()
        # Per-writer monotonic stamp counter. add_image_task and
        # add_touchscreen_task stamp a task with next(self._submit_seq) under
        # _slot_lock, atomically with the slot assignment. A stamp taken
        # before the lock lets racing producers assign out of seq order and
        # leaves a slot holding an older frame. A Clear captures the counter
        # at its own submission through next_submit_seq(), so it can tell
        # which queued frames predate it.
        self._submit_seq = itertools.count()
        # Highest submit_seq whose frame reached the device, rather than one
        # merely queued or dropped at the write boundary. It advances only
        # right after a task's own run() returns, so it means the content is
        # on the deck now. _exec_clear compares it against a Clear's seq to
        # tell the two Clear interleaves apart. Media thread only, like the
        # rest of the drain state.
        self._max_executed_seq: int = -1

        # Wall-clock gap detection. A gap much larger than the loop's own
        # wait interval means the process suspended on a system sleep and then
        # resumed. See check_resume_gap().
        self._last_iter_ts: float = time.time()

        self.fps: list[float] = []
        self.old_warning_state = False

        self.show_fps_warnings = gl.settings_manager.app().enable_fps_warnings

        # Loop-guard state. This thread is the sole writer for paints,
        # brightness, Clear and ClearAndClose, so its death freezes the deck
        # until a replug. The guard in run() keeps it alive, and these two
        # fields rate-limit its logging so a per-tick failure cannot storm
        # the sinks.
        self._last_tick_error_log: float = 0.0
        self._suppressed_tick_errors: int = 0

    def run(self):
        self.running = True

        # Guard the body. An uncaught exception here kills the sole writer and
        # freezes the deck. @log.catch on run() is wrong, because it logs once
        # and returns, so the thread dies anyway; the guard must sit inside
        # the while. The central threading.excepthook reports an escaping
        # death, and this prevents the death.
        try:
            while True:
                try:
                    if not self._run_one_tick():
                        break
                except Exception:
                    now = time.time()
                    if now - self._last_tick_error_log >= 5.0:
                        suffix = (f" ({self._suppressed_tick_errors} earlier repeats were suppressed)"
                                  if self._suppressed_tick_errors else "")
                        log.opt(exception=True).error(
                            f"media writer tick failed -- survived, continuing{suffix}")
                        self._last_tick_error_log = now
                        self._suppressed_tick_errors = 0
                    else:
                        self._suppressed_tick_errors += 1
                    # A tick that raised mid-batch already popped its task
                    # lists, because perform_media_player_tasks drains
                    # image_tasks and touchscreen_task before it runs them, so
                    # the failing frame's siblings are lost too. Without a
                    # scheduled recovery the unpainted keys keep their
                    # previous imagery forever. Arm the pending full repaint,
                    # the same recovery a failed device write uses; its 2s
                    # rate limit makes this safe against a deterministic
                    # per-tick failure.
                    self.deck_controller._schedule_full_repaint()
                    # A raising body never reaches the FPS wait below, so
                    # without this backoff a persistent failure spins at
                    # 100%. Use _wake_event and not sleep, so stop() still
                    # wakes the loop at once. Every producer sets that event
                    # too, so one wait() under a set_media storm returns at
                    # once and the retry rate tracks the producer rate
                    # instead of ~4Hz. Re-wait until the backoff elapses;
                    # only _stop cuts it short.
                    deadline = time.monotonic() + 0.25
                    while not self._stop:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._wake_event.wait(remaining)
                        self._wake_event.clear()
                    if self._stop:
                        break
        finally:
            # This sits in a finally, not at the loop's tail. The guard
            # catches Exception, so a BaseException such as SystemExit or
            # KeyboardInterrupt escapes it, and a death that left
            # running=True makes every later stop() burn its full join
            # timeout on a dead thread.
            self.running = False

    def _open_gate_window(self) -> None:
        """Open the gated render window again for GATE_SETTLE_TICKS quiet
        render ticks, and never more than GATE_WINDOW_MAX_S of wall clock
        however busy the task queues stay. Writer thread only."""
        self._gate_render_ticks = self.GATE_SETTLE_TICKS
        self._gate_window_deadline = time.monotonic() + self.GATE_WINDOW_MAX_S

    def _run_one_tick(self) -> bool:
        """One iteration of the writer loop. Returns False to stop."""
        start = time.time()

        # Drain the control queue fully and first on every wake, ahead of the
        # resume-gap check, the pending-repaint hook, any animation tick or
        # task work, and ahead of a pending stop. Two orderings matter.
        #
        # Drain before _stop, because close_all() submits a terminal
        # ClearAndCloseMsg and then calls stop() at once. A _stop check before
        # this drain, or at the bottom of the loop after a wake that raced
        # that flag set, strands the terminal message unprocessed.
        #
        # Drain before everything else, because the rest of this tick can
        # raise into the run() guard. Anything ahead of the drain lets a
        # persistently failing tick starve SetBrightnessMsg, ClearMsg and
        # ClearAndCloseMsg forever, so the deck never blanks or closes on
        # quit.
        if not self.drain_control_queue():
            return False
        if self._stop:
            return False

        self.check_resume_gap(start)
        repaint_fired = self.deck_controller._run_pending_repaint()

        # Quiescence gate. It runs strictly after the control-queue drain and
        # the _stop check above, because quit, clear and brightness must never
        # wait on quiescence. When it holds, this tick skips the whole
        # animation section below, with no background decode or composite, no
        # key, dial or touchscreen tick, and no scroll-label advance.
        # perform_media_player_tasks() still runs, so queued interactive
        # paints and control-adjacent work stay live and the deck stays
        # functional. Only the animation pauses.
        gated = self.deck_controller.animations_gated()
        force_render = False
        gate_window_open = False
        if gated:
            # Page-generation watch. update_all_inputs() leaves the device
            # paint of a non-opaque key to this loop whenever a background
            # video is set. It paints the dials and the fully-opaque keys and
            # pushes the rest to the UI preview only. A page load or a
            # background swap that lands while gated would then leave the
            # previous page's imagery on every transparent key for the whole
            # away window, and _run_pending_repaint() is no escape, because it
            # calls that same update_all_inputs(). So render un-gated across a
            # new generation, then re-gate.
            #
            # This is a settle window and not a single pass. load_page() bumps
            # the generation at its very top, and queues the work that builds
            # the new page's inputs and background onto this thread
            # afterwards. A single pass fired on the first tick that sees the
            # new generation renders the old page's content, correctly, and
            # then never again. So the window keeps rendering while the task
            # queues are non-empty, which means page-load work is still
            # landing, and counts down only over quiet ticks. The last of
            # those paints the settled new page. GATE_WINDOW_MAX_S bounds the
            # whole thing, because non-empty queues are a producer's permanent
            # state. This uses the same snapshot under _page_gen_lock as
            # perform_media_player_tasks.
            with self.deck_controller._page_gen_lock:
                current_gen = self.deck_controller._page_load_generation
            if current_gen != self._gated_generation:
                self._gated_generation = current_gen
                self._open_gate_window()
            if repaint_fired:
                # A full repaint that just fired, from a resume or from the
                # 2s retry after write failures, bumps no generation but runs
                # through the same update_all_inputs(), so it has the same
                # transparent-key blind spot on a video-background page. Open
                # the window for it too, or a machine that wakes from sleep
                # while the user is away leaves those keys showing whatever
                # survived the suspend.
                self._open_gate_window()
            if self._gate_render_ticks > 0:
                if time.monotonic() >= self._gate_window_deadline:
                    # Wall-clock stop from GATE_WINDOW_MAX_S. The window was
                    # open long enough. It closes here whatever the queue
                    # state below says, because a steady producer holds that
                    # predicate true forever.
                    self._gate_render_ticks = 0
                else:
                    gated = False
                    gate_window_open = True
                    self.gate_window_ticks += 1
                    # The pass must paint. The video block's source-fps tick
                    # divider otherwise skips this single frame on any video
                    # whose fps is below the loop's. The same deadline bounds
                    # that bypass, because it applies only while this window
                    # is open, so no video runs above its own frame rate for
                    # longer than GATE_WINDOW_MAX_S.
                    force_render = True
                    if self.tasks or self.image_tasks or self.touchscreen_task:
                        self._gate_render_ticks = self.GATE_SETTLE_TICKS
                    else:
                        self._gate_render_ticks -= 1

        if gated:
            self.gated_ticks += 1

        # The FPS throttle below reads this even when paused.
        has_bg_video = False

        bg_strip_dirty = False
        video_repaint = False

        # Snapshot once, because Background.set_video(None) from another
        # thread must not null this between the check and the reads.
        video = self.deck_controller.background.video
        if video is not None and not gated:
            if video.page is self.deck_controller.active_page:
                has_bg_video = True
                # Rate-limit the video's repaints to the write budget in
                # _video_write_hz. This gate decides whether the frame renders
                # at all, not only whether it is written.
                min_gap = 1.0 / self._video_write_hz if self._video_write_hz > 0 else 0
                if start - self._last_video_write >= min_gap:
                    video_repaint = True
                    self._last_video_write = start
                # Guard the tick divider against an fps of 0 or None, which
                # raises ZeroDivisionError, and against an fps above FPS. A 0
                # or a None plays at loop FPS. force_render bypasses this
                # divider so the gate's settle pass paints, and
                # GATE_WINDOW_MAX_S bounds that bypass, so a video below the
                # loop fps never runs above its own rate for longer than the
                # window lasts.
                video_fps = video.fps or self.FPS
                video_each_nth_frame = max(1, self.FPS // min(self.FPS, video_fps))
                if video_repaint and (force_render or self.media_ticks % video_each_nth_frame == 0):
                    self.deck_controller.background.update_tiles()
                    # A video extended onto the strip needs the shared
                    # touchscreen re-composited for the new frame.
                    bg_strip_dirty = self.deck_controller.background.get_touchscreen_image() is not None

        # Iterate the keys only when animated content needs an update.
        if not gated and (video_repaint or self._needs_key_ticks()):
            # Snapshot the dict and use .get, because the screensaver swaps
            # the whole inputs dict from another thread. init_inputs builds
            # then swaps, so every dict this sees is complete, but read
            # deck_controller.inputs once per tick and never subscript it
            # directly. _needs_key_ticks uses the same pattern.
            inputs = self.deck_controller.inputs
            #TODO: generalize
            for key in inputs.get(Input.Key, []):
                cast("ControllerKey", key).on_media_player_tick()

            # The dials and any per-touchscreen background video share one
            # touchscreen. Render it at most once per frame, not once per
            # dial.
            dials = inputs.get(Input.Dial, [])
            touchscreens = inputs.get(Input.Touchscreen, [])
            touchscreen_dirty = False
            for dial in dials:
                if cast("ControllerDial", dial).on_media_player_tick():
                    touchscreen_dirty = True
            for touchscreen in touchscreens:
                if cast("ControllerTouchScreen", touchscreen).on_media_player_tick():
                    touchscreen_dirty = True
            if (touchscreen_dirty or bg_strip_dirty) and touchscreens:
                cast("ControllerTouchScreen", touchscreens[0]).update()

        self.perform_media_player_tasks()

        self.media_ticks += 1

        end = time.time()

        if media_prof:
            media_prof.add("tick", end - start)
            media_prof.maybe_report()

        # Use a low FPS when idle, with no animated content and no pending
        # task. These slot reads take no _slot_lock. A torn read only
        # mis-picks the target FPS for one tick and never affects
        # correctness, because the next tick re-reads and self-corrects.
        #
        # gated outranks _cached_needs_ticks. _needs_key_ticks() writes that
        # cache inside the block the gate skips, so under the gate it holds
        # whatever the last rendering tick saw, and a page with a key video
        # would otherwise spin this loop at 30 Hz for nothing. has_pending
        # still outranks the gate, so queued frames, from interactive paints
        # and control-adjacent work, drain at full speed.
        #
        # The gated cadence stays at 2 Hz instead of the slowest possible,
        # because check_resume_gap() reads an inter-iteration gap of 5s or
        # more as a resume and schedules a full repaint. Anyone who lengthens
        # this past ~4s must teach that check about quiescence first. It costs
        # nothing, because a gated tick is a drain, a check and a wait. The
        # CPU win is the skipped decode, composite, encode and write.
        has_pending = bool(self.tasks or self.image_tasks or self.touchscreen_task)
        if has_pending:
            target_fps = self.FPS
        elif gate_window_open:
            # The settle window is open, so run it out at full FPS. It is a
            # render window bounded in wall clock by GATE_WINDOW_MAX_S. At the
            # 2Hz gated cadence one tick sleeps through the whole window, so a
            # page loaded while gated gets exactly one pass, fired before its
            # background finishes installing, and its transparent keys are
            # never painted at all.
            target_fps = self.FPS
        elif gated:
            target_fps = 2
        elif has_bg_video or getattr(self, '_cached_needs_ticks', False):
            target_fps = self.FPS
        else:
            target_fps = 2  # idle; check for new tasks occasionally

        self.append_fps(1 / (end - start))
        self.update_low_fps_warning()
        wait = max(0, 1/target_fps - (end - start))
        # Event-based wait on both paths. A submitted control op or an
        # interactive paint wakes the loop at once, instead of a wait for a
        # full active-FPS tick.
        self._wake_event.wait(wait)
        self._wake_event.clear()

        # No _stop check here. It sits at the top, right after the
        # control-queue drain, so the loop always goes around once more and
        # drains before it honors a stop.
        return True

    def next_submit_seq(self) -> int:
        """Allocate the next value from the writer's monotonic submit-seq
        counter. add_image_task and add_touchscreen_task use it to stamp a
        task, and DeckController.clear() uses it to capture the counter at a
        Clear's submission time."""
        return next(self._submit_seq)

    def submit_control(self, msg) -> None:
        """Append and wake, without blocking. Safe from any thread, because a
        deque append is GIL-atomic and needs no lock.

        It rejects a message once the writer stops or closes. The loop is gone
        by then, so nothing would ever drain a message appended after that
        point, and control_q would grow unbounded for the rest of the
        process's life if a late plugin or API callback kept calling
        set_brightness() on a torn-down deck."""
        if self._stop:
            return
        self.control_q.append(msg)
        self._wake_event.set()

    def drain_control_queue(self) -> bool:
        """Execute every pending control message, FIFO. Returns False after
        it processes a terminal ClearAndCloseMsg, and the caller must then
        stop the loop. It is split out of run() so a unit-tier scenario drives
        the control queue without a running thread; the harness's stub
        controller never starts the thread. See tests/fixtures.py."""
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
        # Write to the device directly, not through
        # DeckController.set_brightness(), which re-submits and loops forever.
        # The error policy is attempt and swallow, reported to the unified
        # per-controller handler as the task classes do.
        try:
            self.deck_controller.deck.set_brightness(int(msg.value))
            self.deck_controller._on_write_result(True)
        except Exception as e:
            log.error(f"Failed to set brightness: {e}")
            self.deck_controller._on_write_result(False)

    def _exec_release_stashed_inputs(self, msg: "ReleaseStashedInputsMsg") -> None:
        """Run on the media player thread, serialized with every render and
        write it does. See the ReleaseStashedInputsMsg docstring for why this
        is a control message and not an add_task()."""
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
        """Detect a wall-clock gap of 5s or more between media-loop
        iterations, which is the signature of a process suspend and resume
        cycle. It is split out of run() so a unit-tier scenario drives it
        without a running thread, as drain_control_queue is. Returns whether
        it detected a gap, and not whether a repaint fired, because
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
        # Wipe only the slots whose frame predates this Clear. A frame
        # submitted after this Clear survives and paints afterward, which
        # keeps the queued Clear in the caller's clear-then-paint order. The
        # _slot_lock covers the per-key get-then-del, which could otherwise
        # delete a newer task assigned in between, one whose submit_seq
        # survives this Clear. The touchscreen slot needs the same guard,
        # because clear_media_player_tasks() and close() also null it from
        # other threads.
        with self._slot_lock:
            for key in list(self.image_tasks.keys()):
                task = self.image_tasks.get(key)
                if task is not None and task.submit_seq is not None and task.submit_seq < msg.seq:
                    del self.image_tasks[key]
            ts_task = self.touchscreen_task
            if (ts_task is not None and ts_task.submit_seq is not None
                    and ts_task.submit_seq < msg.seq):
                self.touchscreen_task = None
        # Reset the dedup state on every current input before the blanks go
        # out. Otherwise an identical repaint after this Clear matches the
        # pre-clear cached hash, is wrongly skipped, and leaves the device
        # stuck on blank.
        self.deck_controller._reset_dedup_hashes()
        try:
            self.deck_controller._write_blank_frames()
        except Exception as e:
            log.error(f"Failed to write blank frames for Clear: {e}")

        # A Clear can execute after the paints it was meant to precede. The
        # caller queues it, then fills the task slots, so a tick that already
        # drained control writes those paints and pops this Clear next pass.
        # The seq filter above cannot help, because those paints already ran,
        # and the blanks land last. Behind a showing screensaver that state is
        # terminal, because a still image animates nothing and no other
        # producer runs, so the deck stays blank until the screensaver is
        # dismissed. Arm the repaint retry, which _run_pending_repaint
        # services, from the media thread only.
        #
        # The test must be exactly that a frame stamped after this Clear
        # reached the device. An arm on an ordinary transition is harmful,
        # because _run_pending_repaint composites the whole deck unlocked from
        # the media thread ahead of the task drain, racing an
        # update_all_inputs() under _load_page_lock, and ControllerKey.update()
        # leaves _last_enqueued_hash and add_image_task unsynchronised, so a
        # pre-swap repaint can land last and stick with both dedup hashes
        # agreeing. Queue occupancy cannot tell the two apart, because the
        # screensaver clears the slots between its Clear and its paints. This
        # counter moves only when a frame goes out, so it exceeds this Clear's
        # seq only where the paints went out and these blanks overwrote them.
        #
        # DeckController.clear() has three callers. The screensaver's entry and
        # exit stamp expects_repaint True; load_page's page-is-None branch does
        # not, because there a blank deck is the requested end state. The
        # submitter stamps it, because hide() clears showing one statement
        # later.
        if msg.expects_repaint and self._max_executed_seq > msg.seq:
            self.deck_controller._schedule_full_repaint()

    def _exec_clear_and_close(self) -> None:
        # Set _stop before any of the work below, and do not rely only on the
        # external stop() call that follows this message. In the window
        # between this terminal message landing and that stop() call, a late
        # submit_control() would otherwise still be accepted into a queue that
        # nothing drains again.
        self._stop = True
        with self._slot_lock:
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
        # True when an input has animated content that advances on the media
        # tick, such as a key or dial video, or a scrolling label.
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
        if not needs:
            for touchscreen in self.deck_controller.inputs.get(Input.Touchscreen, []):
                state = touchscreen.get_active_state()
                if state is not None and state.background_video is not None:
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
        ui_port.get().set_low_fps_warning(self.deck_controller, state)


    def wake(self) -> None:
        """Cut short the loop's inter-tick wait. Safe from any thread,
        because it only calls Event.set(). The in-module producers
        submit_control, add_task, add_image_task, add_touchscreen_task and
        stop poke _wake_event directly. This is the public name for an
        external caller with no task to submit, such as the presence monitor's
        transition fan-out, whose whole effect is that the next tick evaluates
        the gate differently."""
        self._wake_event.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop = True
        self._wake_event.set()  # wake an idle loop so it sees _stop promptly
        start = time.time()
        while self.running and time.time() - start < timeout:
            time.sleep(0.05)

    def add_task(self, method: Callable[..., Any], *args, **kwargs):
        self.tasks.append(MediaPlayerTask(
            deck_controller=self.deck_controller,
            page=self.deck_controller.active_page,
            _callable=method,
            args=args,
            kwargs=kwargs
        ))
        self._wake_event.set()

    def add_touchscreen_task(self, native_image: bytes, page=None, config_gen=None, controller_touchscreen=None, img_hash=None):
        task = MediaPlayerSetTouchscreenImageTask(
            deck_controller=self.deck_controller,
            page=page if page is not None else self.deck_controller.active_page,
            native_image=native_image,
            config_gen=config_gen,
            controller_touchscreen=controller_touchscreen,
            img_hash=img_hash
        )
        # Stamp inside the slot lock. A seq allocated before the lock lets
        # two racing producers, the media tick and a dial update on another
        # thread, allocate in one order and assign in the other, and the
        # single slot then holds the older frame. With the stamp and the
        # assignment atomic, seq order is assignment order, the slot ends up
        # with the newest frame, and a Clear's survives-if-submitted-after
        # test stays consistent with what the slot holds.
        with self._slot_lock:
            task.submit_seq = self.next_submit_seq()
            self.touchscreen_task = task
        self._wake_event.set()

    def add_image_task(self, key_index: int, native_image: bytes, page=None, config_gen=None, controller_key=None, img_hash=None):
        task = MediaPlayerSetImageTask(
            deck_controller=self.deck_controller,
            page=page if page is not None else self.deck_controller.active_page,
            key_index=key_index,
            native_image=native_image,
            config_gen=config_gen,
            controller_key=controller_key,
            img_hash=img_hash
        )
        # Stamp inside the lock as add_touchscreen_task does. The per-key
        # slots have the same producer-against-producer shape.
        with self._slot_lock:
            task.submit_seq = self.next_submit_seq()
            self.image_tasks[key_index] = task
        self._wake_event.set()

    def perform_media_player_tasks(self):
        # Drain the queues before the page and generation snapshot. Every
        # drained task then predates the snapshot, so a mismatch means stale.
        # The reverse order drops a task just queued for the new page, unrun.
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

        # Take _slot_lock. A producer that assigns between the read and the
        # null loses its frame, and with _last_enqueued_hash already stamped a
        # static strip stays stale forever. clear_media_player_tasks also
        # nulls this, from the GTK thread.
        with self._slot_lock:
            touch_task = self.touchscreen_task
            self.touchscreen_task = None

        # Snapshot the page and the generation as one pair, so the whole
        # batch is judged consistently. The assignment in load_page holds the
        # same lock.
        with self.deck_controller._page_gen_lock:
            active_page = self.deck_controller.active_page
            current_gen = self.deck_controller._page_load_generation

        def _is_current(task):
            # Drop a paint for a page the deck left, or for a superseded
            # generation. config_gen is the generation the paint rendered at.
            if task.page is not active_page:
                return False
            if task.config_gen is not None and task.config_gen != current_gen:
                return False
            return True

        for task in task_batch:
            if task.page is active_page:
                task.run()

        # Bulk-batch write pacing, off by default. A video-frame repaint
        # lands as a burst of back-to-back writes, and the transport
        # serializes the reads and writes of a deck on one mutex, so under an
        # unfair lock the writer releases and re-acquires ahead of the waiting
        # 20Hz HID read poll, which starves the dials. A yield between bulk
        # writes hands the reader a mutex slot, and the FIFO transport lock
        # hands it one by construction, so DECKARD_WRITE_YIELD_MS defaults to
        # 0 and this loop is a straight write. The machinery stays as the
        # field bisection tool if dial latency regresses; 1.5 restores the
        # older pacing with no rebuild. An interactive paint, in a small
        # batch, is never paced. The stride exists because the read poll needs
        # one mutex window per ~50ms, not one per write. Per-write yields cost
        # ~12ms per video frame on high-entropy content where dedup skips
        # nothing, measured at a 19fps loop on a busy video.
        bulk = len(image_batch) >= self.BULK_BATCH_THRESHOLD
        writes_since_yield = 0
        for task in image_batch:
            if _is_current(task):
                if bulk and writes_since_yield >= self.YIELD_STRIDE and self._inter_write_yield > 0:
                    time.sleep(self._inter_write_yield)
                    writes_since_yield = 0
                task.run()
                self._note_executed(task)
                writes_since_yield += 1

        if touch_task is not None and _is_current(touch_task):
            # Rate-cap every touchscreen write with the same budget as the
            # background video, _video_write_hz. A dial-state video and a
            # scrolling label re-render the shared strip from the media tick
            # at loop FPS, which is the same HID-starvation vector the cap was
            # built for, through a different content type. The cap sits here
            # at the write point, so it covers every producer: the bg-video
            # strip, a dial video, a scroll label and an interactive paint.
            # The latest frame wins. An over-budget frame goes back into the
            # single task slot unless a newer frame arrived meanwhile, and the
            # next iteration writes the freshest composite, so content is
            # delayed by at most one budget window and never lost. A non-empty
            # slot keeps the loop at active FPS, so the retry is prompt. One
            # _last_touch_write timestamp shares the budget across every
            # producer, so a frame can wait one window against a different
            # stream; a bg-video frame that arrives right after a scroll-label
            # write is re-queued and waits.
            now = time.time()
            min_gap = 1.0 / self._video_write_hz if self._video_write_hz > 0 else 0
            if min_gap and now - self._last_touch_write < min_gap:
                # Locked check-then-set. A producer that assigns a newer
                # frame between the None check and the putback must win;
                # unguarded, the putback clobbers it with this older frame.
                with self._slot_lock:
                    if self.touchscreen_task is None:
                        self.touchscreen_task = touch_task
            else:
                self._last_touch_write = now
                if bulk and writes_since_yield >= self.YIELD_STRIDE and self._inter_write_yield > 0:
                    time.sleep(self._inter_write_yield)
                touch_task.run()
                self._note_executed(touch_task)

    def _note_executed(self, task) -> None:
        """Record that the task's device write was attempted and did not
        raise. A caller reaches this only after the task's own run() returns,
        never for a task dropped as stale or deferred by the touchscreen write
        budget, because a frame that was never sent must not advance this.

        This is not quite the same as reaching the device. The task classes
        swallow StreamDeck.TransportError, so a write that failed at the
        transport still returns normally and advances this. That is safe,
        because every such failure runs _on_write_result(False), which arms
        the same pending repaint this counter triggers, so the recovery
        happens either way.

        Writer thread only, so the read-compare-write needs no lock."""
        seq = task.submit_seq
        if seq is not None and seq > self._max_executed_seq:
            self._max_executed_seq = seq
