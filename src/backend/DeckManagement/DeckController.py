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
import contextlib
import gc
import itertools
import math
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
from PIL import Image, ImageDraw, ImageEnhance, ImageSequence
from StreamDeck.Devices import StreamDeck
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus
from loguru import logger as log

# Import own modules
from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.fair_lock import FairLock
from src.backend.DeckManagement.HelperMethods import *
from src.backend.DeckManagement.ImageHelpers import *
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.backend.DeckManagement.Media.MediaConfig import MediaConfig
from src.backend.DeckManagement.Subclasses.ActionPermissionManager import ActionPermissionManager
from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.ScreenSaver import ScreenSaver
from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.Subclasses import mp4_tile_cache
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import get_video_md5
from src.backend.DeckManagement.Subclasses.encoded_image_cache import EncodedImageCache
from src.backend.DeckManagement.Subclasses.native_tile_cache import NativeTileCache, native_tile_cache_max_bytes
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.mem_telemetry import page_switches
from src.backend import timer_wheel
from src.backend.PageManagement.Page import ActionOutdated, Page, NoActionHolderFound
from src.api import notify_active_page_changed
from src.backend import ui_port

process = psutil.Process()

# Import signals
from src.Signals import Signals

# Import typing
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, overload

from src.backend.PluginManager.ActionCore import ActionCore
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager

    class ComposedKeyLabel(KeyLabel):
        """A KeyLabel that has been through LabelManager.inject_defaults():
        every field is populated, so the render path can read them without a
        None check at each use.

        Declared under TYPE_CHECKING only -- it adds no class at runtime, and
        inject_defaults() keeps returning the very object it filled in. This
        exists so "composed" is a type the checker can carry from the one
        place the invariant is established to the eight places that rely on
        it, instead of eight narrowings that would each have to re-assert it.
        """
        text: str
        font_size: int
        font_name: str
        font_weight: int
        style: str
        color: list[int]
        outline_width: int
        outline_color: list[int]
        alignment: str

    class ComposedImageLayout(ImageLayout):
        """An ImageLayout after LayoutManager.inject_defaults(). Same contract
        and same TYPE_CHECKING-only rationale as ComposedKeyLabel."""
        valign: float
        halign: float
        fill_mode: str
        size: float

# Import globals
import globals as gl

import io


# JPEG quality every key native is encoded at. Named because it is part of
# the native tile cache key: bytes encoded at one quality must never be
# served for another.
KEY_ENCODE_QUALITY = 90


def encode_native_key(deck, image: "Image.Image", quality: int = KEY_ENCODE_QUALITY) -> bytes:
    """PILHelper.to_native_key_format with tunable JPEG quality (the library
    hardcodes q100): smaller JPEGs mean fewer serial USB HID writes per key."""
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
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if fmt["flip"][1]:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
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
    # None when the deck has no active page (boot / mid-teardown). Only ever
    # identity-compared against active_page at the present boundary, never
    # dereferenced -- so a page-less paint is judged stale, not crashed on.
    page: Page | None
    _callable: Callable[..., Any]
    args: tuple
    kwargs: dict

    def run(self):
        self._callable(*self.args, **self.kwargs)

@dataclass
class MediaPlayerSetTouchscreenImageTask:
    deck_controller: "DeckController"
    # None when the deck has no active page (boot / mid-teardown). Only ever
    # identity-compared against active_page at the present boundary, never
    # dereferenced -- so a page-less paint is judged stale, not crashed on.
    page: Page | None
    native_image: bytes
    config_gen: int | None = None  # generation of the content rendered; dropped at present if stale
    submit_seq: int | None = None  # writer's monotonic submit-seq stamp; None for pre-M1 construction
    controller_touchscreen: "ControllerTouchScreen | None" = None  # stamped once this paint is presented
    img_hash: int | None = None  # hash of the presented image, recorded in run()

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
    # None when the deck has no active page (boot / mid-teardown). Only ever
    # identity-compared against active_page at the present boundary, never
    # dereferenced -- so a page-less paint is judged stale, not crashed on.
    page: Page | None
    key_index: int
    native_image: bytes
    config_gen: int | None = None  # generation of the content rendered; dropped at present if stale
    controller_key: "ControllerKey | None" = None  # stamped once this paint is presented
    img_hash: int | None = None  # hash of the presented image, recorded in run()
    submit_seq: int | None = None  # writer's monotonic submit-seq stamp; None for pre-M1 construction

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
    (plan §2.1, preserves the caller's clear-then-paint order).

    `expects_repaint` records the submitter's intent, which the writer
    cannot infer once the message is on the queue: True means "I am about to
    repaint this deck, these blanks are a transition" (the screensaver's
    entry and exit), False means "leave it blank, that IS the result"
    (load_page's page-is-None branch). Only the former may be recovered from
    when it executes late -- see _exec_clear. Stamped at submission rather
    than read off live state at execution, because live state has already
    moved on by then."""
    seq: int
    expects_repaint: bool = False


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


def _env_float(name: str, default: float) -> float:
    """Reads a float tuning knob from the environment, falling back to
    `default` on a malformed value. A typo in an env var must degrade to the
    built-in default with a warning -- never raise out of MediaPlayerThread
    init, where DeckManager would swallow it as "Failed to initialize deck"
    and silently skip the whole device."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(f"Ignoring malformed {name}={raw!r}; using the default {default}")
        return default


def _install_fair_transport_lock(deck) -> bool:
    """Swaps the transport's per-device mutex for a FIFO one.

    The library guards every read, write and feature report of a device with
    `deck.device.mutex`, a stock threading.Lock. Unfair ordering there lets a
    write burst out-race the HID read poll, which is what dial input
    starvation is. The swap is a single attribute assignment on an object
    this process owns; the library is neither vendored nor patched, so an
    upstream rename degrades to today's behaviour (unfair lock, env knobs
    still available) rather than to broken -- hence every guard below returns
    False instead of raising.

    MUST run before `deck.open()`: that is what starts the reader thread, so
    before it no thread can be inside the old lock and the swap cannot leave
    two threads in hidapi at once. The reopen path (resume from suspend)
    reuses the same Device instance, so the FIFO lock survives suspend
    cycles without a reinstall. Returns whether the FIFO lock is installed."""
    device = getattr(deck, "device", None)
    if device is None:
        # FakeDeck / RemoteDeck and friends have no HID transport to order.
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
        # Only reachable if this ever moves after open(): swapping a held
        # lock would let the holder and a new acquirer into hidapi together.
        log.warning("Transport mutex is held; skipping the fair transport lock")
        return False

    device.mutex = FairLock()
    log.info(f"Installed the fair (FIFO) transport lock on "
             f"{type(device).__name__}")
    return True


class MediaPlayerThread(threading.Thread):
    # An image batch at or above this size is a bulk repaint (video frame /
    # full-page paint) and gets inter-write yields; below it is interactive
    # (press feedback, plugin set_media) and writes immediately.
    BULK_BATCH_THRESHOLD = 4
    # Within a bulk batch, yield after every N writes (see the comment at
    # the batch loop in perform_media_player_tasks).
    YIELD_STRIDE = 3
    # Quiet render ticks the page-generation watch owes a new generation
    # before the quiescence gate may re-engage. Small on
    # purpose: these run at full FPS, so the window costs ~100ms of frames
    # per page change made while the user is away.
    GATE_SETTLE_TICKS = 3
    # Hard wall-clock bound on that window. The countdown above re-arms
    # whenever the task queues are non-empty ("page-load work is still
    # landing") -- but that predicate is ALSO permanently true under any
    # >=10Hz producer: a plugin looping set_media -> add_image_task, or the
    # touchscreen latest-wins re-queue (see perform_media_player_tasks)
    # under a low DECKARD_VIDEO_WRITE_HZ. Unbounded, such a producer pins
    # the loop un-gated at full FPS for the entire away window -- a silent
    # total failure of the gate, with nothing in the logs to say so. So the
    # window also closes on this deadline, whatever the queues say. It is
    # generous next to what it bounds (a settle is GATE_SETTLE_TICKS quiet
    # ticks ~= 100ms plus however long the page-load tasks take to drain),
    # and the cost if it ever truncates a genuinely slow page load is one
    # away window of stale imagery on that page's transparent keys -- the
    # same failure the watch exists to avoid, but for one page instead of
    # for the whole gate.
    GATE_WINDOW_MAX_S = 0.5

    def __init__(self, deck_controller: "DeckController"):
        # Suffix the thread name with the deck serial so per-deck writer
        # attribution is possible when two decks run at once (previously every
        # media thread was just "MediaPlayerThread", making the journal's
        # thread_name ambiguous in two-deck scenarios). serial_number() is a
        # cheap attribute read that never raises on any real or fake deck.
        try:
            _serial = deck_controller.serial_number()
        except Exception:
            _serial = "unknown"
        super().__init__(name=f"MediaPlayerThread-{_serial}", daemon=True)
        self.deck_controller: DeckController = deck_controller
        self.FPS = 30 # Max refresh rate of the internal displays

        # Cap how often a background video repaints the device. This used to
        # be a starvation defense: the transport serializes all reads and
        # writes of a deck on one mutex, and with an unfair lock a write
        # flood out-raced the 20Hz HID read poll, so dial events arrived
        # coalesced. The FIFO transport lock (see
        # _install_fair_transport_lock) is the guarantee now -- the reader
        # waits at most for the chunk in flight -- so this cap is only a
        # rate alignment: the same gate drives the repaint DECISION at
        # `now - _last_video_write < min_gap` below, i.e. it governs render
        # cost as well as write cost, and rendering above the loop rate
        # (FPS) buys nothing. Hence 30, not unlimited. Render cost on
        # high-entropy content (tile dedup can't skip anything, ~270
        # candidate writes/s) is the native-tile-cache issue's problem, not
        # this knob's. 0 disables the cap; the env var stays a field
        # bisection tool -- set it back to 20 to reproduce the old pacing.
        self._video_write_hz = _env_float("DECKARD_VIDEO_WRITE_HZ", 30.0)
        self._last_video_write = 0.0
        # The same budget caps ALL touchscreen writes at the write point in
        # perform_media_player_tasks (dial-state videos and scrolling labels
        # otherwise rewrite the strip at loop FPS -- the identical
        # HID-starvation vector via a different content type).
        self._last_touch_write = 0.0

        # Inter-write yield inside bulk batches (seconds); see the comment in
        # perform_media_player_tasks. Off by default now that transport
        # acquisitions are ordered: the yield existed purely to hand the
        # reader a mutex slot, which FIFO ordering grants it anyway. The
        # machinery stays so the env var can restore the old pacing (1.5) in
        # the field without a rebuild.
        self._inter_write_yield = _env_float("DECKARD_WRITE_YIELD_MS", 0.0) / 1000.0

        self.running = False
        self.media_ticks = 0
        # Ticks that skipped the animation section because the user is away.
        # The assertion handle for the quiescence scenarios and
        # the hardware driver; `media_ticks - gated_ticks` is the number of
        # ticks that actually rendered.
        self.gated_ticks = 0
        # Ticks the settle window rendered instead of gating.
        # Lives next to gated_ticks so "the render window is open" is
        # observable from outside the loop: a window that never closes is
        # this gate's worst failure mode and would otherwise be invisible --
        # `gated_ticks` merely stops advancing, which is also what a
        # not-yet-quiescent user looks like.
        self.gate_window_ticks = 0
        # Page-generation watch state (see _run_one_tick): the generation
        # last observed while gated, how many more render ticks the settle
        # window owes it, and the monotonic instant the window closes no
        # matter what (GATE_WINDOW_MAX_S).
        self._gated_generation: int | None = None
        self._gate_render_ticks = 0
        self._gate_window_deadline = 0.0

        self._stop = False

        self.tasks: list[MediaPlayerTask] = []
        self.image_tasks: dict[int, MediaPlayerSetImageTask] = {}
        self.touchscreen_task: MediaPlayerSetTouchscreenImageTask | None = None
        # Guards the single-slot task stores against producer/consumer
        # interleaves: the drain's read-then-null on
        # touchscreen_task and the Clear's get-then-del on image_tasks could
        # both discard a task assigned in between -- and the producer had
        # already stamped _last_enqueued_hash, so static content stayed
        # stale forever (no next tick re-enqueues it). Critical sections are
        # a few instructions; uncontended cost is negligible per tick.
        self._slot_lock = threading.Lock()
        self._wake_event = threading.Event()

        # Control queue (plan §2.1): append/popleft are GIL-atomic, so no
        # extra lock is needed. Drained fully, first, every wake -- before
        # any animation tick or task work -- so control ops (brightness,
        # clear) never wait behind ticker/task work.
        self.control_q: collections.deque = collections.deque()
        # Per-writer monotonic stamp counter: image/touchscreen tasks are
        # stamped with next(self._submit_seq) under _slot_lock, atomically
        # with their slot assignment (add_image_task/add_touchscreen_task:
        # stamping before the lock let racing producers assign
        # out of seq order, leaving a slot holding an older frame), and a
        # Clear captures the counter at its own submission (next_submit_seq())
        # so it can tell which already-queued frames predate it (plan
        # §2.1/§2.2).
        self._submit_seq = itertools.count()
        # Highest submit_seq whose frame actually reached the device, as
        # opposed to merely being queued or dropped at the present boundary.
        # Advanced only right after a task's own run() returns, so it means
        # "this content is on the deck now". Compared against a Clear's seq
        # in _exec_clear to tell the two Clear interleaves apart -- see
        # there. Media-thread-only, like the rest of the drain state.
        self._max_executed_seq: int = -1

        # Wall-clock gap detection (plan §4 M2): a gap much larger than the
        # loop's own wait interval means the process was suspended (system
        # sleep) and just resumed -- DetectResumeThread's proven technique,
        # relocated into this loop instead of a separate thread. See
        # check_resume_gap().
        self._last_iter_ts: float = time.time()

        self.fps: list[float] = []
        self.old_warning_state = False

        self.show_fps_warnings = gl.settings_manager.app().enable_fps_warnings

        # Loop-guard state: this thread is the sole writer for
        # paints/brightness/Clear/ClearAndClose -- if it dies the deck is
        # frozen until replug. The guard in run() keeps it alive; these
        # rate-limit its logging so a per-tick failure can't storm the sinks
        # (local, pending a general limiter).
        self._last_tick_error_log: float = 0.0
        self._suppressed_tick_errors: int = 0

    def run(self):
        self.running = True

        # The body is guarded: an uncaught exception here used to
        # kill the sole writer and permanently freeze the deck. @log.catch on
        # run() would be wrong -- it logs once and RETURNS, dying anyway; the
        # guard must sit inside the while. The central threading.excepthook is
        # complement (reports an escaping death); this prevents the death.
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
                    # lists (perform_media_player_tasks drains image_tasks/
                    # touchscreen_task before running them), so the failing
                    # frame's SIBLINGS are lost too -- without a scheduled
                    # recovery the not-yet-painted keys keep their previous
                    # imagery silently forever. Arm the pending full repaint
                    # (the same recovery a failed device write uses); its 2s
                    # rate limit makes this safe against a deterministic
                    # per-tick failure.
                    self.deck_controller._schedule_full_repaint()
                    # A raising body never reaches the FPS wait below --
                    # without this backoff a persistent failure becomes a
                    # 100% spin. _wake_event (not sleep) so stop() still
                    # wakes us instantly -- but every producer sets that
                    # event too, so a single wait() under a set_media storm
                    # returns immediately and the retry rate would track the
                    # producer rate instead of ~4Hz. Re-wait until the
                    # backoff truly elapsed; only _stop cuts it short.
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
            # In a finally, not at the loop's tail: the guard is `except
            # Exception`, so a BaseException (SystemExit/KeyboardInterrupt)
            # escapes it -- if that death left running=True, every later
            # stop() would burn its full join timeout waiting on a corpse.
            self.running = False

    def _open_gate_window(self) -> None:
        """(Re-)opens the gated render window: GATE_SETTLE_TICKS
        quiet render ticks, and never more than GATE_WINDOW_MAX_S of wall
        clock however busy the task queues stay. Writer thread only."""
        self._gate_render_ticks = self.GATE_SETTLE_TICKS
        self._gate_window_deadline = time.monotonic() + self.GATE_WINDOW_MAX_S

    def _run_one_tick(self) -> bool:
        """One iteration of the writer loop. Returns False to stop."""
        start = time.time()

        # 1. Drain the control queue fully, FIRST, every wake (plan §2.2) --
        # before the resume-gap check, the pending-repaint hook, any
        # animation tick or task work, and before honoring a pending stop.
        # Two orderings matter:
        #   * drain before _stop: stop()'s caller (close_all()) submits a
        #     terminal ClearAndCloseMsg and then immediately calls stop() --
        #     if _stop were checked before this drain (or at the bottom of
        #     the loop, after a wake that raced stop()'s flag-set against
        #     this iteration's work), the just-submitted terminal message
        #     could be stranded unprocessed.
        #   * drain before EVERYTHING else: the rest of this tick can raise
        #     into run()'s guard -- if any of it ran ahead of the drain, a
        #     persistently failing tick would starve SetBrightnessMsg/
        #     ClearMsg/ClearAndCloseMsg forever (deck never blanked or
        #     closed on quit).
        # Every iteration drains first, unconditionally, THEN looks at _stop.
        if not self.drain_control_queue():
            return False
        if self._stop:
            return False

        self.check_resume_gap(start)
        repaint_fired = self.deck_controller._run_pending_repaint()

        # Quiescence gate. STRICTLY after the control-queue
        # drain and the _stop check above: quit/clear/brightness must never
        # wait on quiescence. When it holds, this tick skips the whole
        # animation section below -- no background decode/composite, no key/
        # dial/touchscreen tick, no scroll-label advance -- while
        # perform_media_player_tasks() still runs, so queued interactive
        # paints and control-adjacent work stay live and the deck stays
        # functional. Only *animation* pauses.
        gated = self.deck_controller.animations_gated()
        force_render = False
        gate_window_open = False
        if gated:
            # Page-generation watch: update_all_inputs() deliberately leaves
            # the DEVICE paint of non-opaque keys to this loop whenever a
            # background video is set (see its early branch) -- it paints
            # dials and fully-opaque keys and pushes the rest to the UI
            # preview only. A page load or background swap landing while
            # gated (--change-page, a plugin ChangePage, a deck plugged in
            # while quiescent, ScreenSaver.hide()'s phase 3) would therefore
            # leave the PREVIOUS page's imagery on every transparent key for
            # the whole away window, and _run_pending_repaint() is no escape
            # hatch -- it calls that same update_all_inputs(). So: render
            # un-gated across a new generation, then re-gate.
            #
            # Why a settle WINDOW and not a single pass: the generation is
            # bumped at the very top of load_page(), while the work that
            # builds the new page's inputs and background is queued onto this
            # thread afterwards. A single pass fired on the first tick that
            # sees the new generation would render the OLD page's content --
            # correctly, and then never again. So the window keeps rendering
            # while task queues are non-empty (page-load work still landing)
            # and only counts down over ticks where they are quiet; the last
            # of those is the pass that paints the settled new page -- with a
            # wall-clock stop on the whole thing, since "queues non-empty" is
            # a producer's permanent state (GATE_WINDOW_MAX_S). Same
            # snapshot-under-_page_gen_lock pattern perform_media_player_tasks
            # uses.
            with self.deck_controller._page_gen_lock:
                current_gen = self.deck_controller._page_load_generation
            if current_gen != self._gated_generation:
                self._gated_generation = current_gen
                self._open_gate_window()
            if repaint_fired:
                # A full repaint that just fired (suspend/resume, or the 2s
                # retry after write failures) bumps no generation but runs
                # through the SAME update_all_inputs() -- so it has the same
                # transparent-key blind spot on a video-bg page. Open the
                # window for it too, or a machine that wakes from sleep while
                # the user is still away leaves those keys showing whatever
                # survived the suspend.
                self._open_gate_window()
            if self._gate_render_ticks > 0:
                if time.monotonic() >= self._gate_window_deadline:
                    # Wall-clock stop (GATE_WINDOW_MAX_S): the window has been
                    # open long enough. Closed here regardless of the queue
                    # state below -- that predicate is exactly the one a
                    # steady producer holds true forever.
                    self._gate_render_ticks = 0
                else:
                    gated = False
                    gate_window_open = True
                    self.gate_window_ticks += 1
                    # The pass has to actually paint: the video block's
                    # source-fps tick divider would otherwise skip this single
                    # frame outright on any video whose fps is below the
                    # loop's. That bypass is bounded by the same deadline --
                    # it only applies while this window is open, so no video
                    # is forced above its own frame rate for longer than
                    # GATE_WINDOW_MAX_S.
                    force_render = True
                    if self.tasks or self.image_tasks or self.touchscreen_task:
                        self._gate_render_ticks = self.GATE_SETTLE_TICKS
                    else:
                        self._gate_render_ticks -= 1

        if gated:
            self.gated_ticks += 1

        # Read by the FPS throttle below even when paused.
        has_bg_video = False

        bg_strip_dirty = False
        video_repaint = False

        # Snapshot once: Background.set_video(None) from
        # another thread must not null this between the check and the reads.
        video = self.deck_controller.background.video
        if video is not None and not gated:
            if video.page is self.deck_controller.active_page:
                has_bg_video = True
                # Rate-limit the video's repaints to the write budget (see
                # _video_write_hz) -- this gate decides whether the frame is
                # rendered at all, not just written.
                min_gap = 1.0 / self._video_write_hz if self._video_write_hz > 0 else 0
                if start - self._last_video_write >= min_gap:
                    video_repaint = True
                    self._last_video_write = start
                # Background video: guard the tick divider against fps<=0/None
                # (would ZeroDivisionError) and >FPS; 0/None plays at loop FPS.
                # `force_render` bypasses this divider so the gate's settle
                # pass actually paints; that bypass is bounded by
                # GATE_WINDOW_MAX_S (see the window block above), so a
                # sub-loop-fps video is never driven above its own rate for
                # longer than the window itself lasts.
                video_fps = video.fps or self.FPS
                video_each_nth_frame = max(1, self.FPS // min(self.FPS, video_fps))
                if video_repaint and (force_render or self.media_ticks % video_each_nth_frame == 0):
                    self.deck_controller.background.update_tiles()
                    # A video extended onto the strip needs the shared
                    # touchscreen re-composited for the new frame.
                    bg_strip_dirty = self.deck_controller.background.get_touchscreen_image() is not None

        # Only iterate keys if there is animated content to update
        if not gated and (video_repaint or self._needs_key_ticks()):
            # Snapshot + .get: the screensaver swaps the
            # whole inputs dict from another thread. init_inputs is
            # build-then-swap so any dict we see is complete -- but read
            # `deck_controller.inputs` once per tick and never hard-subscript
            # it (the pattern _needs_key_ticks already uses deliberately).
            inputs = self.deck_controller.inputs
            #TODO: generalize
            for key in inputs.get(Input.Key, []):
                cast("ControllerKey", key).on_media_player_tick()

            # Dials and any per-touchscreen background video share one
            # touchscreen; render it at most once per frame instead of
            # once per dial.
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

        # Perform media player tasks
        self.perform_media_player_tasks()

        self.media_ticks += 1

        end = time.time()

        if media_prof:
            media_prof.add("tick", end - start)
            media_prof.maybe_report()

        # Use low FPS when idle (no animated content, no pending tasks).
        # These slot reads are intentionally unlocked (no _slot_lock): a torn
        # read only mis-picks the target FPS for a single tick, never affects
        # correctness -- the next tick re-reads and self-corrects.
        #
        # `gated` outranks _cached_needs_ticks deliberately: that
        # cache is written by _needs_key_ticks() INSIDE the block the gate
        # skips, so under the gate it holds whatever the last rendering tick
        # saw -- a page with a key video would otherwise spin this loop at 30
        # Hz doing nothing. `has_pending` still outranks the gate: queued
        # frames (interactive paints, control-adjacent work) drain at full
        # speed exactly as today.
        #
        # The gated cadence stays at 2 Hz rather than "as slow as possible"
        # because check_resume_gap() reads a >=5s inter-iteration gap as a
        # suspend/resume and schedules a full repaint: anyone lengthening
        # this past ~4s has to teach that check about quiescence first. It
        # costs nothing -- a gated tick is drain, check, wait. The CPU win is
        # the skipped decode/composite/encode/write, not the wait length.
        has_pending = bool(self.tasks or self.image_tasks or self.touchscreen_task)
        if has_pending:
            target_fps = self.FPS
        elif gate_window_open:
            # The settle window is open: run it out at full FPS. It is a
            # RENDER window, and it is bounded in wall clock
            # (GATE_WINDOW_MAX_S) -- at the 2Hz gated/idle cadence a single
            # tick would sleep through the entire window, so a page loaded
            # while gated would get exactly one pass, fired before its
            # background finished installing, and its transparent keys would
            # never be painted at all.
            target_fps = self.FPS
        elif gated:
            target_fps = 2
        elif has_bg_video or getattr(self, '_cached_needs_ticks', False):
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
        return True

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
        # Under _slot_lock (Clear half): the per-key get-then-del
        # could delete a NEWER task assigned in between -- one whose
        # submit_seq contractually survives this Clear. Same for the
        # touchscreen slot (also nulled by clear_media_player_tasks()/close()
        # from other threads).
        with self._slot_lock:
            for key in list(self.image_tasks.keys()):
                task = self.image_tasks.get(key)
                if task is not None and task.submit_seq is not None and task.submit_seq < msg.seq:
                    del self.image_tasks[key]
            ts_task = self.touchscreen_task
            if (ts_task is not None and ts_task.submit_seq is not None
                    and ts_task.submit_seq < msg.seq):
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

        # A transition's Clear can execute AFTER the very frames it was
        # submitted to precede. Its caller submits it here on the control
        # queue and only then enqueues the paints onto the task slots, so a
        # tick that has already drained control writes those paints and pops
        # this Clear on its NEXT pass. The seq filter above cannot help --
        # it wipes queued tasks, and these already ran -- so the blanks land
        # last on a deck with nothing left to repaint it. Behind a showing
        # screensaver that is terminal: a still image animates nothing and
        # no other producer is running, so the deck stays blank until the
        # screensaver is dismissed. Arm the repaint retry, the same "content
        # written into a lost window" recovery this already provides after a
        # failed device write, and let _run_pending_repaint restore the
        # imagery. Media-thread state written from the media thread -- the
        # no-lock single-writer contract holds; never arm this from the
        # submitting thread.
        #
        # Both terms are load-bearing.
        #
        # _max_executed_seq is exactly "a frame stamped after this Clear has
        # already reached the device", and nothing weaker will do. Arming on
        # an ORDINARY transition would be actively harmful, not merely
        # wasteful: _run_pending_repaint composites and encodes the whole
        # deck from the media thread with no lock, at the top of a tick and
        # therefore ahead of the task drain, while the transition's own
        # update_all_inputs() is still running under _load_page_lock on its
        # own thread. ControllerKey.update() has no synchronisation around
        # its _last_enqueued_hash/add_image_task pair, so a repaint
        # composited against the pre-swap background can land last and
        # stick, leaving the OLD page's imagery on the deck with both dedup
        # hashes agreeing on it. Queue occupancy cannot express the
        # difference: the screensaver calls clear_media_player_tasks()
        # between submitting its Clear and enqueueing its paints, and that
        # empties the image_tasks dict and nulls the touchscreen slot (the
        # generic `tasks` list it also clears is not what this would be
        # reading), so the slots are empty at Clear time on every ordinary
        # transition too. This counter moves only when a frame actually goes
        # out, so it exceeds this Clear's seq only in the interleave where
        # the paints already went out and these blanks just overwrote them.
        #
        # expects_repaint keeps the recovery to callers who were going to
        # repaint anyway. DeckController.clear() has three: the screensaver's
        # entry and exit, which stamp it True, and load_page's page-is-None
        # branch, which does not -- there a blank deck is the requested end
        # state, and repainting it would undo the request. It is the
        # submitter's stamp rather than a screen_saver.showing read here
        # because that flag has already moved by the time this executes:
        # hide() submits its Clear one statement before clearing it.
        if msg.expects_repaint and self._max_executed_seq > msg.seq:
            self.deck_controller._schedule_full_repaint()

    def _exec_clear_and_close(self) -> None:
        # Set before doing any of the work below (not just relying on the
        # external stop() call that follows submitting this message): the
        # window between this terminal message landing and stop() actually
        # being called is exactly when a late submit_control() would
        # otherwise still be accepted into a queue nothing will ever drain
        # again (design doc bug 12).
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
        """Cuts short the loop's inter-tick wait. Safe from any thread
        (Event.set()). The in-module producers (submit_control, add_task,
        add_image_task, add_touchscreen_task, stop) poke `_wake_event`
        directly; this is the public name for external callers with no task
        to submit -- the presence monitor's transition fan-out,
        whose whole effect is that the NEXT tick evaluates the gate
        differently."""
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
        # Stamp INSIDE the slot lock: allocating the seq before
        # taking the lock let two racing producers (media tick vs. a dial
        # update on another thread) allocate in one order and assign in the
        # other, leaving the single slot holding the LOWER-seq (older) frame.
        # With stamp+assign atomic, seq order is assignment order, so the
        # slot always ends up with the newest frame -- and a Clear's
        # "survives if submitted after" comparison stays consistent with
        # what the slot actually holds.
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
        # Same stamp-inside-the-lock as add_touchscreen_task:
        # the per-key slots have the identical producer-vs-producer shape.
        with self._slot_lock:
            task.submit_seq = self.next_submit_seq()
            self.image_tasks[key_index] = task
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

        # Under _slot_lock: a producer assigning between the read and the
        # null lost its frame -- and with _last_enqueued_hash already
        # stamped, a static strip stayed stale forever (drain half).
        # clear_media_player_tasks (GTK thread) also nulls this.
        with self._slot_lock:
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

        # Bulk-batch write pacing, off by default: a
        # video-frame repaint lands as a burst of back-to-back writes, and
        # the transport serializes reads and writes of a deck on one mutex,
        # so with an unfair lock the writer could release and immediately
        # re-acquire ahead of the waiting 20Hz HID read poll (the
        # dial-starvation mechanism). Forcing a yield between BULK writes
        # handed the reader a mutex slot; the FIFO transport lock hands it
        # one by construction, so DECKARD_WRITE_YIELD_MS now defaults to 0
        # and this loop is a straight write. The machinery stays because the
        # yield is the field bisection tool if dial latency ever regresses:
        # 1.5 restores the old pacing without a rebuild. Interactive paints
        # (small batches) were never paced. The stride exists because the
        # read poll needs ONE mutex window per ~50ms, not one per write --
        # per-write yields cost ~12ms per video frame on high-entropy
        # content where dedup can't skip anything (measured: loop 19fps on a
        # busy video).
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
            # Rate-cap ALL touchscreen writes with the same budget as
            # background video (_video_write_hz): dial-state videos and
            # scrolling labels re-render the shared strip from the media
            # tick at loop FPS, which is the identical HID-starvation vector
            # the cap was built for, via a different content type. Enforced
            # here at the write point so every producer (bg-video strip,
            # dial video, scroll label, interactive paint) is covered.
            # Latest-wins: an over-budget frame goes back into the single
            # task slot (unless a newer frame arrived meanwhile) and the
            # next iteration writes the freshest composite -- content is
            # delayed by at most one budget window, never lost. The slot
            # being non-empty keeps the loop at active FPS (see has_pending
            # in run()), so the retry is prompt. The budget is shared across
            # ALL producers via the one _last_touch_write timestamp, so a
            # frame can be deferred even against a different stream -- e.g. a
            # bg-video frame arriving right after a scroll-label write waits
            # one window (re-queued, latest-wins); that's fine and by design.
            now = time.time()
            min_gap = 1.0 / self._video_write_hz if self._video_write_hz > 0 else 0
            if min_gap and now - self._last_touch_write < min_gap:
                # Locked check-then-set: a producer assigning a NEWER frame
                # between the None-check and the putback must win (unguarded,
                # the putback clobbered it with this older frame).
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
        """Records that `task`'s device write was attempted and did not
        raise. Called only after the task's own run() returns, never for one
        dropped as stale or deferred by the touchscreen write budget -- a
        frame that was never sent must not advance this.

        Not quite "reached the device": the task classes swallow
        StreamDeck.TransportError, so a write that failed at the transport
        still returns normally and advances this. That is safe rather than
        merely tolerable -- every such failure runs _on_write_result(False),
        which arms the very same pending repaint this counter exists to
        trigger, so the recovery happens either way.

        Writer thread only, so the read-compare-write needs no lock."""
        seq = task.submit_seq
        if seq is not None and seq > self._max_executed_seq:
            self._max_executed_seq = seq


class DeckController:
    # Bound on close() step 6's wait for plugin teardown hooks;
    # class-level so the harness can tighten it.
    TEARDOWN_JOIN_TIMEOUT_S = 10.0

    def __init__(self, deck_manager: "DeckManager", deck: StreamDeck.StreamDeck):
        self.deck_manager: DeckManager = deck_manager

        # Per-instance memo for stable deck properties (lru_cache on an instance
        # method would pin every self on the class and never evict).
        self._serial_number: str | None = None
        self._key_image_size: tuple[int, int] | None = None
        self._touchscreen_image_size: tuple[int, int] | None = None
        self._native_key_format_sig: tuple | None = None

        # Open the deck - why store it as self.deck? So that self.get_alive() returns True in get_deck_settings
        # (the raw handle answers is_open() the same way the BetterDeck wrapper
        # installed a few lines below does).
        # Assigned the raw StreamDeck handle here; rewrapped as BetterDeck a few
        # lines below.
        self.deck: BetterDeck = deck
        # Order the transport mutex FIFO before open() starts the reader
        # thread -- see _install_fair_transport_lock for why the ordering of
        # these two lines is load-bearing.
        _install_fair_transport_lock(deck)
        # Resume-from-suspend handle reopen is the library's only mode now
        # (plan §9.1, decided 2026-07-04) -- always on. Called on the raw
        # handle (`self.deck is deck` here): the wrapper's open() takes no
        # arguments.
        deck.open(True)

        rotation = self.get_deck_settings().get("rotation", 0)
        self.deck = BetterDeck(deck, rotation)

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
        
        self.hold_time: float = gl.settings_manager.app().hold_time
        
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
        # close() itself idempotent against a second call. The transition is
        # made under _close_lock: unplug-thread and
        # app-quit teardown can race, and an unlocked check-then-set let both
        # callers pass the gate and run the teardown sweep -- plugin
        # on_removed hooks and device closes -- concurrently.
        self._closing: bool = False
        self._close_lock = threading.Lock()

        # Timestamp of the last post-load GC (see maybe_collect_garbage).
        self._last_gc_time: float = 0.0

        self.active_page: Page | None = None

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
        self._screensaver_pending_page: "Page | None" = None
        # Serializes background loads on the pool; a superseded load must not
        # overwrite a newer page's background.
        self._background_load_lock = threading.Lock()
        self._bg_future = None

        # Native (encoded) key image caches. Initialized before the inputs
        # and the background: the paint path dereferences both directly, and
        # a background change clears them, so neither may lag behind
        # anything that can paint.
        #
        # encode_memo: keyed by (composite hash, rotation) -- repeated frames
        # (looping background video) skip conversion + JPEG encode.
        self.encode_memo = EncodedImageCache(max_bytes=32 * 1024 * 1024)
        # native_tile_cache: the same natives keyed by FRAME IDENTITY for the
        # passthrough path -- a bare key over a video background skips
        # the tobytes+hash too, so a warmed loop costs a dict lookup per key.
        self.native_tile_cache = NativeTileCache(max_bytes=native_tile_cache_max_bytes())
        # Enrol both in the process-wide image-cache budget: their own
        # caps bound each cache, the budget bounds the SUM across decks --
        # without it, total image-cache RAM scales with deck count and a cold
        # deck's full memo never yields a byte to a hot one. The registry is
        # weak, so close() needs no matching unregister. Wrapped here as well
        # as inside register(): DeckManager swallows anything raised out of
        # __init__ as "Failed to initialize deck" and silently skips the whole
        # device, which no telemetry/housekeeping feature may ever cost a user.
        try:
            self._register_image_caches()
        except Exception as e:
            log.warning(f"Could not register the image caches with the budget: {e}")

        # Heterogeneous registry: the value list's element type is fixed by the
        # key (Input.Key -> ControllerKey, Input.Dial -> ControllerDial,
        # Input.Touchscreen -> ControllerTouchScreen), a key-dependent relation
        # the type system cannot express for a plain dict. `list[Any]` states
        # that honestly rather than naming one element type and lying about the
        # other two; the base-typed view is get_inputs(), which is annotated.
        self.inputs: dict[type[InputIdentifier], list[Any]] = {}
        for i in Input.All:
            self.inputs[i] = []
        self.init_inputs()

        self.background = Background(self)

        self.deck.set_key_callback(self.key_event_callback)
        self.deck.set_dial_callback(self.dial_event_callback)
        self.deck.set_touchscreen_callback(self.touchscreen_event_callback)

        # Unified write-error/resume-repaint state (plan §4 M2). Touched only
        # from the media thread (_on_write_result from the task classes'
        # run() and _exec_set_brightness; _run_pending_repaint and the
        # guard's except path from the run loop) -- no lock needed, single
        # writer. MUST be initialized before the media thread starts: its
        # very first iteration dereferences _full_repaint_pending. (The loop
        # is guarded now, so an AttributeError here no longer
        # kills the writer, but it would still fail every tick until this
        # init won the race; keep the order.)
        self._had_write_failure: bool = False
        self._full_repaint_pending: bool = False
        self._last_full_repaint_ts: float = 0.0

        # Start media player thread
        self.media_player = MediaPlayerThread(deck_controller=self)
        self.media_player.start()
        # Register the sole expected device writer for the owner-assertion
        # tooling (DECKARD_ASSERT_DEVICE_OWNER; BetterDeck.py). A
        # no-op unless that env var is set -- harness/dev tooling only.
        self.deck.set_expected_writer(self.media_player)

        # Bounded thread pool for action callbacks (tick/update/ready/event),
        # sized so every input can run its on_tick concurrently.
        total_inputs = sum(len(inputs) for inputs in self.inputs.values())
        # Nulled by close() step 9 (asserted by scenario_deck_close), so every
        # reader either getattr-defaults or None-checks before submitting.
        self.action_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
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
        self.load_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
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
        self.last_manual_loaded_page_path: str | None = None

        deck_settings = self.get_deck_settings()

        # None so the first set_brightness() below always writes to the device,
        # even when the stored value equals the skip-write guard's default.
        self.brightness = None
        brightness = deck_settings.get("brightness", {}).get("value", 75)
        self.set_brightness(brightness)

        # self.rotation = 270
        # rotation = deck_settings.get("rotation", {}).get("value", self.rotation)
        # self.set_rotation(rotation)


        # If screen is locked start the screensaver - this happens when the deck gets reconnected during the screensaver
        if gl.screen_locked and gl.settings_manager.app().lock_on_lock_screen:
            self.allow_interaction = False
            self.screen_saver.show()
        else:
            self.load_default_page()

    def init_inputs(self):
        # Build-then-swap: the media writer reads
        # self.inputs concurrently; filling a live dict in place gives it an
        # empty/partial view (the screensaver-entry KeyError window). Build
        # complete, then publish with one GIL-atomic assignment.
        new_inputs = {}
        for i in Input.All:
            new_inputs[i] = []
            input_class = getattr(sys.modules[__name__], i.controller_class_name)

            for k in input_class.Available_Identifiers(self.deck):
                controller_input = input_class(self, Input.FromTypeIdentifier(i.input_type, k))
                # Stamp with the current generation so paints from freshly built
                # inputs (e.g. the screensaver's) aren't dropped as stale.
                controller_input.config_gen = self._page_load_generation
                new_inputs[i].append(controller_input)
        self.inputs = new_inputs

    def get_inputs(self, identifier: InputIdentifier) -> list["ControllerInput"]:
        input_type = type(identifier)
        if input_type not in self.inputs:
            raise ValueError(f"Unknown input type: {input_type}")
        return self.inputs[input_type]

    # The registry's key-dependent value type (see self.inputs) IS expressible
    # here, where a concrete identifier class is in hand -- so every caller that
    # asks for a dial gets a ControllerDial back instead of the base class.
    @overload
    def get_input(self, identifier: Input.Key) -> "ControllerKey | None": ...

    @overload
    def get_input(self, identifier: Input.Dial) -> "ControllerDial | None": ...

    @overload
    def get_input(self, identifier: Input.Touchscreen) -> "ControllerTouchScreen | None": ...

    @overload
    def get_input(self, identifier: InputIdentifier) -> "ControllerInput | None": ...

    def get_input(self, identifier: InputIdentifier) -> "ControllerInput | None":
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
                    # Initial DEVICE paint for opaque keys: the
                    # per-frame video loop deliberately never repaints keys
                    # whose composed color is fully opaque (their tile hides
                    # the video, nothing changes frame-to-frame) -- but that
                    # also meant they never received their FIRST paint after
                    # switching onto a bg-video page: the device kept showing
                    # the previous page's content there until a keypress. An
                    # opaque tile hides the video, so this write cannot
                    # disturb it. update() paints BOTH the device and the app
                    # preview, so opaque keys don't also need the UI-only
                    # mirror below (which would be a redundant second push).
                    state = i.get_active_state()
                    if state is not None and state.background_manager.get_composed_color()[-1] >= 255:
                        i.update()
                        continue
                except Exception:
                    log.exception(f"Opaque-key initial paint failed for {i.identifier}")
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
        # decode (bounded) so keys composite over the new background. Blocking
        # the sole writer here is a recorded design tradeoff; the wait is
        # merely SLICED so a page switch that supersedes this one mid-decode
        # abandons it at the next slice instead of sitting out the remainder
        # of a 10s decode for a page we've already left (starving the new
        # page's paints). Total bound and the paint-anyway fallback unchanged.
        if not self._page_is_current(gen):
            return
        if bg_future is not None:
            deadline = time.time() + 10
            while True:
                try:
                    bg_future.result(timeout=0.5)
                    break
                except FutureTimeoutError:
                    if not self._page_is_current(gen):
                        return
                    if time.time() >= deadline:
                        log.warning("Background not ready before update_all_inputs; painting anyway")
                        break
                except Exception:
                    log.warning("Background not ready before update_all_inputs; painting anyway")
                    break
        self.update_all_inputs(gen=gen)
        # Inputs are loaded and painted (media tasks are FIFO, so
        # load_all_inputs has completed by the time this task runs) --
        # the sidebar can now render the NEW page's state objects.
        if self._page_is_current(gen):
            ui_port.get().on_page_changed(self)

    def animations_gated(self) -> bool:
        """Whether this deck's media loop should skip its animation section
        this tick because nobody is looking.

        Two terms:
          * the process-wide presence signal (`gl.presence_monitor`), which
            reports False forever in the default pause mode -- so this is
            False for everyone who has not opted in, and the loop behaves
            exactly as it did before this existed;
          * `not screen_saver.showing`. While the screensaver owns the deck,
            its animation IS the intended visible content -- the physical
            deck is visible even when the monitor is locked -- so the gate
            never applies to it. No per-page logic is
            needed beyond that: show() already released the underlying
            page's media, so a showing screensaver has nothing else left to
            decode.

        Read once per tick on the writer's critical path; both terms are
        plain attribute reads and neither takes a lock."""
        pm = getattr(gl, "presence_monitor", None)
        return pm is not None and pm.is_quiescent() and not self.screen_saver.showing

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
    
    def get_key_image_size(self) -> tuple[int, int]:
        if self._key_image_size is not None:
            return self._key_image_size
        if not self.get_alive():
            # Dead/closing deck: hand back the fallback without memoizing it, so
            # a deck that comes back is re-queried at its real size. This used to
            # `return` None, but no caller None-checks -- they unpack two ints or
            # pass the result straight to Image.new -- so a media tick racing an
            # unplug raised TypeError instead of drawing a stub tile.
            #
            # (72, 72) is the same constant the size-unavailable branch below
            # uses. It is NOT correct for every device -- an XL's keys are
            # 96x96 -- but this path only produces throwaway tiles for a deck
            # that can no longer be written to, and the real size is restored
            # the moment the deck answers again.
            return (72, 72)
        size = self.deck.key_image_format()["size"]
        if size is None:
            size = (72, 72)
        else:
            size = max(size[0], 72), max(size[1], 72)
        self._key_image_size = size
        return size

    def native_key_format_sig(self) -> tuple:
        """Hashable signature of the deck's native key image format. Part of
        every native tile cache key, so bytes encoded for one device format
        can never be served for another; memoized because the driver's
        format is fixed for the life of the device (the user-facing rotation
        is NOT part of it -- that lives on BetterDeck and is keyed
        separately)."""
        if self._native_key_format_sig is None:
            fmt = self.deck.key_image_format()
            self._native_key_format_sig = (
                tuple(fmt["size"]), fmt["format"], tuple(fmt["flip"]), fmt["rotation"],
            )
        return self._native_key_format_sig

    def clear_encoded_key_caches(self) -> None:
        """Drops every cached native key image -- the pixel-hash encode memo
        AND the frame-identity native tiles. Called wherever the content
        those entries were encoded from is orphaned wholesale: a background
        content change, a rotation change, or teardown. getattr-guarded so a
        close() after a half-finished __init__ still sweeps what exists."""
        for cache_name in ("encode_memo", "native_tile_cache"):
            cache = getattr(self, cache_name, None)
            if cache is not None:
                cache.clear()

    def _register_image_caches(self) -> None:
        """Enrols this deck's two native-image caches with the process-wide
        budget. Labels carry the serial so a multi-deck rig's
        eviction/thrash logs are attributable; `totals()` sums per group, so
        the telemetry columns stay deck-agnostic."""
        serial = self.serial_number()
        cache_budget.register(self.encode_memo, label=f"encode_memo:{serial}")
        cache_budget.register(self.native_tile_cache, label=f"native_tiles:{serial}")

    def refresh_tile_cache_min_age(self, video: "BackgroundVideo" = None) -> None:
        """Retunes how long the native tile cache's entries are shielded from
        GLOBAL eviction, to the duration of the background video now playing
        (clamped to DEFAULT_MIN_AGE_S..MAX_MIN_AGE_S); back to the default
        when there is no video.

        Native tile entries are keyed per FRAME, so an entry is re-touched
        once per loop of the content, not once per media tick. Under a
        binding ceiling a flat 2 s min-age would therefore make a playing
        video's whole frame set eligible for eviction exactly one loop before
        it is needed again -- silently reinstating the per-frame encode this
        cache exists to remove. A closed video's frames become immediately
        eligible again, which is what we want: they are stale.

        frames/source_fps is only the loop period once the tile cache is
        BUILT. While it is still building, get_next_tiles() advances the
        frame sequentially -- one frame per media tick, deliberately, so no
        frame is skipped and no seek is forced -- and the media tick can be
        slower than the source's fps, sometimes far slower on a heavy page.
        The real loop period is then unknown and strictly longer than
        frames/fps, so installing that number would under-protect exactly the
        frame set the build is racing to fill. The clamp maximum goes in
        instead, and the first tick past completion (BackgroundVideo.
        get_next_tiles) calls back in here for the real value. The wrong
        direction to err in is the short one: over-protection costs a stale
        entry surviving a pass, under-protection costs the re-encode."""
        min_age = cache_budget.DEFAULT_MIN_AGE_S
        if video is not None:
            min_age = cache_budget.MAX_MIN_AGE_S
            try:
                if video.is_cache_complete():
                    fps = float(video.get_source_fps() or getattr(video, "fps", 0) or 0)
                    frames = int(getattr(video, "n_frames", 0) or 0)
                    if fps > 0 and frames > 0:
                        min_age = max(cache_budget.DEFAULT_MIN_AGE_S,
                                      min(cache_budget.MAX_MIN_AGE_S, frames / fps))
            except Exception:
                min_age = cache_budget.MAX_MIN_AGE_S
        cache = getattr(self, "native_tile_cache", None)
        if cache is not None:
            cache_budget.set_min_age(cache, min_age)

    def get_touchscreen_image_size(self) -> tuple[int, int]:
        if self._touchscreen_image_size is not None:
            return self._touchscreen_image_size
        if not self.get_alive():
            # Same dead-deck contract (and same fallback-only caveat) as
            # get_key_image_size: callers unpack two ints, none None-checks.
            return (800, 100)
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
                # Load the requested page if it's different from the current
                # one. Snapshot + None-guard: active_page can be None here (a
                # racing close()/clear, or the load above deferred by a
                # showing screensaver) -- no current page means the requested
                # one is trivially "different", so proceed with the load.
                active_page = self.active_page
                if active_page is None or os.path.abspath(requested_page_path) != os.path.abspath(active_page.json_path):
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
        # loop defaults ON: a screensaver video/GIF whose config
        # predates the loop toggle used to play exactly one pass and then hold
        # its last frame on-device for the whole idle window -- a frozen deck,
        # never what "screensaver" means. True is also what every media-layer
        # default already is (ScreenSaver.loop, Background.set_from_path/
        # prebuild_from_path, BackgroundVideo/GifBackground). The settings
        # UI's read defaults match this, so the toggle never shows OFF while
        # the media actually loops. The page background keeps loop=False by
        # default -- a one-shot page-entry flourish is a real use there.
        self.screen_saver.set_loop(config.get("loop", True))
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
        if executor is None:
            # close() nulls the pools; a load that started before it has
            # nothing left to submit onto. Bail like the per-submit
            # RuntimeError guard below does -- reaching .submit() on None
            # raised AttributeError, which that guard does not catch.
            return
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

    def take_pending_screensaver_page(self) -> "Page | None":
        """Pops the page recorded by load_page's screensaver guard; None when
        no page change arrived while the screensaver was showing."""
        pending = self._screensaver_pending_page
        self._screensaver_pending_page = None
        return pending

    def load_input_from_identifier(self, identifier: InputIdentifier, page: Page, update: bool = True):
        controller_input = self.get_input(identifier)
        if controller_input is not None:
            self.load_input(controller_input, page, update)

    def load_input(self, controller_input: "ControllerInput", page: Page, update: bool = True):
        input_dict = controller_input.identifier.get_config(page)
        controller_input.load_from_input_dict(input_dict, update)

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

        # Sweep the background under _background_load_lock (a known
        # residual): a load_background already inside set_from_path holds this
        # lock while it attaches a fresh BackgroundVideo. Without taking it
        # here, the sweep could run BETWEEN that load's gen-gate and its
        # attach, leaving the fresh cv2 capture on self.background.video after
        # the sweep -- leaked until process exit. Taking it makes the sweep
        # wait for any in-flight attach; that attach is itself now suppressed
        # (apply_prebuilt's _closing re-check), so the lock guarantees we
        # observe and release the FINAL background object, not a stale None.
        with self._background_load_lock:
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

            # Reset every key's pressed visual BEFORE the generation bump:
            # press_state lives on the reused ControllerKey and
            # survives the page swap, so the key that triggered this switch
            # (still physically down) had every new-page render composed
            # through is_pressed() -> shrink_image(), and the release's
            # repaint can lose the enqueue race against a loader render that
            # read press_state just before the UP landed -- leaving the new
            # page's key stuck "pressed". Ordering is the point: renders read
            # config_gen at the start of update() and press_state later (at
            # composite time), so writing press_state=False before the bump
            # guarantees any render stamped with the new generation composes
            # unpressed. Gesture bookkeeping (down_start_time, hold timer,
            # the DOWN-time action snapshot) is deliberately untouched: the
            # physical release must still dispatch its events.
            for controller_key in self.inputs.get(Input.Key, []):
                controller_key.press_state = False

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

            # UI sync is NOT triggered here: at this point the new page's
            # input states and actions don't exist yet, so a sidebar rebuild
            # would render the OLD page's data with nothing correcting it
            # later. It fires from the load-completion side instead -- see
            # _queue_ui_page_sync callers (end of the awaited input load on
            # the media thread, and after initialize_actions below).

            bg_future = None
            if load_background:
                # Decode the background off the media thread so it overlaps input
                # loading; the update task below awaits it before keys composite.
                from src.backend.main_loop import run_in_background
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

        # Second completion signal: action_objects now exist, so the
        # sidebar's ActionManager can render the new page's actions. The
        # port coalesces this with the media-thread trigger above; each
        # callback renders the live state, so the later completion wins.
        ui_port.get().on_page_changed(self)

        # Notify plugin actions. `page.json_path`, not active_page (same
        # rationale as initialize_actions above): a racing switch or close()
        # can swap/null active_page after the lock released, and the deref
        # would AttributeError into @log.catch -- silently skipping this
        # signal and the DBus notify for a switch that DID happen.
        gl.signal_manager.trigger_signal(Signals.ChangePage, self, old_path, page.json_path)

        # Notify DBus API of the page change
        notify_active_page_changed(self.serial_number(), page.get_name())

        log.info(f"Loaded page {page.get_name()} on deck {self.deck.get_serial_number()}")
        self.maybe_collect_garbage()

    # Minimum seconds between post-load garbage collections, so rapid page
    # switching doesn't pay a full GC pause on every single switch. Adopted
    # from upstream "Improve page swich speeds" (88d632cc).
    GC_MIN_INTERVAL = 10.0

    def maybe_collect_garbage(self):
        now = time.time()
        if now - self._last_gc_time < self.GC_MIN_INTERVAL:
            return
        self._last_gc_time = now
        gc.collect()

    def reload_page(self):
        self.load_page(
            page=self.active_page,
            allow_reload=True
        )

    def set_brightness(self, value):
        value = min(100, max(0, value))
        if not self.get_alive(): return
        if value == self.brightness:
            # Unchanged: skip the queued device write. The device can stall
            # noticeably on a brightness write during an image-write burst.
            return
        # Routed through the media thread's control queue (plan §2.1) so the
        # device write happens on the sole writer, not the calling (GTK/
        # Timer/switch) thread. self.brightness is the last-commanded value,
        # not a hardware-confirmed one -- same caveat as before this change
        # (the old direct write had no error handling around it either).
        self.brightness = value
        self.media_player.submit_control(SetBrightnessMsg(value))

    def set_rotation(self, value):
        self.deck.set_rotation(value)
        # Rotation is part of both native cache keys, so nothing stale can
        # be served -- this is memory hygiene: every entry encoded for the
        # old rotation is dead the moment it changes.
        self.clear_encoded_key_caches()

        # The UI rebuilds its key grid for the new geometry. Synchronous when
        # we are already on the main loop (our only caller is), so the
        # load_page below repaints into the NEW grid rather than the
        # transposed old one.
        ui_port.get().on_deck_layout_changed(self)

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
            ticked_page = self.mark_page_ready_to_clear(False)
            # A showing screensaver gets no per-input work from this loop.
            # Its imagery lives in `background`: a video advances on the
            # media thread (update_tiles() plus the per-key
            # on_media_player_tick()), a still image is composited and
            # encoded by whichever thread called apply_prebuilt()/
            # set_from_path() and written once by the media thread. The
            # input set ScreenSaver.show() swaps in is freshly built by
            # init_inputs(): no action, no media, no label on any of it, and
            # ActionCore.get_is_present() refuses plugin writes for the
            # duration, so nothing here has state of its own to advance.
            #
            # Repainting every input once a second regardless is not free
            # and is not this loop's job. Recovery from a lost device write
            # -- including the blank a late-executing Clear leaves behind on
            # screensaver entry, which this repaint used to be the only cure
            # for -- belongs to the media loop's pending-full-repaint retry,
            # armed at the sites that lose the write (_on_write_result,
            # _exec_clear). Restoring the page on wake is hide()'s
            # load_page().
            if not self.screen_saver.showing:
                for t in self.inputs:
                    for i in self.inputs[t]:
                        i.get_active_state().own_actions_tick_threaded()

            # Reset the SAME page the False-call marked.
            self.mark_page_ready_to_clear(True, ticked_page)

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
    
    def get_key_by_coords(self, coords: tuple) -> "ControllerKey | None":
        index = self.coords_to_index(coords)
        return self.get_key_by_index(index)
    
    def get_key_by_index(self, index: int) -> "ControllerKey | None":
        keys = self.inputs.get(Input.Key, [])
        if index < 0 or index >= len(keys):
            return None
        return keys[index]

    def mark_page_ready_to_clear(self, ready_to_clear: bool, page: "Page" = None):
        """Marks (and returns) the page whose eviction-safety flag was set.
        Callers that bracket work between a False-call and a True-call MUST
        pass the page captured from the False-call back to the True-call:
        re-dereferencing active_page after the work marked whatever page a
        concurrent switch had installed, leaving the OLD page pinned
        ready_to_clear=False forever -- unevictable, silently shrinking the
        eviction budget."""
        if page is None:
            page = self.active_page
        if page is not None:
            page.ready_to_clear = ready_to_clear
        return page
    
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
    # Valid range for the saturation factor -- matches the UI scale
    # (DeckGroup.Saturation, min=1.0/max=1.5). A persisted value outside this
    # is corruption (or a hand-edit): clamp rather than trust it, because the
    # factor also becomes part of the fitted-bg / tile-cache key and a poison
    # value (NaN, inf) there is a key that never matches -> a cache that never
    # hits and re-enhances every composite.
    MIN_DISPLAY_SATURATION = 1.0
    MAX_DISPLAY_SATURATION = 1.5

    def _read_display_saturation(self) -> float:
        try:
            value = float(
                self.get_deck_settings().get("display", {}).get(
                    "saturation", self.DEFAULT_DISPLAY_SATURATION
                )
            )
        except (TypeError, ValueError):
            return self.DEFAULT_DISPLAY_SATURATION
        # float() accepts "nan"/"inf" without raising: reject non-finite so a
        # poison value can't reach an ImageEnhance factor or a cache key.
        if not math.isfinite(value):
            return self.DEFAULT_DISPLAY_SATURATION
        return min(self.MAX_DISPLAY_SATURATION, max(self.MIN_DISPLAY_SATURATION, value))

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
        if abs(value - self.display_saturation) <= 0.001:
            # Same-value echo -- notably the settings pane re-emitting the
            # loaded value on open (DeckGroup's Saturation.load_default is
            # deferred to map, so its set_value() fires the already-connected
            # value-changed handler): persisting + a full page reload would
            # be a pure no-op with a visible flicker, so skip entirely.
            return
        deck_settings = self.get_deck_settings()
        deck_settings.setdefault("display", {})["saturation"] = value
        gl.settings_manager.save_deck_settings(self.deck.get_serial_number(), deck_settings)

        self.display_saturation = value

        if self.active_page is not None:
            self.load_page(self.active_page, allow_reload=True)
    
    def get_own_deck_stack_child(self):
        """Deprecated in-process shim: kept for out-of-tree plugins.

        The engine no longer caches or resolves widgets -- the attached UI
        owns the controller->child binding (by object identity at add_page
        time, never by matching a re-read serial against a stack
        child's name). Returns None when no UI is attached.
        """
        return ui_port.get().query_deck_widget(self, "deck_stack_child")

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

    def clear(self, expects_repaint: bool = False) -> None:
        """Gen-agnostic async clear: submits a seq-stamped ClearMsg to the
        media thread's control queue instead of writing directly (plan
        §2.1). The seq stamp orders this against in-flight/future frame
        submissions: tasks already queued with a lower submit_seq are wiped,
        tasks submitted after this call (even same tick) survive and paint
        afterward -- preserving the caller's clear-then-paint order as
        blank-then-content on the device.

        Pass expects_repaint=True when this clear is the blank half of a
        blank-then-paint transition, so that a Clear which executes after
        its own paints already landed can be recovered from instead of
        leaving the deck blank (see MediaPlayerThread._exec_clear). Leave it
        False when a blank deck is the intended end state."""
        seq = self.media_player.next_submit_seq()
        self.media_player.submit_control(ClearMsg(seq=seq, expects_repaint=expects_repaint))

    def get_own_key_grid(self):
        """Deprecated in-process shim -- see get_own_deck_stack_child."""
        return ui_port.get().query_deck_widget(self, "key_grid")
    
    def clear_media_player_tasks(self, gen=None):
        # Skip the clear when a newer page load has superseded this one, so a late
        # clear can't wipe the newer load's freshly-queued tasks (stranding). The
        # lock spans check AND clear so a generation bump can't land mid-clear.
        with self._page_gen_lock:
            if gen is not None and gen != self._page_load_generation:
                return
            self.media_player.tasks.clear()
            # Under the writer's slot lock so this can't interleave with the
            # drain's read-then-null or a producer's assignment.
            with self.media_player._slot_lock:
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
        dedicated daemon thread (not the shared main_loop pool, which quit's
        shutdown_background_pool() would cancel mid-close). `app_quit=True`
        is the one case that's expected to run synchronously on main: it
        skips step 6 entirely (no plugin hooks to block on), and on_quit's
        6s force-quit timer is the backstop for everything else here.

        `remove_media` gates step 7's resource sweep (background/input media
        + caches); the rest of the sequence (device/thread/registration
        teardown) always runs.
        """
        # Locked compare-and-set: two teardown callers
        # (USB unplug thread vs. app-quit main thread) racing the unlocked
        # check-then-set could both pass the gate and run the whole sweep
        # concurrently -- duplicate plugin on_removed hooks, double device
        # close. Only the transition is under the lock; the sweep itself
        # stays unlocked (it can block on plugin hooks).
        with self._close_lock:
            if self._closing:
                return
            self._closing = True

        # Invalidate any in-flight page load NOW: a load_page
        # that already passed the _closing gate could otherwise attach a
        # fresh BackgroundVideo (cv2 capture + registry ref + possible
        # builder thread) AFTER step 7's resource sweep -- leaked until
        # process exit. The generation bump makes load_background /
        # load_all_inputs / the awaiting-update task abort at their gen
        # checks; cancelling the future covers the not-yet-started decode.
        page_gen_lock = getattr(self, "_page_gen_lock", None)
        if page_gen_lock is not None:
            with page_gen_lock:
                self._page_load_generation += 1
        bg_future = getattr(self, "_bg_future", None)
        if bg_future is not None:
            bg_future.cancel()

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
        #
        # Bounded: a wedged plugin teardown hook (pulsectl
        # precedent) used to strand this thread inside step 6 forever --
        # steps 7-9 (media sweep, fallback deck.close, deregistration) never
        # ran, the unplug leak returned, and _closing=True made a retry a
        # permanent no-op. On timeout the daemon hook thread is deliberately
        # abandoned: completing device/registration teardown matters more
        # than waiting out a hook that may never return.
        #
        # The abandoned-thread residual is inherent to abandon-on-timeout: a
        # thread we stop join()ing may still be running when steps 7-9 (and a
        # later GC) proceed. The surface is narrow by construction -- the wedge
        # is a plugin hook, and plugin hooks run in _teardown_actions's FIRST
        # step, clear_action_objects (ActionCore.teardown), which is BEFORE its
        # screensaver-input/background cleanup. So an abandoned thread is
        # parked in clear_action_objects; it has not reached (and will not
        # reach, while wedged) the close_resources()/original_inputs.clear()
        # that step 7 also touches. The only state it can still mutate is the
        # action_objects step 8's discard_controller drops anyway. Not worth a
        # guard; documented so a future change to _teardown_actions's ordering
        # (moving resource cleanup ahead of the hooks) knows it would widen it.
        if not app_quit:
            teardown_thread = threading.Thread(
                target=self._teardown_actions,
                name=f"DeckCloseTeardown-{getattr(self, '_serial_number', None) or '?'}",
                daemon=True,
            )
            teardown_thread.start()
            teardown_thread.join(self.TEARDOWN_JOIN_TIMEOUT_S)
            if teardown_thread.is_alive():
                log.error(
                    f"close(): action teardown still running after "
                    f"{self.TEARDOWN_JOIN_TIMEOUT_S:.0f}s -- a plugin teardown "
                    f"hook is wedged; abandoning it and completing "
                    f"device/registration teardown"
                )

        # Step 7: resource sweep. The writer is stopped, so nothing races a
        # paint touching these caches/objects concurrently.
        if remove_media:
            try:
                self.close_image_ressources()
            except Exception:
                log.opt(exception=True).warning("Failed to close image resources during close()")
            self.clear_encoded_key_caches()
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
        # A page change deferred while the screensaver was showing would
        # otherwise keep its whole page object graph pinned on this dead
        # controller. Teardown-only: the pending mechanism itself (and
        # active_page while a screensaver shows) is deliberately untouched.
        self._screensaver_pending_page = None

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

        self.image: "BackgroundImage | None" = None
        self.video: "BackgroundVideo | None" = None

        # Extend the background onto the touchscreen strip (SD+). For static
        # images the slice is memoized because the strip re-composites on
        # every dial label change; for videos update_tiles() refreshes
        # _video_strip once per frame.
        self.extend_to_touchscreen: bool = False
        self._touchscreen_slice: Image.Image | None = None
        self._video_strip: Image.Image | None = None

        # Read-only view: update_tiles() replaces the whole list (from sources
        # that yield either all-Image or, on the video cache's defensive path,
        # None entries) and nothing mutates it in place, so Sequence is both
        # accurate and the only way to accept both element types.
        self.tiles: Sequence[Image.Image | None] = [None] * deck_controller.deck.key_count()
        # (tiles, (video md5, frame index)) for the frame `tiles` holds, or
        # None for anything whose frame can't be named -- see
        # get_identified_tile().
        self._identified_tiles: tuple | None = None

    def set_image(self, image: "BackgroundImage", update: bool = True) -> None:
        self.image = image
        if self.video is not None:
            self.video.close()
        self.video = None
        self._touchscreen_slice = None
        self._video_strip = None
        # mem-plan P2.5: a content change orphans every cached native --
        # every entry was keyed against the OLD background's composited
        # pixels/hashes (or its frames). Left uncleared, a full memo from
        # the previous background would simply sit there dead until LRU
        # eviction happened to churn through it.
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
        # mem-plan P2.5: see set_image()'s comment -- same reasoning applies
        # to a video-to-video (or image-to-video) content change. The md5 in
        # a native tile key already makes a source swap collision-free; the
        # clear is what stops the old video's frames lingering.
        self._identified_tiles = None
        self.deck_controller.clear_encoded_key_caches()
        # The new video's loop duration is what its frame entries must be
        # shielded for; see refresh_tile_cache_min_age.
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
            # Refreshed by update_tiles() once per video frame; None unless
            # the video was built with extend_touchscreen.
            return self._video_strip
        image = self.image
        if image is None or not self._extend_effective():
            return None
        if self._touchscreen_slice is None:
            self._touchscreen_slice = image.get_touchscreen_image()
        return self._touchscreen_slice

    def prebuild_from_path(self, path: str | None, fps: int = 30, loop: bool = True, allow_keep: bool = True):
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
                    # (Holds for GifBackground too -- it carries the same
                    # three attributes; and for a GIF that fell back to the
                    # cv2 path below, keeping the fallback avoids re-paying
                    # the failed PIL decode attempt on every transition.)
                    return ("keep", path)
            if os.path.splitext(path)[1].lower() == ".gif":
                # .gif diverts to the PIL provider so alpha and the
                # per-frame delay timeline survive (cv2's demuxer drops
                # both). Over budget, or undecodable by PIL: fall back to
                # the EXISTING cv2 path below -- opaque, source-fps,
                # today's behavior -- rather than risk an OOM. One warning
                # per construction; the keep-check above stops it repeating
                # while the fallback stays loaded.
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
        """Release the resources a prebuilt-but-never-applied payload holds
        (a known residual): a "video"/"image" payload already opened its
        cv2 capture / retained its PIL image in prebuild_from_path. Dropping
        the object without closing it leaks that handle. "keep"/"noop"/"blank"
        carry no fresh resource, so they are no-ops here."""
        if kind not in ("video", "image") or payload is None:
            return
        try:
            payload.close()
        except Exception:
            log.opt(exception=True).warning(
                "Failed to close an orphaned prebuilt background payload during close()"
            )

    def apply_prebuilt(self, kind: str, payload, fps: int = 30, loop: bool = True, update: bool = True) -> None:
        """Phase-2 counterpart to prebuild_from_path(): performs the actual
        swap. Callers that need the lock-free/locked split (the screensaver
        transition, plan §4 M3) call this under _background_load_lock with a
        generation re-check already done; no file I/O happens here, only
        object assignment + the same update_all_inputs() fan-out set_video/
        set_image already trigger."""
        # Authoritative close-vs-load guard (a known residual): a
        # load_background that already passed load_background's
        # _page_is_current(gen) gate before close() bumped the generation is
        # in-flight HERE with a freshly prebuilt payload -- prebuild_from_path
        # already opened its cv2 capture / retained its image. If it attached
        # now, close()'s step-7 sweep (which already ran, or is blocked on
        # _background_load_lock waiting for us) would never see it and it would
        # leak until process exit. _closing is set at the very top of close(),
        # before the sweep, so re-checking it here catches every ordering.
        # Release the orphaned payload's resources instead of dropping it on
        # the floor.
        if getattr(self.deck_controller, "_closing", False):
            self._discard_prebuilt(kind, payload)
            return
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

    def set_from_path(self, path: str | None, fps: int = 30, loop: bool = True, update: bool = True, allow_keep: bool = True) -> None:
        """Synchronous convenience wrapper (prebuild + apply in one call) for
        callers that don't need the lock-free/locked split -- load_background
        (already under _background_load_lock itself) and ScreenSaver's
        setters that act while already showing (plan §4 M3)."""
        kind, payload = self.prebuild_from_path(path, fps=fps, loop=loop, allow_keep=allow_keep)
        self.apply_prebuilt(kind, payload, fps=fps, loop=loop, update=update)

    def get_identified_tile(self, key_index: int) -> tuple | None:
        """(tile, (video md5, frame index)) for a video background, or None
        when there is no tile whose frame can be named (image/blank
        background, mid-rebuild, fallback frame). Tiles and identity are
        published as ONE pair and handed out as one read, so a concurrent
        update_tiles() can never let a caller pair this frame's pixels with
        the next frame's identity -- update_tiles() runs on the media tick
        but also on the GTK/screensaver threads (set_image/set_video/
        apply_prebuilt), so the media thread is not its only writer."""
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
        # Old tiles are reclaimed by refcounting once unreferenced; closing them
        # here would race a concurrent composite still holding one.
        try:
            identity = None
            if self.image is not None:
                self.tiles = self.image.get_tiles(extend_touchscreen=self._extend_effective())
            elif self.video is not None:
                # An extended video frame carries the strip slice as one extra
                # entry after the key tiles (see BackgroundVideoCache).
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
            # A tile error must not kill the media thread; keep the old tiles.
            # Rate-limited: a broken video would otherwise log every frame.
            now = time.time()
            if now - getattr(self, "_last_tile_error_log", 0) > 10:
                self._last_tile_error_log = now
                log.opt(exception=True).error("Failed to update background tiles; keeping previous")

class BackgroundImage:
    def __init__(self, deck_controller: DeckController, image: Image.Image, path: str | None = None) -> None:
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
        # Nulled by close(); _ensure_fits_canvas/create_full_deck_sized_image
        # both handle the released state.
        self.image: Image.Image | None = self._fit_to_canvas(image, self._extend_effective())

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

    def create_full_deck_sized_image(self, extend_touchscreen: bool = False) -> Image.Image:
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

        # close() releases the source image. Raise rather than compose a
        # transparent canvas: Background.update_tiles catches this and KEEPS the
        # previous tiles behind a rate-limited log, so the failure stays loud and
        # self-preserving. Returning a blank canvas here would silently blank
        # every key instead (and do it once per tile refresh, unlogged).
        source = self.image
        if source is None:
            raise RuntimeError(
                "background image was released (close()) while its tiles were "
                "still being composed"
            )

        # Convert to RGBA first to preserve transparency, then resize
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

        self.page: Page | None = self.deck_controller.active_page

        self.active_frame: int = -1
        self._play_start: float | None = None  # wall-clock playback start, set on first real-time frame
        self._last_frame_tick: float | None = None  # last real-time frame pick, for gap clamping
        # Whether the tile cache's min-age has been retuned to this video's
        # real loop period. False until the first tick after the cache
        # completes: before that, playback is not running at source fps and
        # the loop period is not knowable (refresh_tile_cache_min_age).
        self._min_age_synced: bool = False

        super().__init__(video_path, deck_controller=deck_controller, extend_touchscreen=extend_touchscreen)

    def get_next_tiles(self) -> tuple[list[Image.Image | None], tuple | None]:
        """(tiles, identity) for the frame this tick lands on, where identity
        is (video md5, source frame index) or None when the tiles' frame
        can't be named (fallback/alpha payload). Returned as one pair so a
        caller can never file one frame's pixels under another's identity --
        the frame actually served is not always the one asked for (see
        Mp4FrameCache.get_frame_and_index)."""
        if self.is_cache_complete():
            if not self._min_age_synced:
                # First tick past cache completion. Up to here the frame set
                # was shielded by the conservative clamp maximum, because
                # sequential build playback has no knowable loop period; from
                # here frames are picked by wall clock at source fps, so the
                # real one can go in. One bool test per tick to get it.
                self._min_age_synced = True
                self.deck_controller.refresh_tile_cache_min_age(self)
            # Cache built -> any frame is a free lookup. Pick it by wall-clock so a
            # slow media loop drops frames (stays real-time) instead of playing the
            # video in slow-motion. Playback runs at the SOURCE's fps -- the
            # page's fps setting only limits how often the media loop renders
            # a new frame (the tick divider in MediaPlayerThread.run), it must
            # not change playback speed.
            playback_fps = float(self.get_source_fps() or self.fps or 30)
            now = time.time()
            if self._play_start is None:
                # Seed the timebase from the current position, not zero: the cache
                # completes mid-play (sequential decode or async disk load), and a
                # zero base would replay a non-looping video / jump a looping one.
                self._play_start = now - (self.active_frame + 1) / playback_fps
            elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
                # Ticks stop while the page is away; shift the timebase across the
                # gap so playback resumes in place instead of fast-forwarding.
                self._play_start += (now - self._last_frame_tick) - 1.0 / playback_fps
            self._last_frame_tick = now
            frame = int((now - self._play_start) * playback_fps)
            self.active_frame = frame % self.n_frames if self.loop else min(frame, self.n_frames - 1)
        else:
            # Still decoding into the cache: advance sequentially so every frame is
            # decoded (wall-clock jumps would leave gaps and force expensive seeks).
            self.active_frame += 1
            if self.active_frame >= self.n_frames and self.loop:
                self.active_frame = 0

        frame_index: int | None
        copied_tiles: list[Image.Image | None]
        tiles, frame_index = self.get_tiles_and_index(self.active_frame)
        try:
            # Defensive: every path through get_tiles_and_index() currently
            # yields real Images (decoded tiles, the last good payload, or
            # the alpha fallback), so this only fires if a future cache
            # substitutes None for a tile it could not decode.
            copied_tiles = [tile.copy() for tile in tiles]
        except AttributeError:
            copied_tiles = [None for _ in range(len(tiles))]
            frame_index = None
        identity = None if frame_index is None else (self.video_md5, frame_index)
        return copied_tiles, identity


#: extend_touchscreen implies the strip geometry was computed in __init__.
#: If that ever breaks, raising keeps Background.update_tiles' contract --
#: previous tiles retained, one rate-limited log -- instead of publishing a
#: key-only tile set that would leave the strip frozen and silent.
_STRIP_GEOMETRY_MISSING = (
    "extend_touchscreen is set but the strip geometry was never computed"
)


class GifBudgetExceeded(Exception):
    """Raised by decode_gif_frames when the estimated decoded footprint
    exceeds the caller's budget, BEFORE any frame is decoded -- callers fall
    back to a bounded path (GifBackground -> the existing cv2/mp4 pipeline)
    instead of risking an OOM on a pathological many-frame GIF."""


# RAM ceiling for a fully-decoded GIF background. One constant,
# no new cache layer: GifBackground estimates n_frames x W x H x 4 against it
# at open and falls back to the opaque cv2 path when over. 128MB covers
# ~200 canvas frames on an SD+ (~600KB each) / ~90 on an XL (~1.4MB each) --
# generous for the looping decorations this feature targets, small next to
# what the old unbounded cv2 canvas pool used to swallow (mem-plan §3).
GIF_BG_BUDGET_MB = 128

# Per-GIF ceiling for a KEY's retained RGBA frame list. Only
# alpha-carrying GIFs build one at all -- opaque ones play off the mp4 tile
# cache at O(1) RAM -- so this bounds the exception, not the common case.
# 32MB is ~220 frames at 2x an SD+ tile / ~110 at 2x an XL tile: far beyond
# the looping badges and spinners this feature targets, and small enough that
# a pathological page cannot outweigh the whole image-cache budget by itself.
GIF_KEY_BUDGET_MB = 32

# Cache-file variant for the over-budget ladder's artifact (see
# KeyGIF._cold_streaming_walk). Its frames have had alpha flattened away, so
# it must never be read back by a GIF that is now UNDER budget and could
# have kept its transparency -- a separate variant keeps the two renderings
# from ever sharing a file.
BOUNDED_TILE_VARIANT = ".bounded"

# Raw DECKARD_GIF_KEY_BUDGET_MB values already warned about, so a bad (or
# pathologically small) setting costs ONE log line per distinct value for the
# life of the process instead of one per GIF key per page load. Same
# once-per-distinct-value discipline as cache_budget._warned_ceiling_values.
_warned_gif_budget_values: "set[str]" = set()


def gif_key_budget_bytes() -> int:
    """Byte ceiling for one key GIF's retained frame list, from
    DECKARD_GIF_KEY_BUDGET_MB. 0 (or a negative value) disables the frame
    list entirely: every GIF then plays off the mp4 tile cache, trading alpha
    for a hard bound.

    A malformed value degrades to the default with a warning, per the
    house contract (native_tile_cache.native_tile_cache_max_bytes): this is
    read during page load, where an exception would surface as a key that
    silently lost its media -- a tuning knob typo must never cost content.
    "Malformed" includes the values float() ACCEPTS but int() cannot take
    ("nan", "inf", "1e400"): they parse, survive the sign test (every nan
    comparison is False), then raise from int().

    A sub-1-MiB budget also warns: it is smaller than a SINGLE fitted frame
    at any tile size, so every alpha GIF in the app silently loses its
    transparency to the bounded route. That is a legitimate setting (the
    knob is "bound this hard"), but it is never what a typo'd "0.5" meant."""
    raw = os.environ.get("DECKARD_GIF_KEY_BUDGET_MB")
    if raw is None:
        return GIF_KEY_BUDGET_MB * 1024 * 1024
    try:
        mb = float(raw)
        usable = math.isfinite(mb)
    except ValueError:
        usable = False
    if not usable:
        if raw not in _warned_gif_budget_values:
            _warned_gif_budget_values.add(raw)
            log.warning(
                f"Ignoring malformed DECKARD_GIF_KEY_BUDGET_MB={raw!r}; "
                f"using the default {GIF_KEY_BUDGET_MB}"
            )
        return GIF_KEY_BUDGET_MB * 1024 * 1024
    if mb < 0:
        return 0
    if 0 < mb < 1 and raw not in _warned_gif_budget_values:
        _warned_gif_budget_values.add(raw)
        log.warning(
            f"DECKARD_GIF_KEY_BUDGET_MB={raw!r} is under 1 MiB -- smaller than one "
            f"fitted GIF frame, so EVERY alpha-carrying GIF key will drop its "
            f"transparency for the bounded mp4 route"
        )
    return int(mb * 1024 * 1024)


def contained_size(source_size: "tuple[int, int]", max_size: "tuple[int, int]") -> "tuple[int, int]":
    """The dimensions ImageOps.contain lands on: aspect-preserving,
    SHRINK-ONLY (a source already inside max_size keeps its own size --
    upscaling would multiply retained memory for zero display benefit,
    mem-plan P2.3).

    "Lands on" to within a pixel, not bit-for-bit: ImageOps.contain rounds
    each axis independently (round(w * scale) with its own recomputed
    scale), so the two disagree by 1 px on one axis for a small minority of
    extreme aspect ratios (94 of 1.1M source/target pairs surveyed). The
    routes below share THIS function, so they cannot drift from each other;
    the residual is a 1-px difference from what a raw ImageOps.contain call
    elsewhere would produce.

    Shared by the frame-list decode (its pre-decode budget estimate) and the
    video route (the tile size it asks the mp4 registry to build), so a GIF's
    geometry cannot depend on which route it took."""
    src_w, src_h = source_size
    max_w, max_h = max_size
    if src_w <= max_w and src_h <= max_h:
        return src_w, src_h
    scale = min(max_w / src_w, max_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def tile_video_size(source_size: "tuple[int, int]", max_size: "tuple[int, int]") -> "tuple[int, int]":
    """The tile-cache geometry for a source that would be CONTAINED into
    max_size -- i.e. contained_size(), rounded down to even dimensions (and
    never below 2).

    Even because mp4v silently rounds odd dimensions down (a 133x144 writer
    produces 132x144 frames), which would leave every payload a pixel off
    the geometry that was asked for; the >= 2 floor because a 1-px axis has
    no even value below it. So a cached tile can be up to 1 px smaller per
    axis than the retained frame list's, and a pathologically thin source
    (3x100 -> 2x100) loses a third of its width -- accepted: the alternative
    is no cache at all for those, and compositing scales per tick anyway.

    Shared by every KeyGIF route so a GIF's geometry cannot depend on which
    one it took."""
    out_w, out_h = contained_size(source_size, max_size)
    return max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)


def normalize_gif_delay(raw: "int | None") -> int:
    """One frame's GIF `duration` metadata -> the delay actually played, in
    ms, normalized the browser way (Firefox/Chrome): missing or < 20ms ->
    100ms, anything else trusted as-is.

    The old "< 50 -> x10 centiseconds" heuristic played legitimate fast GIFs
    (40ms == 25fps) 10x too slow, and an all-zero-duration GIF made the
    cumulative timeline degenerate and froze on frame 0 (scenario_gif_delays
    pins both). One definition, shared by the full decode and the
    header-only probe, so the two can never disagree about a timeline."""
    if raw is None or raw < 20:
        return 100
    return raw


def cumulative_gif_delays(delays_ms: "list[int]") -> "list[float]":
    """Delay list (ms) -> cumulative timeline in seconds, where element i is
    the wall-clock time at which frame i's display window ENDS. Picking a
    frame for elapsed time t is then a single bisect instead of a per-tick
    increment-and-compare loop (KeyGIF.get_next_frame /
    GifBackground._pick_frame index it directly)."""
    return list(itertools.accumulate(d / 1000.0 for d in delays_ms))


@dataclass(frozen=True, slots=True)
class GifTimeline:
    """A GIF's playback timeline and geometry, with NO decoded frame
    retained: the frame count, the per-frame delays, the cumulative
    wall-clock edges, and the source dimensions.

    This is what a KeyGIF needs when the pixels come from somewhere else --
    a tile cache PIL already wrote on an earlier page load.
    Timing authority always lives here, never in the video container."""
    n_frames: int
    frame_delays: "list[int]"
    cum_delays: "list[float]"
    size: "tuple[int, int]"


def gif_header_geometry(path: str) -> "tuple[int, tuple[int, int]]":
    """(frame count, canvas size) from the GIF's headers alone -- no frame
    decoded, no seek. Enough to price a decode before paying for it."""
    gif = Image.open(path)
    try:
        return getattr(gif, "n_frames", 1), gif.size
    finally:
        gif.close()


def probe_gif_timeline(path: str) -> GifTimeline:
    """The playback timeline of the GIF at `path`, retaining NOTHING.

    Cost note: the frame count and the size come from headers alone, but PIL
    only exposes a frame's `duration` after seeking to it, and seeking
    composes the previous frame (GifImagePlugin._seek -> load()). So this
    walk does run the decoder -- what it never does is convert, fit,
    saturate or KEEP anything, which is where both the memory and the bulk
    of the time go: measured on a 200-frame 500x500 GIF, ~49 ms and O(1) RAM
    here against ~320 ms and the whole retained frame list for
    decode_gif_frames.

    Raises whatever PIL raises on a corrupt/truncated file -- callers treat
    that exactly like a failed decode (fail soft to the opaque video path)."""
    gif = Image.open(path)
    try:
        size = gif.size
        n_frames = getattr(gif, "n_frames", 1)
        delays_ms: "list[int]" = []
        for index in range(n_frames):
            gif.seek(index)
            delays_ms.append(normalize_gif_delay(gif.info.get("duration")))
    finally:
        gif.close()

    return GifTimeline(
        n_frames=len(delays_ms),
        frame_delays=delays_ms,
        cum_delays=cumulative_gif_delays(delays_ms),
        size=size,
    )


def frame_has_alpha(frame: Image.Image) -> bool:
    """Does this RENDERED frame actually carry transparency?

    The exact test, not the declared one. A GIF's header transparency index
    says almost nothing about the composited result: a survey of 396 real
    animated GIFs found 75% declaring a transparent index on frame 0 and
    only 11% ever rendering a pixel with alpha < 255 -- delta-encoding
    against the previous frame (disposal 1) declares the index purely to
    mean "leave this pixel alone". Routing on the declaration therefore
    stranded ~64% of real GIFs on the expensive path for nothing, which is
    why v2 asks the pixels instead.

    One C-level extrema pass per frame (min alpha over the band); the caller
    stops asking once the answer is yes."""
    if frame.mode != "RGBA":
        return False
    extrema = frame.getextrema()
    return len(extrema) >= 4 and extrema[3][0] < 255


def gif_frame_walk(path: str, max_size: "tuple[int, int]" = None,
                   fit_size: "tuple[int, int]" = None,
                   saturation: float = 1.0):
    """Generator over one GIF's frames: PIL composites each frame onto the
    running canvas (disposal, partial extents and all), converts to RGBA,
    sizes it, bakes the saturation, and yields `(frame, delay_ms)`.

    THE one GIF compositor in the app. Everything that shows GIF pixels --
    the retained frame list, GIF backgrounds, and the tile mp4 KeyGIF writes
    for an opaque GIF -- consumes this walk, so no two of them can disagree
    about what frame N looks like (the review found FFmpeg's own GIF demuxer
    disagreeing with PIL on 7 of 15 frames of a stock file).

    Sizing -- exactly one of the two, or neither:
      * max_size: shrink-only ImageOps.contain. A frame larger than this is
        contained (aspect preserved); a smaller one keeps its own size --
        upscaling a small GIF would multiply retained memory for zero
        display benefit (KeyGIF's 2x-tile policy, mem-plan P2.3).
      * fit_size: every frame is ImageOps.fit to EXACTLY this size.
        Backgrounds must fill the canvas so per-key crop coordinates hold --
        the same fill contract as the cv2 background cache's re-encode.

    The source handle is closed when the walk finishes OR when the consumer
    abandons it (generator close), so no fd or full-res PIL frame cache
    outlives the pass."""
    # The source file is only needed for the duration of the walk -- close it
    # as soon as it ends so the app doesn't hold a dangling fd + full-res
    # frame cache alive underneath the fitted copies a caller keeps.
    gif = Image.open(path)
    try:
        for frame in ImageSequence.Iterator(gif):
            decoded = frame.convert("RGBA")
            if fit_size is not None:
                if decoded.size != fit_size:
                    decoded = ImageOps.fit(decoded, fit_size, Image.Resampling.LANCZOS)
            elif max_size is not None and (decoded.width > max_size[0] or decoded.height > max_size[1]):
                decoded = ImageOps.contain(decoded, max_size)
            if abs(saturation - 1.0) > 0.001:
                decoded = ImageEnhance.Color(decoded).enhance(saturation)
            yield decoded, normalize_gif_delay(gif.info.get('duration'))
    finally:
        gif.close()


def decode_gif_frames(path: str, max_size: "tuple[int, int]" = None,
                      fit_size: "tuple[int, int]" = None,
                      saturation: float = 1.0,
                      budget_bytes: int = None) -> "tuple[list[Image.Image], list[int], list[float]]":
    """Every frame of the GIF at `path`, decoded to RGBA and retained, plus
    its delay timeline -- gif_frame_walk() materialized. GifBackground's
    entry point (KeyGIF drives the walk itself so it can decide, frame by
    frame, whether to keep what it is looking at).

    budget_bytes: pre-decode gate -- the retained footprint is estimated
    from the header (n_frames x out_w x out_h x 4) and GifBudgetExceeded is
    raised before decoding when it would exceed the budget.

    Returns (frames_rgba, frame_delays_ms, cum_delays) where cum_delays[i]
    is the wall-clock second at which frame i's display window ENDS (the
    callers' bisect timelines index it directly).
    """
    if budget_bytes is not None:
        n_frames, (out_w, out_h) = gif_header_geometry(path)
        if fit_size is not None:
            out_w, out_h = fit_size
        elif max_size is not None:
            # The dims ImageOps.contain would land on (shared with the
            # video route's tile size -- see contained_size).
            out_w, out_h = contained_size((out_w, out_h), max_size)
        estimate = n_frames * out_w * out_h * 4
        if estimate > budget_bytes:
            raise GifBudgetExceeded(
                f"{path}: ~{estimate / (1024 * 1024):.1f}MB decoded "
                f"({n_frames} frames at {out_w}x{out_h} RGBA) exceeds the "
                f"{budget_bytes / (1024 * 1024):.1f}MB budget"
            )

    frames: "list[Image.Image]" = []
    delays_ms: "list[int]" = []
    with contextlib.closing(gif_frame_walk(path, max_size=max_size, fit_size=fit_size,
                                           saturation=saturation)) as walk:
        for frame, delay in walk:
            frames.append(frame)
            delays_ms.append(delay)

    return frames, delays_ms, cumulative_gif_delays(delays_ms)


class GifBackground:
    """RGBA GIF provider for deck/strip backgrounds.

    Satisfies the contract BackgroundVideo (the cv2/mp4 canvas cache)
    exposes to the compositor -- get_next_tiles() -> (entries, identity)
    with the strip slice as one extra entry when extended, plus the
    video_path/extend_touchscreen/saturation/page/fps/loop attributes the
    prebuild keep-check, the media tick, and the screensaver setters read --
    but decodes through PIL so alpha and the per-frame delay timeline
    survive (cv2's GIF demuxer drops both).

    Frames are decoded once at construction, fitted to exactly the deck
    canvas (per-key crop coordinates must hold), and owned by this object:
    same lifetime as BackgroundVideo, freed with the background swap
    (Background.set_image/set_video close the old provider). No global
    cache, no registry -- two decks showing the same GIF decode it twice
    (accepted v1 trade-off; the mp4 tile cache's refcounted registry stays
    the only one). Construction raises GifBudgetExceeded before decoding
    when the estimated footprint exceeds GIF_BG_BUDGET_MB; callers fall
    back to the existing opaque cv2 path.

    canvas_size overrides the deck-canvas geometry for the strip-background
    route (ControllerTouchScreenState._get_background_video_frame): frames
    are fitted to the strip and served whole via get_next_frame() -- the
    per-touchscreen composite alpha_composites the frame, so the exact-size
    RGBA decode is load-bearing there.
    """

    def __init__(self, deck_controller: "DeckController", gif_path: str, loop: bool = True,
                 fps: int = 30, extend_touchscreen: bool = False,
                 canvas_size: "tuple[int, int] | None" = None) -> None:
        self.deck_controller = deck_controller
        self.video_path = gif_path
        self.loop = loop
        self.fps = fps

        self.page: Page | None = deck_controller.active_page
        self.saturation = deck_controller.get_display_saturation()

        deck = deck_controller.deck
        self.extend_touchscreen = extend_touchscreen and deck.is_touch()

        # Both stay None unless extend_touchscreen is on -- the strip geometry
        # only exists for the extended canvas. Every read is paired with that
        # flag; the None-guards at those reads make the pairing checkable.
        self.strip_size: "tuple[int, int] | None" = None
        self._strip_box: "tuple[int, int, int, int] | None" = None
        if canvas_size is None:
            # Same canvas/crop geometry as BackgroundVideoCache -- computed
            # once into plain boxes here since the frame list is fixed after
            # decode (no per-call geometry methods needed).
            key_rows, key_cols = deck.key_layout()
            self.key_count = deck.key_count()
            key_w, key_h = deck.key_image_format()['size']
            spacing_x, spacing_y = deck_controller.key_spacing

            canvas_w = key_w * key_cols + spacing_x * (key_cols - 1)
            canvas_h = key_h * key_rows + spacing_y * (key_rows - 1)

            self._key_regions: "list[tuple[int, int, int, int]]" = []
            for key in range(self.key_count):
                row, col = divmod(key, key_cols)
                x = col * (key_w + spacing_x)
                y = row * (key_h + spacing_y)
                self._key_regions.append((x, y, x + key_w, y + key_h))

            if self.extend_touchscreen:
                # Extend the canvas below the key grid so the frame
                # continues onto the touchscreen strip: one bezel gap plus
                # the strip mapped into canvas coordinates (same geometry as
                # BackgroundImage/BackgroundVideoCache).
                self.strip_size = deck_controller.get_touchscreen_image_size()
                strip_canvas_h = round(self.strip_size[1] * canvas_w / self.strip_size[0])
                canvas_h += spacing_y + strip_canvas_h
                self._strip_box = (0, canvas_h - strip_canvas_h, canvas_w, canvas_h)
            canvas_size = (canvas_w, canvas_h)
        else:
            # Strip-background mode: whole-frame service only.
            self.key_count = 0
            self._key_regions = []
            self.extend_touchscreen = False

        self.canvas_size = canvas_size

        self.frames, self.frame_delays, self._cum_delays = decode_gif_frames(
            gif_path, fit_size=canvas_size, saturation=self.saturation,
            budget_bytes=GIF_BG_BUDGET_MB * 1024 * 1024,
        )
        self._total_delay: float = self._cum_delays[-1] if self._cum_delays else 0.0

        # Frame identity for the passthrough-key native-encode memo
        # (Background.get_identified_tile consumers): (md5, frame index),
        # the exact BackgroundVideo contract, so steady-state loop playback
        # is a dict lookup + USB write per key.
        self.video_md5 = get_video_md5(gif_path)

        self.active_frame: int = -1
        # Wall-clock timeline state -- KeyGIF.get_next_frame's arithmetic
        # (see _pick_frame).
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None
        # (frame index, entries) of the last cropped frame: at loop FPS most
        # ticks land on the frame already cut (a 10fps GIF under a 30Hz tick
        # re-uses each crop set ~3x). Handed out as copies either way.
        self._tiles_memo: tuple = (None, None)

    def _pick_frame(self, now: float = None) -> int:
        """Wall-clock frame index for `now`: bisect over the cumulative
        delay timeline plus the away-gap clamp -- KeyGIF.get_next_frame's
        arithmetic kept in lockstep (see that method's comments for the
        rationale on each branch)."""
        # Snapshot the timeline once: close() swaps frames/_cum_delays/
        # _total_delay from another thread mid-call (deck route: GTK/
        # screensaver swap racing the media tick). Locals keep the guard and
        # the arithmetic looking at the SAME generation -- a racer can't
        # land a zero modulo or an emptied-list index between them. (The
        # caller indexes its own frames snapshot; every index returned here
        # comes from either the shared full timeline or the 0 fallback, so
        # it stays in range for that snapshot too.)
        cum = self._cum_delays
        total = self._total_delay
        n = len(self.frames)
        if n <= 1 or total <= 0 or not cum:
            self.active_frame = 0
            return 0

        if now is None:
            now = time.time()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            self._play_start += (now - self._last_frame_tick) - cum[0]
        self._last_frame_tick = now

        elapsed = now - self._play_start
        t = elapsed % total if self.loop else min(elapsed, total)

        frame = bisect.bisect_right(cum, t)
        if frame >= n:
            frame = n - 1  # float-edge / non-loop clamp landing on t == total
        self.active_frame = frame
        return frame

    def get_next_tiles(self) -> "tuple[list[Image.Image], tuple | None]":
        """(entries, identity) for the frame this tick lands on --
        BackgroundVideo.get_next_tiles's contract: key tiles, plus the strip
        slice as one extra entry when extended; identity is (md5, frame
        index). Crops are cut straight from the retained RGBA canvas frame
        (no paste onto an opaque intermediate, so alpha reaches the
        compositor) and handed out as copies: consumers paste onto the
        tiles/strip slice in place."""
        frames = self.frames  # snapshot: close() empties this from other threads
        strip_size = self.strip_size
        strip_box = self._strip_box
        if not frames:
            entries = [self.deck_controller.generate_alpha_key() for _ in range(self.key_count)]
            if self.extend_touchscreen:
                if strip_size is None:
                    raise RuntimeError(_STRIP_GEOMETRY_MISSING)
                entries.append(Image.new("RGBA", strip_size, (0, 0, 0, 0)))
            return entries, None

        index = self._pick_frame()
        memo_index, memo_entries = self._tiles_memo
        if memo_index != index or memo_entries is None:
            frame = frames[index]
            memo_entries = [frame.crop(box) for box in self._key_regions]
            if self.extend_touchscreen:
                if strip_size is None or strip_box is None:
                    raise RuntimeError(_STRIP_GEOMETRY_MISSING)
                # Bottom slice of the extended canvas at strip resolution
                # (same crop+HAMMING resize as BackgroundVideoCache.
                # crop_strip_from_deck_sized_image).
                memo_entries.append(
                    frame.crop(strip_box).resize(strip_size, Image.Resampling.HAMMING)
                )
            self._tiles_memo = (index, memo_entries)
        return [entry.copy() for entry in memo_entries], (self.video_md5, index)

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        """The whole canvas-size RGBA frame for now (strip-background
        route). Returns the retained frame itself -- the caller's
        convert("RGBA") copies before anything pastes onto it. None once
        close() has emptied the frame list."""
        frames = self.frames
        if not frames:
            return None
        return frames[self._pick_frame(now)]

    def set_playback(self, fps: int, loop: bool) -> None:
        """fps is only the owner's render cap here (playback position is
        wall-clock over the GIF's own delay timeline, mirroring InputVideo's
        natural_speed arm) -- no timebase rebase needed."""
        self.fps = fps
        self.loop = loop

    def close(self) -> None:
        """Drops the retained frame list (the whole footprint). Safe against
        an in-flight tick: get_next_tiles/get_next_frame snapshot
        self.frames, so a racer finishes on its own reference and the list
        is reclaimed right after."""
        self.frames = []
        self.frame_delays = []
        self._cum_delays = []
        self._total_delay = 0.0
        self._tiles_memo = (None, None)


class KeyGIF(SingleKeyAsset):
    """Animated-GIF provider for one key, playing its own per-frame delay
    timeline.

    Holds its frames one of two ways:

      * OPAQUE GIF -> the shared mp4 tile registry (mp4_tile_cache): one
        refcounted cache file per (source, size, saturation), one reader
        with one decoded frame in hand. RAM is O(1) per key instead of
        O(frame count) -- the whole point of the issue, since a 200-frame
        GIF is ~29MB retained and a page of them ~0.9GiB.
      * ALPHA-CARRYING GIF -> the retained RGBA frame list, the only way to
        keep transparency (an mp4 has no alpha channel).

    Two rules make that split safe, both learned the hard way in review:

    1. PIL IS THE ONLY COMPOSITOR. The tile mp4 is written FROM PIL-
       composited frames (mp4_tile_cache.acquire_from_frames); FFmpeg never
       demuxes a GIF here. FFmpeg's own GIF compositing disagrees with PIL
       on disposal and partial-extent frames -- 7 of 15 frames on a stock
       test file, ~48% of pixels off -- so letting it build the cache
       silently changed what opaque keys looked like.
    2. THE ROUTE IS DECIDED BY RENDERED ALPHA, not by the header's
       transparency declaration: 75% of real GIFs declare it, 11% ever
       render it (see frame_has_alpha), so declaring was the wrong question
       and left ~64% of GIFs on the expensive path.

    Which means the classification costs one PIL walk the FIRST time a GIF
    is seen at a given size -- the same walk main paid unconditionally, plus
    an alpha extrema pass and (for an opaque GIF) one encode. Afterwards the
    artifact on disk IS the classification: a warm construction walks the
    delays only and attaches a reader, decoding no pixels at all.

    With `performance.cache-videos` off there is no disk cache to route to,
    so every GIF stays on the frame list and no GIF ever reaches the
    registry.

    Either way the TIMELINE is PIL's: wall-clock picking over the cumulative
    per-frame delays, so an irregularly-timed GIF plays at its own rhythm
    rather than at a video's constant fps. On the video route the picked
    index is handed to the reader instead of subscripting a list; the
    arithmetic is identical (pinned by scenario_gif_timeline /
    scenario_gif_delays)."""

    # Class-level default: get_next_frame's picking arithmetic is exercised
    # against synthetic timelines on instances built attribute-by-attribute
    # via __new__ (scenario_gif_timeline), which never touch the video route.
    video_cache = None

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
        self._play_start: float | None = None
        self._last_frame_tick: float | None = None

        # Serializes close() against an in-flight frame fetch on the video
        # route -- InputVideo._close_lock's contract: a
        # get_frame() that starts after release() can resurrect a capture
        # through _maybe_adopt_shared_cache and leak it.
        self._close_lock = threading.Lock()

        self.frames: "list[Image.Image]" = []
        self._frames_bytes = 0

        # mem-plan P2.3: cap frame size at 2x the key tile instead of source
        # resolution -- a 500px/200-frame GIF is ~200MB at source res vs
        # ~46MB fitted. Composited size is decided per tick by
        # add_image_to_background/get_composed_layout (UI max is 200%,
        # ImageEditor.py), so 2x tile is the largest a frame is ever
        # displayed at. Shrink-only and aspect-preserving on BOTH routes
        # (see tile_video_size).
        tile_w, tile_h = self.deck_controller.get_key_image_size()
        fit_size = (max(1, tile_w * 2), max(1, tile_h * 2))

        # Saturation is baked in once, at decode/build time -- the frames are
        # this asset's per-frame memo (get_next_frame only picks from them),
        # so enhancing per tick would re-pay ImageEnhance forever. A
        # saturation change reloads the page, which rebuilds this object
        # under the new factor (see set_display_saturation) -- the same
        # contract as InputImage/BackgroundImage, and the registry keys its
        # cache files on the factor for the video route. Skipped entirely at
        # the default factor.
        saturation = self.deck_controller.get_display_saturation()

        self.frame_delays: "list[int]" = []
        self._cum_delays: "list[float]" = []
        self._total_delay: float = 0.0

        # No disk cache configured -> nothing to route to. Every GIF keeps
        # its frame list, and the registry
        # never sees a GIF at all: a reader with no artifact to read falls
        # back to decoding the SOURCE, which for a GIF means both FFmpeg's
        # divergent compositing and (with no builder to promote anything) a
        # capture that is released at end-of-source and then repeats its
        # last frame forever. Read once, here, so one object cannot change
        # routes mid-life.
        if not mp4_tile_cache.cache_videos_enabled():
            frames, _ = self._decode_all(fit_size, saturation)
            self._hold_frame_list(frames)
            budget = gif_key_budget_bytes()
            if budget and self._frames_bytes > budget:
                log.warning(
                    f"{self.gif_path}: {self._frames_bytes / (1024 * 1024):.1f}MB of "
                    f"GIF frames retained, over the "
                    f"{budget / (1024 * 1024):.1f}MB per-GIF budget -- the bounded "
                    f"route needs performance.cache-videos, which is disabled"
                )
            return

        # Raises on a corrupt/truncated file (the header parse), exactly
        # like the decode it precedes -- the construct site fails soft to
        # InputVideo.
        n_frames, source_size = gif_header_geometry(self.gif_path)
        out_size = tile_video_size(source_size, fit_size)

        # Which of the two artifacts this GIF is allowed to use, decided
        # before any pixel is touched: the lossless one an opaque GIF builds
        # for itself, or the alpha-dropping one the over-budget ladder
        # streams. They are separate cache variants on purpose -- sharing
        # one name would let a GIF that once streamed under a tiny budget be
        # read back forever as "proven opaque", so raising the budget could
        # never give it its transparency back. The estimate is the RETAINED
        # footprint, measured against the frame list's own geometry rather
        # than the mp4's even-rounded one.
        retained_size = contained_size(source_size, fit_size)
        estimate = n_frames * retained_size[0] * retained_size[1] * 4
        budget = gif_key_budget_bytes()
        over_budget = estimate > budget
        variant = BOUNDED_TILE_VARIANT if over_budget else ""

        # WARM: that artifact already exists, so this GIF has been walked
        # before at this size -- and, on the lossless variant, found opaque.
        # Walk the delays and attach: no frame is decoded, converted, fitted
        # or retained. This is the steady state after first sight, and the
        # page-load win.
        reader = mp4_tile_cache.attach_promoted(self.gif_path, out_size, saturation,
                                                variant=variant)
        if reader is not None:
            try:
                self._adopt_timeline(probe_gif_timeline(self.gif_path).frame_delays)
            except Exception:
                # Never leave the registry holding a reference for an object
                # whose constructor is about to raise.
                mp4_tile_cache.release(reader)
                raise
            self.video_cache = reader
            return

        # COLD: one PIL walk decides everything (see the class docstring).
        if over_budget:
            self._cold_streaming_walk(fit_size, saturation, out_size,
                                      estimate, budget, n_frames, retained_size)
        else:
            self._cold_retained_walk(fit_size, saturation, out_size)

    def _adopt_timeline(self, delays_ms: "list[int]") -> None:
        """Per-frame delays -> this object's playback timeline. One place,
        so a route can never install a timeline the others could not."""
        self.frame_delays = list(delays_ms)
        self._cum_delays = cumulative_gif_delays(self.frame_delays)
        self._total_delay = self._cum_delays[-1] if self._cum_delays else 0.0

    def _composited_walk(self, fit_size: "tuple[int, int]", saturation: float,
                         delays_out: "list[int]", alpha_out: "list[bool]"):
        """The single PIL pass v2 is built on: composite (PIL, never
        FFmpeg), fit shrink-only to 2x tile, bake saturation -- recording
        each frame's delay and the exact rendered-alpha verdict as it goes.

        Yields the frames; the caller decides whether to KEEP them (the RAM
        route, and the source for an opaque GIF's mp4) or hand them straight
        to the writer and stay O(1). The alpha check stops asking once the
        answer is yes -- a truly-alpha GIF usually settles it on frame 0."""
        with contextlib.closing(gif_frame_walk(
                self.gif_path, max_size=fit_size, saturation=saturation)) as walk:
            for frame, delay in walk:
                delays_out.append(delay)
                if not alpha_out[0] and frame_has_alpha(frame):
                    alpha_out[0] = True
                yield frame

    def _decode_all(self, fit_size: "tuple[int, int]",
                    saturation: float) -> "tuple[list[Image.Image], bool]":
        """The whole GIF, decoded and retained, timeline installed.
        Returns (frames, has_rendered_alpha)."""
        delays: "list[int]" = []
        alpha = [False]
        frames = list(self._composited_walk(fit_size, saturation, delays, alpha))
        self._adopt_timeline(delays)
        return frames, alpha[0]

    def _hold_frame_list(self, frames: "list[Image.Image]") -> None:
        """Keep the decoded frames as this key's per-frame memo: the only
        representation that carries alpha.

        Registers with the image-cache census, accounting-only and only for
        this route -- the video route's RAM is the reader's, already counted
        under video_readers. Never evictable: these frames ARE the memo, so
        evicting them would re-decode the GIF every media tick."""
        self.frames = frames
        self._frames_bytes = sum(
            frame.width * frame.height * len(frame.getbands()) for frame in frames
        )
        cache_budget.register(
            self, label=f"gif_frames:{os.path.basename(self.gif_path)}", evictable=False)

    def _cold_retained_walk(self, fit_size: "tuple[int, int]", saturation: float,
                            out_size: "tuple[int, int]") -> None:
        """Under budget: walk once holding the frames, then let what they
        turned out to BE decide where they live.

        Alpha -> they stay (the RAM route, main's exact cost and pixels).
        Opaque -> they are written into the shared tile cache and dropped;
        the key ends up at O(1) RAM with pixels PIL itself composited. The
        peak here is one fitted frame list, which is main's *steady* state,
        held for the length of one encode (~40ms per 200 frames at 2x tile
        -- measured, so this stays on the constructing thread rather than
        buying a thread and a serve-from-list-until-promoted handover)."""
        frames, has_alpha = self._decode_all(fit_size, saturation)
        if has_alpha:
            self._hold_frame_list(frames)
            return

        reader = mp4_tile_cache.acquire_from_frames(
            self.gif_path, out_size, saturation, frames)
        if reader is None:
            # Cache unwritable (no codec, full/read-only disk): keeping the
            # frames is the honest degrade -- the key plays, correctly, at
            # main's footprint. The warning names the cost.
            log.warning(
                f"Could not build the tile cache for {self.gif_path}; keeping "
                f"{len(frames)} frames in RAM for this key instead"
            )
            self._hold_frame_list(frames)
            return
        self.video_cache = reader

    def _cold_streaming_walk(self, fit_size: "tuple[int, int]", saturation: float,
                             out_size: "tuple[int, int]", estimate: int, budget: int,
                             n_frames: int, retained_size: "tuple[int, int]") -> None:
        """Over budget: walk once WITHOUT holding anything, straight into
        the tile-cache writer.

        This is the one path in the design that can trade visuals for
        bounds: a GIF whose retained frames would exceed
        DECKARD_GIF_KEY_BUDGET_MB (default 32MB) plays without alpha rather
        than being allowed to blow a page's memory up on its own. Bounded
        beats pretty -- the same ladder GifBackground walks down to the cv2
        path -- and the alpha loss is announced once, at construction, never
        per tick."""
        log.warning(
            f"{self.gif_path}: ~{estimate / (1024 * 1024):.1f}MB of frames "
            f"({n_frames} at {retained_size[0]}x{retained_size[1]} RGBA) exceeds the "
            f"{budget / (1024 * 1024):.1f}MB per-GIF budget -- streaming it into the "
            f"mp4 tile cache instead, at one frame of RAM"
        )
        delays: "list[int]" = []
        alpha = [False]
        reader = mp4_tile_cache.acquire_from_frames(
            self.gif_path, out_size, saturation,
            self._composited_walk(fit_size, saturation, delays, alpha),
            variant=BOUNDED_TILE_VARIANT)
        if reader is None:
            raise RuntimeError(
                f"GIF is over the per-GIF frame budget and its tile cache could "
                f"not be built: {self.gif_path}"
            )
        if alpha[0]:
            log.warning(
                f"{self.gif_path} carries transparency, which the bounded mp4 route "
                f"cannot keep -- this key plays opaque. Raise "
                f"DECKARD_GIF_KEY_BUDGET_MB above "
                f"{estimate / (1024 * 1024):.1f} to keep it in RAM instead"
            )
        self.video_cache = reader
        if not delays:
            # The artifact already existed (another key built it while this
            # walk was being set up), so the generator was never consumed
            # and the delays never collected. Walk them, cheaply.
            delays = probe_gif_timeline(self.gif_path).frame_delays
        self._adopt_timeline(delays)

    def _source_index(self, cache, index: int) -> int:
        """Timeline frame index -> the reader's frame index.

        The cache is written frame-for-frame from the PIL walk, so this is
        normally the identity. It is not trusted, because the reader's count
        MOVES at runtime -- a promoted cache reports what the container
        actually holds, and a short read clamps it -- so a disagreement
        scales the index into the reader's range rather than reading past
        the end. Timing authority stays with the PIL delay timeline either
        way."""
        n_video = cache.n_frames
        n_timeline = len(self._cum_delays)
        if n_video <= 0 or n_timeline <= 0 or n_video == n_timeline:
            return index
        return min(n_video - 1, index * n_video // n_timeline)

    def _video_frame(self, index: int) -> Image.Image | None:
        """One frame off the shared tile cache. Check-then-hold, exactly
        InputVideo.get_next_frame's pattern: the unlocked peek keeps the
        post-close hot path free, the lock makes close() wait for an
        in-flight decode instead of releasing the reader underneath it."""
        if self.video_cache is None:
            return None
        with self._close_lock:
            cache = self.video_cache
            if cache is None:
                return None
            return cache.get_frame(self._source_index(cache, index))

    def _frame_at(self, index: int) -> Image.Image | None:
        """The payload for a picked timeline index, whichever route holds
        it. None once the route it came from has been released."""
        frames = self.frames
        if frames:
            return frames[index]
        return self._video_frame(index)

    def budget_bytes(self) -> int:
        """Pixel bytes of the retained frame list (image-cache census). Computed
        once at decode time: the list is immutable for this object's life.
        Zero on the video route, which registers nothing here -- its RAM is
        the reader's, counted under video_readers."""
        return self._frames_bytes

    def _frame_count(self) -> int:
        """Frames this object can serve. The retained list on the alpha
        route; the timeline's length on the video route, where the frames
        live in the cache file rather than in this object."""
        if self.frames:
            return len(self.frames)
        return len(self._cum_delays) if self.video_cache is not None else 0

    def get_next_frame(self, now: float | None = None) -> Image.Image | None:
        # ONE snapshot of the timeline for the whole tick. close() rebinds
        # these to fresh empty containers from the teardown thread, so
        # re-reading them mid-arithmetic could divide by a _total_delay that
        # was non-zero at the guard and zero at the modulo (reproduced:
        # ~2e-5 per close at the default switch interval -- bounded by the
        # media tick's own guard, but it costs a dropped tick and a 250ms
        # backoff exactly at page teardown), or index a timeline emptied
        # after its length was read.
        cum_delays = self._cum_delays
        total_delay = self._total_delay
        n = self._frame_count()
        if n == 0:
            return None
        if n == 1 or total_delay <= 0:
            # Single-frame GIF, or no usable timing info: nothing to pick.
            self.active_frame = 0
            return self._frame_at(0)

        if now is None:
            now = time.time()

        if self._play_start is None:
            self._play_start = now
        elif self._last_frame_tick is not None and now - self._last_frame_tick > 1.0:
            # Ticks stopped while the page/key was away (screensaver, page
            # switch, suspend): shift the timebase across the gap so playback
            # resumes near where it left off instead of fast-forwarding
            # through the whole gap (mirrors BackgroundVideo's gap clamp).
            frame_period = cum_delays[0] if cum_delays else total_delay / n
            self._play_start += (now - self._last_frame_tick) - frame_period
        self._last_frame_tick = now

        elapsed = now - self._play_start
        t = elapsed % total_delay if self.loop else min(elapsed, total_delay)

        frame = bisect.bisect_right(cum_delays, t)
        if frame >= n:
            frame = n - 1  # guard the end: float-edge / non-loop clamp landing on t == total
        self.active_frame = frame

        return self._frame_at(self.active_frame)

    def get_frame_delay(self) -> float:
        """Get delay for current frame in seconds"""
        if self.active_frame < 0 or self.active_frame >= len(self.frame_delays):
            return 1.0 / self.fps  # Fallback to fps-based timing
        return self.frame_delays[self.active_frame] / 1000.0  # Convert ms to seconds
    
    def get_raw_image(self) -> Image.Image:
        # get_next_frame() is None once close() has released the frames;
        # widening only this override would be an incompatible return type.
        return self.get_next_frame()  # type: ignore[return-value]  # root cause: SingleKeyAsset.get_raw_image -> Image.Image is too narrow (Subclasses/SingleKeyAsset.py)
    
    def close(self) -> None:
        """Drops the retained frame list (the whole footprint) and leaves the
        object safely tickable.

        Swaps in fresh EMPTY containers instead of None/del: a
        media tick can land after close() -- teardown races the media loop --
        and get_next_frame()/get_frame_delay() read len(self.frames) /
        len(self.frame_delays) on every tick, where `None` raised TypeError
        and the deleted attribute raised AttributeError. With empty lists the
        n == 0 arm already returns None, so a late tick and a double close are
        both no-ops by construction (GifBackground.close()'s pattern -- the
        two providers stay in lockstep).

        Also leaves the image-cache census immediately: the frames are gone, so the
        registered byte count must stop being reported rather than linger
        until GC drops the weak registration.

        Video route: detaches this reader from the shared tile-cache registry
        (InputVideo.close()'s contract -- the shared cache file and its
        builder outlive us if another key still wants them). The timeline is
        emptied FIRST, so a tick racing this call sees zero frames and
        returns before it can ask a released reader for pixels; the lock then
        waits out any fetch already in flight. Idempotent."""
        self.frames = []
        self.frame_delays = []
        self._cum_delays = []
        self._total_delay = 0.0
        self._frames_bytes = 0
        cache_budget.unregister(self)
        with self._close_lock:
            if self.video_cache is not None:
                mp4_tile_cache.release(self.video_cache)
                self.video_cache = None

# Shared, context-independent text measurement for label layout / scroll
# detection: textbbox only computes layout (it never touches the pixels), and
# it matches what the per-key render's own draw context would report --
# unlike font.getbbox, which is single-line and counts '\n' toward the width
# (the phantom-scroll trigger).
_label_measure_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


class _RecordingTooLarge(Exception):
    """A label's glyph masks blew the retention budget mid-recording.

    Not an error condition: the caller drops the partial recording and pins
    the label to the direct per-frame draw. Raised from inside draw_bitmap so
    the abort also stops the RASTERIZATION -- the expensive half -- rather
    than letting text() finish and discarding the result afterwards."""


class _BitmapRecorder:
    """Captures the bitmap blits ImageDraw.text() would issue, instead of
    running them.

    ImageDraw.text() is two steps: rasterize the (stroked) glyph run into a
    coverage mask via FreeType -- expensive -- and blit that mask onto the
    target with a solid ink -- cheap. A static label re-runs both on every
    media tick even though only the pixels UNDER it changed. Standing in for
    the draw core while text() runs records step 2's arguments, so later
    frames can replay the blit with the mask already rasterized.

    Everything else is delegated to the real core object -- notably
    draw_ink(), which ImageDraw._getink() calls to resolve the fill colors,
    so the recorded ink is exactly the one a direct draw would have used.

    max_ops/max_bytes are the hard retention bound (see
    LabelManager._MAX_LABEL_OPS / _MAX_LABEL_MASK_BYTES): text() emits one
    blit per line per pass, so both the retained bytes and the FreeType work
    scale with the line count, and this recording runs on the sole device
    writer. Exceeding either raises _RecordingTooLarge on the spot."""
    __slots__ = ("_core", "ops", "_max_ops", "_max_bytes", "_bytes")

    def __init__(self, core, max_ops: int, max_bytes: int):
        self._core = core
        self.ops: list[tuple] = []
        self._max_ops = max_ops
        self._max_bytes = max_bytes
        self._bytes = 0

    def __getattr__(self, name):
        return getattr(self._core, name)

    def draw_bitmap(self, coord, mask, ink) -> int:
        # The mask is an 8-bit coverage ImagingCore, so 1 byte per pixel.
        self._bytes += mask.size[0] * mask.size[1]
        if len(self.ops) >= self._max_ops or self._bytes > self._max_bytes:
            raise _RecordingTooLarge(
                f"{len(self.ops) + 1} blits / {self._bytes} mask bytes past the "
                f"{self._max_ops}-op / {self._max_bytes}-byte budget")
        self.ops.append((tuple(coord), mask, ink))
        return 0


class LabelManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input
        
        self.page_labels: dict[str, "KeyLabel"] = {}
        self.action_labels: dict[str, "KeyLabel"] = {}
        self.scroll_wait = 25
        # Monotonic version stamp for the three LATCH-style memos below --
        # the ones whose stored value carries no identity of its own, so a
        # reader cannot tell fresh data from resurrected data.
        #
        # Those memos are filled lazily on the RENDER path (media thread) and
        # dropped on the EDIT path (UI/plugin threads), unlocked. A plain
        # None-latch loses the race: reader sees None, composes, editor
        # invalidates, reader stores -- and the pre-edit value is now pinned
        # FOREVER. Reproduced in review round 2 for _composed_labels_cache
        # (the old label text stays on screen) and it pre-exists on main for
        # _has_visible_labels_cache (a stale False makes
        # ControllerKey._tile_passthrough_ok paint a labelled key as a bare
        # tile, permanently).
        #
        # The fix is to stamp every publication with the epoch it was
        # computed UNDER: builders capture the epoch BEFORE composing and
        # publish (epoch, value); readers accept the memo only while its
        # stamp still equals the current epoch. A late store therefore
        # publishes a stamp the readers already reject. Two concurrent
        # increments can collapse into one -- harmless, because the counter
        # never decreases, so any change still leaves the epoch past every
        # stamp captured before it. No lock, no ordering assumption.
        #
        # _bbox_cache / _scroll_strips / _static_ops deliberately do NOT need
        # this; see _bump_label_epoch().
        self._label_epoch: int = 0
        # (epoch, {position -> text width}), for composed labels that are
        # wider than the key AND rolling labels are enabled -- i.e. the
        # labels that actually scroll. None = needs recompute (invalidated
        # with the label setters; a rolling-labels toggle lands via
        # reload_page, which rebuilds these managers).
        # get_has_scroll_labels() derives from this.
        self._scroll_widths_cache: tuple[int, dict[str, int]] | None = None
        # (epoch, bool): whether any composed label has non-empty text.
        self._has_visible_labels_cache: tuple[int, bool] | None = None
        # position -> (cache key, strip image, ax, ay): the label's text +
        # outline rasterized ONCE onto a transparent strip; scroll frames
        # composite a window of it instead of re-running draw.text
        # (the per-tick raster was ~2.5ms per key).
        self._scroll_strips: dict[str, tuple] = {}
        # position -> (cache key, blit ops | None): the STATIC label's glyph
        # masks, rasterized once and replayed per frame (the
        # per-tick draw.text with stroke was ~820us per key, ~50% of the tick
        # on a populated animated page). None ops = this position is pinned
        # to the direct draw (see _draw_static_label).
        self._static_ops: dict[str, tuple] = {}
        # position -> (cache key, (w, h)): textbbox measurement of the
        # composed label; the freetype layout pass is the second-biggest
        # per-frame cost after the raster itself.
        self._bbox_cache: dict[str, tuple] = {}
        # (epoch, {position -> KeyLabel}): the merged page+action+defaults
        # labels. See get_composed_labels() for the invalidation contract.
        self._composed_labels_cache: tuple[int, dict[str, "ComposedKeyLabel"]] | None = None

        self.init_labels()
        # Rolling-label animation state per position: the current scroll
        # offset in whole pixels, and the wall-clock deadline of the next
        # advance (None = fresh, starts with the leading hold). Wall-clock
        # (not tick-count) so the scroll speed doesn't change with the media
        # loop's actual iteration rate, which event wakes can push past FPS.
        self.frames: dict[str, dict] = {
            "top": {"position": 0, "next_step_at": None},
            "center": {"position": 0, "next_step_at": None},
            "bottom": {"position": 0, "next_step_at": None},
        }

    def init_labels(self):
        for position in ["top", "center", "bottom"]:
            self.page_labels[position] = KeyLabel(self.controller_input)
            self.action_labels[position] = KeyLabel(self.controller_input)
 
    def _bump_label_epoch(self) -> None:
        """Retire the latch-style label memos: move the epoch, then drop
        them.

        Every site that changes what a composed label looks like must go
        through here -- dropping a memo without moving the epoch reopens the
        store-after-clear window (see _label_epoch), and moving the epoch
        without dropping the memo would leave the pre-edit value reachable
        until the next successful publish. One method so the two can never
        drift apart.

        _bbox_cache, _scroll_strips and _static_ops are NOT reset here and do
        NOT need the epoch: each entry stores its own CONTENT KEY alongside
        the value and every reader re-checks that key on the hit path. Their
        keys cover every input to what they hold -- text, resolved font file
        (which is what encodes family/weight/style) and size for the bbox,
        plus colors, outline, alignment, anchor, absolute draw coordinates
        and target geometry for the blits/strip -- so equal key implies equal
        pixels. A store that lands after a clear can therefore only
        resurrect an entry that is still CORRECT for the current label (the
        next reader hits it and skips a recompute) or one whose key no longer
        matches (the next reader misses and rebuilds). Neither is a
        correctness bug, and the retained bytes are bounded by the size caps
        below. The reset calls that do exist are eager memory release, not a
        correctness requirement."""
        self._label_epoch += 1
        self._scroll_widths_cache = None
        self._has_visible_labels_cache = None
        self._composed_labels_cache = None

    def invalidate_scroll_caches(self) -> None:
        """Drop the derived label caches so the next tick/render recomputes
        scroll detection and geometry. Any code path that mutates a label's
        attributes IN PLACE (i.e. not through set_page_label/set_action_label
        -- notably Page.set_label_* poking page_labels[pos].<attr> directly)
        must call this, or get_scroll_label_widths() keeps returning the old
        overflow set and the render composites a stale strip: a shortened
        label keeps scrolling forever and a lengthened one never starts until
        a page reload (when the label is edited through the editor). Cheap: the
        widths/visible flags are recomputed lazily and the strip/bbox/static
        dicts are re-keyed on demand."""
        self._bump_label_epoch()
        self._bbox_cache.clear()
        self._scroll_strips.clear()
        self._static_ops.clear()

    def clear_labels(self):
        self.init_labels()
        self._bump_label_epoch()
        self._scroll_strips.clear()
        self._static_ops.clear()
        self._bbox_cache.clear()

    def set_page_label(self, position: str, label: "KeyLabel", update: bool = True):
        if label is None:
            label = self.page_labels[position]
            label.clear_values()
        else:
            self.page_labels[position] = label

        self._bump_label_epoch()
        self._static_ops.clear()
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

        self._bump_label_epoch()
        self._static_ops.clear()
        self.update_label_editor()
        if update:
            self.update_label(position)

    def update_label_editor(self):
        """Kept as the caller-facing name; the widget work is the adapter's.

        Page.set_label_* calls this on every label styling change (8 sites in
        PageManagement/Page.py) and the trailing update_input repaint runs
        after it -- so this must stay a plain forwarder that never raises.
        """
        ui_port.get().on_input_visuals_changed(
            self.controller_input.deck_controller, self.controller_input.identifier,
            self.controller_input.state, "labels")

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

    def get_composed_label(self, position: str) -> "ComposedKeyLabel":
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
    
    def get_composed_labels(self) -> dict[str, "ComposedKeyLabel"]:
        """The merged page+action+defaults labels for all three positions,
        memoized.

        The merge itself is not free: three KeyLabel copies plus
        inject_defaults' nine settings reads measured ~60us per key per media
        tick, paid on every frame of an animated background even though the
        labels only change when something sets one.

        Invalidation: every label mutation goes through set_page_label /
        set_action_label / clear_labels, or -- for the in-place editor path --
        Page.set_label_*, which calls invalidate_scroll_caches(); all of them
        land on _bump_label_epoch(), which retires this memo even against a
        concurrent render already halfway through composing one.

        A change to the app-wide FONT DEFAULTS is not a label mutation and
        needs no separate channel: all four Settings writers that touch
        gl.settings_manager.font_defaults (the font row, the font-color row,
        the outline-color row and the outline-width row -- Settings.py:448,
        478, 502, 527) call page_manager.reload_all_pages(), and reloading a
        page runs create_n_states(), which REPLACES every input state object
        and with it every LabelManager. So a font-defaults change does not
        invalidate this memo, it destroys the object holding it -- a stronger
        guarantee than any clear_labels()-style reasoning, and one that
        covers dials and touchscreens too (verified on main, review round 2).
        get_scroll_label_widths() documents the weaker sibling assumption for
        the rolling-labels toggle.

        The returned KeyLabels are SHARED, so treat them as read-only.
        get_composed_label() still returns a fresh object per call for
        callers that want to mutate one (e.g. the label editor)."""
        memo = self._composed_labels_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        # Stamp with the epoch read BEFORE composing: an invalidation that
        # lands during the compose moves the epoch past this stamp, so the
        # store below publishes something every reader rejects instead of
        # silently reinstating pre-edit labels.
        epoch = self._label_epoch
        labels = {
            position: self.get_composed_label(position)
            for position in ("top", "center", "bottom")
        }
        self._composed_labels_cache = (epoch, labels)
        # Return the dict we just built rather than re-reading the attribute,
        # so a concurrent publish cannot swap the result mid-call.
        return labels

    
    def inject_defaults(self, label: "KeyLabel") -> "ComposedKeyLabel":
        """Fills every unset field from the app-wide font defaults, in place.

        Returns the SAME object, retyped: after this runs there is no field
        left for a reader to None-check (see ComposedKeyLabel)."""
        if label.text is None:
            label.text = ""
        if label.color is None:
            # List, not tuple: the field is declared list[int] and the
            # settings path yields a JSON list, so a tuple fallback made the
            # attribute's runtime type depend on which branch filled it.
            label.color = gl.settings_manager.font_defaults.get("font-color") or [255, 255, 255, 255]
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
            label.outline_color = gl.settings_manager.font_defaults.get("outline-color") or [0, 0, 0, 255]
        if label.alignment is None:
            label.alignment = gl.settings_manager.font_defaults.get("alignment") or "center"

        return cast("ComposedKeyLabel", label)
    
    def fix_invalid(self, label: "ComposedKeyLabel") -> "ComposedKeyLabel":
        if not isinstance(label.text, str):
            label.text = str(label.text)

        return label

    def update_label(self, position: str):
        self.controller_input.update()

    def get_available_width(self) -> int:
        return self.controller_input.get_image_size()[0]

    def get_has_visible_labels(self) -> bool:
        # A label is drawn iff its text is non-empty (see add_labels_to_image).
        # Epoch-stamped: a stale False here is not just a missed repaint, it
        # sends ControllerKey._tile_passthrough_ok down the bare-tile fast
        # path, so the labelled key renders as an empty tile until something
        # else invalidates (see _label_epoch).
        memo = self._has_visible_labels_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        epoch = self._label_epoch
        labels = self.get_composed_labels()
        visible = any(label.text not in (None, "") for label in labels.values())
        self._has_visible_labels_cache = (epoch, visible)
        return visible

    def _measure_text(self, position: str, label: "ComposedKeyLabel") -> tuple[int, int]:
        """(w, h) of the composed label's rendered text block, cached per
        position. Both scroll detection and the render path measure through
        here, so they can never disagree about whether a label overflows."""
        font = label.get_font()
        key = (label.text, getattr(font, "path", None), getattr(font, "size", None))
        cached = self._bbox_cache.get(position)
        if cached is not None and cached[0] == key:
            return cached[1]
        _, _, w, h = _label_measure_draw.textbbox((0, 0), label.text, font=font)
        # textbbox is typed to return floats (it adds the origin, which may be
        # fractional); this call anchors at integer (0, 0) with an integer
        # glyph bbox, so the values are already ints and int() is identity.
        measured = (int(w), int(h))
        self._bbox_cache[position] = (key, measured)
        return measured

    def get_scroll_label_widths(self) -> dict[str, int]:
        """Text widths of the composed labels that actually scroll: rolling
        labels enabled AND rendered text wider than the input. Measured with
        the same multiline-aware textbbox the render path uses, so detection
        can never flag a label the render would draw statically (that
        mismatch kept the media loop at full FPS re-rendering identical
        frames)."""
        # Cache invalidation: label edits go through invalidate_scroll_caches()
        # (set_page_label/set_action_label and the Page.set_label_* setters); a
        # rolling-labels TOGGLE lands via reload_page(), which rebuilds these
        # managers. A rolling-labels change made OUTSIDE the Settings dialog
        # (a direct settings.json edit, or a plugin writing app settings) does
        # NOT reload_page and so leaves this cache stale until the next label
        # edit or page load -- a pre-existing lifecycle assumption, acceptable
        # because that path isn't a supported runtime toggle.
        #
        # Epoch-stamped like the other latch memos: a store landing after a
        # concurrent edit would otherwise pin the pre-edit overflow set --
        # exactly the "a shortened label keeps scrolling forever" shape.
        memo = self._scroll_widths_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        epoch = self._label_epoch

        widths: dict[str, int] = {}
        rolling_labels_enabled = gl.settings_manager.app().rolling_labels
        if rolling_labels_enabled:
            available_width = self.get_available_width()
            labels = self.get_composed_labels()
            for position in labels:
                text = labels[position].text
                if text in (None, ""):
                    continue
                w, _ = self._measure_text(position, labels[position])
                if w > available_width:
                    widths[position] = w
        self._scroll_widths_cache = (epoch, widths)
        return widths

    def get_has_scroll_labels(self) -> bool:
        return len(self.get_scroll_label_widths()) > 0

    # Original cadence, expressed in wall time instead of loop iterations
    # (at the nominal 30 FPS the old code advanced 1px per two ticks and
    # burned scroll_wait=25 ticks -- even ticks at the leading edge -- per
    # hold). Wall-clock keeps the speed stable when event wakes push the
    # loop past its nominal rate.
    _NOMINAL_TICK_RATE = 30.0
    SCROLL_STEP_SECONDS = 2.0 / _NOMINAL_TICK_RATE

    def _scroll_hold_start_seconds(self) -> float:
        return self.scroll_wait * 2.0 / self._NOMINAL_TICK_RATE

    def _scroll_hold_end_seconds(self) -> float:
        return self.scroll_wait / self._NOMINAL_TICK_RATE

    def tick_scroll_labels(self) -> bool:
        """Advances the rolling-label animation and reports whether any
        visible scroll offset changed (= a re-render is needed). This is the
        ONLY place scroll state moves -- rendering is pure -- so the hold
        plateaus and the between-step ticks cost integer/time math here
        instead of a full composite that the hash de-dup would throw away
        anyway."""
        changed = False
        now = time.monotonic()
        available_width = self.get_available_width()
        for position, w in self.get_scroll_label_widths().items():
            frame = self.frames[position]
            # The sweep runs from x=start (10px right of centered) down to
            # one pixel past x=stop (10px left of centered), like the
            # original: overshoot = start - stop.
            overshoot = w - available_width + 20
            next_at = frame.get("next_step_at")
            if next_at is None:
                # Fresh label: hold at the start position first.
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
                continue
            if now < next_at:
                continue
            if frame["position"] > overshoot:
                # Trailing hold elapsed: snap back to the start and hold.
                frame["position"] = 0
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
            else:
                frame["position"] += 1
                if frame["position"] > overshoot:
                    frame["next_step_at"] = now + self._scroll_hold_end_seconds()
                else:
                    frame["next_step_at"] += self.SCROLL_STEP_SECONDS
                    # Re-anchor instead of bursting to catch up if the loop
                    # stalled (page switch, suspend, ...).
                    if frame["next_step_at"] < now - 0.5:
                        frame["next_step_at"] = now
            changed = True
        return changed

    # A precomposed strip is width x keyheight x 4 bytes, retained per label
    # position per state for the whole sweep. Strip width scales with TEXT
    # length, so a pasted 50k-char label would retain ~95 MB and stall the
    # sole-writer media thread for seconds rasterizing it (review round 1).
    # Past this width we fall back to the pre-MR direct per-frame draw: only
    # pathological labels pay the per-frame raster CPU, and nothing is
    # retained. 4096 px is ~290 'm' glyphs at font 15 -- far past any legible
    # key label -- and caps the retained strip near ~1.6 MB even on a
    # 100px-tall SD+ dial image.
    _MAX_STRIP_WIDTH = 4096
    # ...but only on ONE axis. Strip HEIGHT tracks the composed text block,
    # so a multiline label re-opens the same hole vertically: a 2000-line
    # label measures ~42000 px tall, i.e. a ~700 MB strip (review round 2).
    # 4 MiB leaves ~2.5x headroom over the widest legitimate strip the width
    # cap already permits (4096 x 100 x 4 = 1.6 MB on an SD+ dial image) and
    # rejects the pathological-height case outright.
    _MAX_STRIP_BYTES = 4 * 1024 * 1024

    # The same bound for the recorded static-label blits. Retention there is
    # 1 byte/px of glyph mask rather than 4 byte/px of RGBA canvas, but it
    # scales with LINE COUNT the same way -- and unlike the strip, the
    # recording also costs FreeType time on the sole device writer: measured
    # headless, 500 lines = 1000 blits / 430 KB / 0.22 s, 2000 lines = 4000
    # blits / 2.0 MB / 1.06 s, and the reviewer's 20k-line probe = 32 MB and
    # a 5 s media-thread stall.
    #
    # 512 KiB per position is ~6x the largest label that can actually be READ
    # on an input: a 200x200 dial image covered edge to edge in glyphs is
    # ~40k px per pass, ~80 KB for the stroke+fill pair. It also covers a
    # single-line label stretched to the full 4096 px width cap (~240 KB).
    # Three positions -> a 1.5 MiB ceiling per input state, bounded on BOTH
    # axes. 512 ops is 256 lines x (stroke pass + fill pass); a label that
    # tall is already thousands of pixels past any key.
    #
    # Enforced twice: cheaply BEFORE recording from the measurements the
    # render already has (_label_ops_budget_ok), and as a hard abort inside
    # the recorder, which is what actually bounds the wall time.
    _MAX_LABEL_MASK_BYTES = 512 * 1024
    _MAX_LABEL_OPS = 512

    def _label_ops_budget_ok(self, label: "ComposedKeyLabel", w: int, h: int) -> bool:
        """Whether recording this label's blits is worth the retention and
        the media-thread stall, decided from measurements the caller already
        has (no rasterization).

        text() masks each LINE separately and runs the whole thing twice when
        there is an outline, so the op count is 2 per line and the stroke
        padding is paid per line rather than once for the block. That makes
        this an over-estimate of the real mask total (~2x on measured cases,
        because a mask is the glyph run's tight bbox, not the block
        rectangle) -- the safe direction for a pre-check, since the exact
        bound is enforced inside _BitmapRecorder."""
        lines = label.text.count("\n") + 1
        if lines * 2 > self._MAX_LABEL_OPS:
            return False
        stroke = label.outline_width or 0
        estimated_bytes = (2 * (int(w) + 2 * stroke)
                           * (int(h) + lines * 2 * stroke))
        return estimated_bytes <= self._MAX_LABEL_MASK_BYTES

    def _composite_scroll_strip(self, image: Image.Image, position: str, label: "ComposedKeyLabel",
                                w: int, h: int, x_position: float, y_position: float) -> None:
        """Draws a scrolling label by compositing a window of its precomposed
        text strip at this tick's offset. The strip is rasterized once per
        (text, font, colors) and reused for every frame of the sweep; a
        direct draw.text with stroke costs ~2.5ms per frame, the composite
        ~0.014ms, pixel-identical (the target coords' fractional parts are
        constant across the sweep and get baked into the strip, so the paste
        offset is always a whole pixel).

        NOTE: the composite matches a direct draw for opaque ink. Semi-
        transparent fill/outline (alpha < 255, reachable only via the plugin
        set_label API / hand-edited page JSON, not the color picker) blends
        with straight-alpha OVER here vs PIL's coverage blend in draw.text, so
        the scrolling frame differs slightly from the static draw for those.
        The static twin (_draw_static_label) caches one layer lower --
        the glyph masks rather than a composited strip -- which is exact for
        any ink, but needs a fixed paste position, so it does not generalize
        back to the sweep."""
        font = label.get_font()
        outline_width = label.outline_width
        pad = outline_width + 6

        strip_width = int(w) + 2 * pad + 1
        strip_height = int(h) + 2 * pad + 1
        if strip_width > self._MAX_STRIP_WIDTH or \
                strip_width * strip_height * 4 > self._MAX_STRIP_BYTES:
            # Pathological label: skip the strip cache entirely and draw the
            # text directly at the scroll offset (the pre-MR path). Bounded
            # memory (nothing retained), correct pixels; per-frame raster CPU
            # is the trade, acceptable for a label this long. The byte test
            # is what covers a many-LINE label, whose block grows vertically
            # and slips straight past the width cap.
            self._scroll_strips.pop(position, None)
            ImageDraw.Draw(image).text((x_position, y_position), text=label.text,
                                       font=font, anchor="mm", align=label.alignment,
                                       fill=tuple(label.color),
                                       stroke_width=outline_width,
                                       stroke_fill=tuple(label.outline_color))
            return

        ay_base = pad + h / 2
        dy = (y_position - ay_base) % 1.0
        key = (label.text, getattr(font, "path", None), label.font_size,
               tuple(label.color), outline_width, tuple(label.outline_color),
               label.alignment, w, h, dy)
        cached = self._scroll_strips.get(position)
        if cached is None or cached[0] != key:
            ax = pad + w / 2
            ay = ay_base + dy
            # Antialiased edge pixels blend toward the canvas color; pre-fill
            # with the outermost ink color (at alpha 0) so the strip's edges
            # match a direct draw onto the key image.
            edge = tuple(label.outline_color[:3]) if outline_width > 0 else tuple(label.color[:3])
            strip = Image.new("RGBA", (int(w) + 2 * pad + 1, int(h) + 2 * pad + 1), edge + (0,))
            ImageDraw.Draw(strip).text((ax, ay), text=label.text, font=font,
                                       anchor="mm", align=label.alignment,
                                       fill=tuple(label.color),
                                       stroke_width=outline_width,
                                       stroke_fill=tuple(label.outline_color))
            cached = (key, strip, ax, ay)
            self._scroll_strips[position] = cached
        _, strip, ax, ay = cached

        px = round(x_position - ax)
        py = round(y_position - ay)
        # Crop to the visible window: in-place alpha_composite requires a
        # non-negative dest, and it does the correct straight-alpha OVER
        # (paste-with-mask would under-write the alpha channel on the
        # antialiased edges).
        crop_left, crop_top = max(0, -px), max(0, -py)
        crop_right = min(strip.width, image.width - px)
        crop_bottom = min(strip.height, image.height - py)
        if crop_right <= crop_left or crop_bottom <= crop_top:
            return
        if (crop_left, crop_top, crop_right, crop_bottom) != (0, 0, strip.width, strip.height):
            window = strip.crop((crop_left, crop_top, crop_right, crop_bottom))
        else:
            window = strip
        if image.mode == "RGBA":
            image.alpha_composite(window, (px + crop_left, py + crop_top))
        else:
            image.paste(window, (px + crop_left, py + crop_top), window)

    def _draw_static_label(self, image: Image.Image, draw: ImageDraw.ImageDraw,
                           position: str, label: "ComposedKeyLabel", w: int, h: int,
                           x_position: float, y_position: float, anchor: str) -> None:
        """Draws a non-scrolling label by replaying its cached glyph blits.

        A static label's pixels are a pure function of (text, font, colors,
        outline, alignment, image geometry) -- none of which change between
        media ticks -- yet draw.text() re-rasterized the stroked glyph run
        every frame: ~820us per key, ~50% of the whole tick on a populated
        page over an animated background. The rasterization is
        recorded ONCE here (via _BitmapRecorder standing in for the draw
        core) and later frames replay only the mask blits, ~11us.

        Pixel-exact by construction, not by approximation: the replay issues
        the identical C blits, with the identical masks, inks and absolute
        coordinates that draw.text() itself would have issued, in the same
        order. That is why this path -- unlike the scroll strip, whose
        straight-alpha OVER only matches for opaque ink -- needs no
        semi-transparent-ink carve-out. (A precomposed RGBA strip cannot be
        exact here: where a partially covered stroke pixel is overdrawn by a
        partially covered fill pixel, the strip has to collapse two coverage
        blends into one straight-alpha value, which measured up to 19/255 off
        a direct draw.)

        Falls back to the direct draw -- today's behavior, no cache retained
        -- for a label past the width cap or past the retention/op budget,
        and if PIL's text() internals ever stop routing through draw_bitmap.

        The non-RGB(A) guard is not a working fallback, it is behavior
        PARITY. The recorded ink is resolved for the target's mode (a
        palette index for "P", a packed int for "RGB"/"RGBA"), so a
        recording is only valid on the mode it was taken on, and this branch
        keeps such targets on whatever main already did with them -- which
        for "L" is to RAISE: a direct draw.text with an RGBA fill tuple onto
        an "L" image is a TypeError there too. In-app every label target is
        RGBA."""
        if image.mode not in ("RGB", "RGBA") or \
                int(w) + 2 * (label.outline_width + 6) + 1 > self._MAX_STRIP_WIDTH or \
                not self._label_ops_budget_ok(label, w, h):
            self._static_ops.pop(position, None)
            if media_prof:
                media_prof.count("label_ops_fallback")
            draw.text((x_position, y_position), text=label.text, font=label.get_font(),
                      anchor=anchor, align=label.alignment, fill=tuple(label.color),
                      stroke_width=label.outline_width,
                      stroke_fill=tuple(label.outline_color))
            return

        font = label.get_font()
        # The geometry (x/y/anchor) is derived from the image size and the
        # measured text, but keying on it directly means a resized deck image
        # or a re-measured label can never replay blits at stale coordinates.
        key = (label.text, getattr(font, "path", None), label.font_size,
               tuple(label.color), label.outline_width, tuple(label.outline_color),
               label.alignment, anchor, x_position, y_position,
               image.size, image.mode, w, h)
        cached = self._static_ops.get(position)
        if cached is None or cached[0] != key:
            ops = self._record_label_blits(
                image, label, font, (x_position, y_position), anchor)
            # A failed recording is memoized as None under the SAME key: the
            # attempt can cost hundreds of milliseconds, so it must not be
            # retried on every frame of an animated background.
            self._static_ops[position] = (key, ops)
            # Only a recording that actually produced replayable blits is a
            # cache MISS; a failed one is a fallback, counted below. Lumping
            # them together made a permanently-uncacheable label read as a
            # healthy warm cache in the profile.
            if media_prof and ops is not None:
                media_prof.count("label_ops_miss")
        else:
            ops = cached[1]
            if media_prof and ops is not None:
                media_prof.count("label_ops_hit")

        if ops is None:
            # Recording is not available for this label (see above): keep
            # drawing it the old way, without re-attempting the recording
            # every frame.
            if media_prof:
                media_prof.count("label_ops_fallback")
            draw.text((x_position, y_position), text=label.text, font=font,
                      anchor=anchor, align=label.alignment, fill=tuple(label.color),
                      stroke_width=label.outline_width,
                      stroke_fill=tuple(label.outline_color))
            return

        core = draw.draw
        for coord, mask, ink in ops:
            core.draw_bitmap(coord, mask, ink)

    def _record_label_blits(self, image: Image.Image, label: "ComposedKeyLabel", font,
                            xy: tuple, anchor: str) -> tuple | None:
        """Runs draw.text() against a throwaway target whose draw core only
        RECORDS the mask blits, and returns them (None = not recordable, draw
        directly instead).

        The probe target matches the real image's MODE because the ink is
        resolved for the mode. It matches the real SIZE because the probe
        doubles as the interception TRIPWIRE: a recording is only safe to
        replay if the recorder saw the WHOLE draw, and the proof of that is
        that the probe came out blank. Anything text() writes through a
        channel other than draw_bitmap -- PIL's embedded-color route pastes
        onto the target image directly, bypassing the draw core entirely --
        lands on this probe as residue, and a full-size probe is the only one
        that can still show it. (A 1x1 probe would record the identical ops,
        since text() derives the blit coordinates from xy/anchor/mask rather
        than from the canvas, but it would clip every escaped write away and
        blind this check.)

        So the bar is non-empty ops AND a blank probe. `not ops` alone only
        catches TOTAL loss; a stroke pass that records while the fill pass
        escapes would otherwise cache an outline-only label forever.

        Residue detection is one-sided -- black ink escaping onto an "RGB"
        probe is invisible to getbbox() -- so it can never reject a good
        recording, only miss a bad one. Every in-app label target is RGBA,
        where any escaped ink moves the alpha channel off zero."""
        try:
            probe_image = Image.new(image.mode, image.size)
            probe = ImageDraw.Draw(probe_image)
            recorder = _BitmapRecorder(probe.draw, self._MAX_LABEL_OPS,
                                       self._MAX_LABEL_MASK_BYTES)
            probe.draw = recorder
            probe.text(xy, text=label.text, font=font, anchor=anchor,
                       align=label.alignment, fill=tuple(label.color),
                       stroke_width=label.outline_width,
                       stroke_fill=tuple(label.outline_color))
            ops = tuple(recorder.ops)
            residue = probe_image.getbbox()
        except _RecordingTooLarge as too_large:
            # Expected for pathological labels that slipped past the cheap
            # pre-check; the partial recording is dropped here and the caller
            # pins the label to the direct draw.
            log.info(f"Label blit recording exceeded the retention budget "
                     f"({too_large}); falling back to the per-frame draw for "
                     f"this label")
            return None
        except Exception:
            # log.opt(exception=True), not exc_info=: loguru has no exc_info
            # kwarg and would treat it as a format argument, so the traceback
            # this fallback exists to surface was being dropped.
            log.opt(exception=True).warning(
                "Label blit recording failed; falling back to the per-frame "
                "draw for this label")
            return None
        if not ops or residue is not None:
            # Either no blit at all for non-empty text, or pixels on the probe
            # -- both mean PIL took a path this recorder does not model (e.g.
            # embedded-color glyphs). Replaying the recording would erase the
            # label, or keep only the half that was intercepted.
            log.warning(
                f"Label blit recording did not intercept the whole draw "
                f"({len(ops)} ops, probe residue {residue}); falling back to "
                f"the per-frame draw for this label")
            return None
        return ops

    def add_labels_to_image(self, image: Image.Image) -> Image.Image:
        # image = image.rotate(self.deck.get_rotation()*-1)
        if not self.get_has_visible_labels():
            # Nothing to draw: hand the caller back its own image rather than
            # a key-sized RGBA copy per frame. ControllerKey.get_current_image
            # knows the result can BE its input and skips the matching
            # close()es; every other caller returns it straight through.
            return image

        draw = ImageDraw.Draw(image)

        labels = self.get_composed_labels()
        scroll_widths = self.get_scroll_label_widths()
        for label in labels:
            text = labels[label].text
            if text in [None, ""]:
                continue

            alignment = labels[label].alignment

            w, h = self._measure_text(label, labels[label])

            # Vertical placement is shared by the static and scrolling paths.
            if label == "top":
                y_position = h/2 + 3
            elif label == "bottom":
                y_position = image.height - h/2 - 3
            else:
                y_position = (image.height - 0) / 2

            if label in scroll_widths:
                # Rolling label: composite the precomposed strip at this
                # tick's offset. Scroll state advances in
                # tick_scroll_labels() only -- rendering is pure, so paints
                # from key presses / page loads can't perturb the animation.
                start = image.width / 2 - (image.width - w) / 2 + 10
                x_position = start - self.frames[label]["position"]
                self._composite_scroll_strip(image, label, labels[label], w, h,
                                             x_position, y_position)
                continue

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

            # Use appropriate anchor based on alignment (x-anchor + "m" for vertical middle)
            anchor = anchor_x + "m"

            self._draw_static_label(image, draw, label, labels[label], w, h,
                                    x_position, y_position, anchor)

        del draw

        # The copy stays for ControllerKey.get_current_image: this method
        # draws IN PLACE, and that caller closes the buffer it passed in as
        # soon as the labelled result is a different object (its
        # `key_image is not labeled_image` guard). The dial/touchscreen
        # caller (ControllerTouchScreenState.get_rendered_touch_image) just
        # rebinds the name and never closes, so the copy is redundant on that
        # path -- droppable, but only by giving the two callers separate
        # contracts, and this only runs for inputs that carry a label at all.
        return image.copy()
        # return image.copy().rotate(self.deck.get_rotation())


class LayoutManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input

        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()

        # (token, layout key, resized image): the resized foreground for a
        # static asset, valid while the caller passes the same asset object,
        # the same backing source image (an in-place re-decode swap changes
        # it -- see add_image_to_background's fg_key), and unchanged
        # layout/geometry. Single tuple so concurrent updates swap it
        # atomically.
        self._fg_cache: tuple | None = None

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
    
    def get_composed_layout(self) -> "ComposedImageLayout":
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
    
    def inject_defaults(self, layout: ImageLayout) -> "ComposedImageLayout":
        """Fills every unset field, in place; returns the same object retyped
        (see ComposedImageLayout)."""
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

        return cast("ComposedImageLayout", layout)
    
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
        ui_port.get().on_input_visuals_changed(
            self.controller_input.deck_controller, self.controller_input.identifier,
            self.controller_input.state, "layout")

    def add_image_to_background(self, image: Image.Image | None, background: Image.Image, cache_token=None) -> Image.Image:
        if image is None:
            return background
        layout = self.get_composed_layout()

        width, height = background.size
        image_size = (int(width * layout.size), int(height * layout.size))

        if 0 in image_size:
            return background.copy()

        # The resized foreground depends only on the source asset and layout,
        # not on the (possibly animated) background. cache_token is the asset
        # object itself (the InputImage/InputVideo), pinned alive by the
        # `cached[0] is cache_token` identity check below -- a held reference
        # can't collide, unlike a freed id().
        #
        # cache_token alone is NOT enough to key the resized foreground:
        # InputImage._ensure_fits_composed() re-decodes and swaps its backing
        # `image` IN PLACE (B-03 -- the asset object stays identical while its
        # pixels change to a higher resolution). fg_key must therefore also
        # track WHICH source image was resized, or a post-swap composite would
        # be served the stale low-res entry cached from before the swap. Today
        # a swap only ever grows the image, so image_size (driven by
        # layout.size) already differs across a swap; but that coupling is
        # implicit -- a future same-size re-decode (e.g. a saturation change)
        # would not change image_size. id(image) closes that gap explicitly:
        # while cache_token is alive it holds a strong ref to `image`, so this
        # id cannot be reused by another object under us.
        fg_key = (layout.fill_mode, layout.halign, layout.valign, image_size,
                  id(image), image.size)
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
        
        self.action_color: list[int] | None = None
        self.page_color: list[int] | None = None

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
            ui_port.get().on_input_visuals_changed(
                self.controller_input.deck_controller, self.controller_input.identifier,
                self.controller_input.state, "background")

    def get_color_is_set(self, color: list[int] | None) -> bool:
        return color not in [None, [None]*3, [None]*4]

    def get_use_page_background(self) -> bool:
        return self.get_color_is_set(self.page_color)
    
    def get_composed_color(self) -> list[int]:
        # The `is not None` tests are implied by get_color_is_set (which reports
        # False for None) -- spelled out so the returns are provably non-None.
        page_color = self.page_color
        action_color = self.action_color
        if self.get_use_page_background() and page_color is not None and self.get_color_is_set(page_color):
            return page_color
        elif action_color is not None and self.get_color_is_set(action_color):
            return action_color
        else:
            return [0] * 4


class ControllerInputState:
    def __init__(self, controller_input: "ControllerInput", state: int):
        self.controller_input = controller_input
        self.deck_controller = controller_input.deck_controller
        self.state = state
        self._overlay: Image.Image | None = None
        self.hide_overlay_timer: "timer_wheel.TimerHandle | None" = None

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
        # Snapshot once and use the snapshot throughout: active_page is
        # nulled/swapped from other threads (close() step 8, load_page), and
        # re-reading the live attribute after the None check raced exactly
        # that window (AttributeError out of every own_actions_* caller).
        active_page = self.deck_controller.active_page
        if active_page is None:
            return []
        if active_page.action_objects is None:
            return []
        actions = active_page.get_all_actions_for_input(self.controller_input.identifier, self.state)

        return actions

    def update(self) -> None:
        if self.controller_input.state == self.state:
            self.controller_input.update()
    
    def own_actions_update(self) -> None:
        for action in self.get_own_actions():
            if not isinstance(action, ActionCore):
                continue
            # Gate on ready_finished, not ready_called: the default on_update
            # calls on_ready (compat), so dispatching here mid-initialization
            # ran a second on_ready concurrently with the pool's initial one
            # (duplicate backend processes). Skipping is lossless -- the
            # initial ready sequence ends with its own on_update.
            if not action.on_ready_finished:
                continue
            action.on_update()

    @log.catch
    def own_actions_tick(self) -> None:
        for action in self.get_own_actions():
            if not isinstance(action, ActionCore):
                continue
            # on_ready_called is true from schedule time; ticks must wait for
            # on_ready to actually finish.
            if not action.on_ready_finished:
                continue
            action.on_tick()

    @log.catch
    def own_actions_event_callback(self, event: InputEvent, data: dict = None, show_notifications: bool = False, actions: list = None) -> None:
        # `actions` lets the caller pin the dispatch to a list resolved
        # earlier (ControllerKey's DOWN-time gesture snapshot). By
        # default it's resolved here, when the pool worker actually runs --
        # which reads deck_controller.active_page and therefore tracks any
        # page swap that happened between the event and this dispatch.
        if actions is None:
            actions = self.get_own_actions()
        for action in actions:
            plugin_manager = gl.plugin_manager
            if isinstance(action, ActionOutdated):
                if show_notifications and plugin_manager is not None:
                    plugin_id = plugin_manager.get_plugin_id_from_action_id(action.id)
                    ui_port.get().notify_plugin_problem(plugin_id, "outdated")
                continue
            if isinstance(action, NoActionHolderFound):
                if show_notifications and plugin_manager is not None:
                    plugin_id = plugin_manager.get_plugin_id_from_action_id(action.id)
                    ui_port.get().notify_plugin_problem(plugin_id, "missing")
                continue

            # parsed_event = event
            # if action.allow_event_configuration:
                # parsed_event = action.event_manager.get_event_assigner_for_event(event)

            if event is None:
                continue

            if not isinstance(action, ActionCore):
                continue

            # A pinned snapshot (ControllerKey's DOWN-time gesture list) can
            # outlive its page's cache entry: mark_page_ready_to_clear(True)
            # runs when the DOWN callback returns -- not at gesture end -- so
            # a mid-hold eviction (clear_old_cached_pages), remove_page, or
            # reload-diff can run ActionCore.teardown on a snapshot member
            # (clean_up(): page=None, signals disconnected) while its UP is
            # still owed. Never dispatch into a torn-down action.
            # _cleaned_up is clean_up()'s idempotency marker, set under
            # _cleanup_lock; the lock-free read here is benign -- worst case
            # one event reaches an action mid-teardown, the same envelope as
            # live resolution always had.
            if getattr(action, "_cleaned_up", False):
                continue

            # Per-action isolation: the method-level @log.catch would abort
            # this whole loop at the first raiser, starving every later
            # action in the list of its event.
            try:
                action._raw_event_callback(event, data)
            except Exception:
                log.opt(exception=True).error(
                    f"Action {getattr(action, 'action_id', action)} raised handling {event}"
                )

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

    def own_actions_event_callback_threaded(self, event: InputEvent, data: dict = None, show_notifications: bool = False, actions: list = None) -> None:
        self._submit_action_callback(self.own_actions_event_callback, event, data, show_notifications, actions)

    def set_image(self, image: "InputImage | None", /, update: bool = True) -> None:
        """Attach (or clear, with None) this state's still media.

        The media protocol ActionCore.set_media drives an input state through.
        ControllerKeyState and ControllerDialState implement it;
        ControllerTouchScreenState does not -- and nothing reaches this base
        body today, because ActionCore.set_media early-returns for any
        identifier outside [Input.Key, Input.Dial] (ActionCore.py:190). The
        declaration exists so the protocol is checkable at that call site; a
        future touchscreen media route must override it rather than inherit
        this.
        """
        raise NotImplementedError

    def set_video(self, video: "InputVideo | KeyGIF", /) -> None:
        """Attach this state's animated media. See set_image for who implements
        it and why the base body is unreachable. Both providers are accepted:
        the .gif route builds a KeyGIF, everything else an InputVideo."""
        raise NotImplementedError

    def remove_media(self) -> None:
        page = self.controller_input.deck_controller.active_page
        if page is None:
            return

        # Clearing the media is exactly a None path.
        page.set_media_path(identifier=self.controller_input.identifier, state=self.state, path=None)  # type: ignore[arg-type]  # root cause: Page.set_media_path declares path: str while None is the clear-media value (PageManagement/Page.py)

        self.update()


#: The state class an input owns. Each ControllerInput subclass pins exactly one
#: (ControllerKey -> ControllerKeyState, ...), which is what lets the shared
#: state plumbing below stay in the base class without erasing the subclass's
#: state type at every `get_active_state()` call.
StateT = TypeVar("StateT", bound="ControllerInputState")


class ControllerInput(Generic[StateT]):
    # Per-input dedup slots, created lazily by the paint path (update() reads
    # them through getattr with a None default before the first paint writes
    # them). Declared -- not assigned -- so the annotation adds no attribute at
    # runtime and the lazy-creation contract is unchanged.
    _last_img_hash: int | None
    _last_enqueued_hash: int | None

    def __init__(self, deck_controller: DeckController, state_class: type[StateT], identifier: InputIdentifier):
        self.deck_controller = deck_controller
        self.state = 0
        self.hide_error_timer: Timer | None = None
        self.hold_start_timer: "timer_wheel.TimerHandle | None" = None
        self.ControllerStateClass = state_class
        self.identifier: InputIdentifier = identifier
        self.media_ticks: int = 0
        # Generation of the content this input holds; paints tag it at render
        # start and are dropped at the present boundary once it's superseded.
        self.config_gen: int = 0

        self.is_visual: bool = True

        self.enable_states: bool = True

        # Serializes state-object replacement (create_n_states during a load)
        # against action media writes (ActionCore.set_media): a paint must
        # land either fully before the wipe (so the load's stash-and-restore
        # carries it over) or fully after (on the recreated state object) --
        # never on a destroyed state.
        self._states_lock = threading.RLock()

        self.states: dict[int, StateT] = {
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
            
        page = self.deck_controller.active_page
        if page is None:
            # No page loaded (boot, or mid-teardown): there is nothing to
            # persist the new state onto.
            return
        d = self.identifier.get_config(page)

        # Add new state
        self.states[len(self.states)] = self.ControllerStateClass(self, len(self.states))
        # Write to json
        for state in self.states.keys():
            d["states"].setdefault(str(state), {})

        page.save()
        page_manager = gl.page_manager
        if page_manager is not None:
            page_manager.update_dict_of_pages_with_path(page.json_path)

        self.update_state_switcher()

        if switch:
            log.info(f"Switching to state: {len(self.states)-1}")
            self.set_state(len(self.states)-1)

    def remove_state(self, state: int):
        page = self.deck_controller.active_page
        if page is None:
            # See add_new_state: no page, nothing to edit.
            return
        d = self.identifier.get_config(page)

        if str(state) in d["states"]:
            d["states"].pop(str(state))

        old_loaded_state = int(self.state)

        state_to_remove = self.states.get(state)
        if state_to_remove:
            state_to_remove.close_resources()
            self.states.pop(state)

        # Fill gaps in self.states
        sorted_state_keys = sorted(self.states.keys())

        new_states: dict[int, StateT] = {}
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


        page.save()
        page_manager = gl.page_manager
        if page_manager is not None:
            page_manager.update_dict_of_pages_with_path(page.json_path)

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
        """Kept as the plugin-facing name; the widget work is the adapter's.

        Was an UNGUARDED, un-idled reach into the window's sidebar from
        plugin/action threads -- an AttributeError crash before the window
        existed, and an off-main widget mutation after it.
        """
        ui_port.get().on_input_states_changed(
            self.deck_controller, self.identifier, len(self.states))

    def get_active_state(self) -> StateT:
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
        """Kept as the plugin-facing name; the widget work is the adapter's.

        The visible-child read used to happen on the CALLING thread (an
        off-main GTK read); it now runs inside the adapter's idle together
        with the refresh.
        """
        ui_port.get().on_input_state_selected(
            self.deck_controller, self.identifier, self.state)

    def load_from_config(self, config, update: bool = True):
        n_states = len(config.get("states", {}))
        self.create_n_states(max(1, n_states))

        old_state_index = self.state

        self.state = 0

        #TODO: Reset states
        for state_key in config.get("states", {}):
            state = self.states.get(int(state_key))
            if state is None:
                continue

            state_dict = config["states"][str(state.state)]

            if update:
                self.set_state(old_state_index)
                self.update()

    def clear(self, update: bool = True):
        active_state = self.get_active_state()
        # Abstract-by-convention: ControllerKeyState and ControllerTouchScreenState
        # define clear(); a dial therefore raises AttributeError here.
        # Pre-existing and still unfixed.
        active_state.clear()  # type: ignore[attr-defined]  # root cause: ControllerDialState has no clear()
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
    
    def get_empty_background(self) -> Image.Image | None:
        # No ControllerInput subclass overrides this, so the base's None is
        # what every caller actually gets (KeyImage tolerates it).
        return None

    def get_image_size(self) -> tuple[int, int]:
        # Overridden by ControllerKey/ControllerTouchScreen/ControllerDial --
        # the base is never the one that answers.
        raise NotImplementedError

class ControllerKey(ControllerInput["ControllerKeyState"]):
    def __init__(self, deck_controller: DeckController, ident: Input.Key):
        super().__init__(deck_controller, ControllerKeyState, ident)
        self.index = ident.get_index(deck_controller)
        # Seed the cached press state from the device so event_callback can diff
        # against it. key_states() is logical-indexed (rotation applied there),
        # so self.index -- a logical index -- selects this key's own state.
        self.press_state: bool = self.deck_controller.deck.key_states()[self.index]

        self.down_start_time: float | None = None

        # DOWN-time gesture snapshot: a (state, actions) pair captured
        # when the key went down, or None outside a gesture. The rest of the
        # gesture (HOLD_START, HOLD_STOP/SHORT_UP, UP) dispatches to this
        # snapshot, NOT to whatever the key resolves to at release time --
        # a ChangePage action on this key swaps active_page (and rebuilds
        # this key's states) synchronously during the DOWN dispatch, which
        # used to send the UP to the NEW page's actions: the old page's
        # actions never saw their release (RunCommand's registered_down
        # latch then jammed shut) while the new page's
        # actions got a spurious SHORT_UP for a press that wasn't theirs.
        # A single attribute (not one per field) so writers clear it in one
        # atomic store and the hold-timer callback -- which can race the UP
        # branch past its cancel() -- reads a coherent pair or None, never a
        # torn half. Written from the deck's serialized input-callback path
        # and from ScreenSaver.show()'s cancel_gesture sweep (under
        # _load_page_lock, after this key was swapped out of the live input
        # set and can receive no further events).
        self._gesture: tuple | None = None

    def cancel_gesture(self) -> None:
        """Ends an in-flight gesture without dispatching its release events:
        drops the DOWN-time snapshot, the gesture clock, and the pending
        hold timer. For paths where the physical release can never reach
        this key -- ScreenSaver.show() confiscates the whole input set
        mid-hold (the release then lands on the replacement key and is
        swallowed), which otherwise left this key's hold timer armed to
        fire HOLD_START into the pinned snapshot after the finger already
        left, and kept that snapshot's action objects pinned forever."""
        self.down_start_time = None
        self.stop_hold_timer()
        self._gesture = None

    def on_hold_timer_end(self):
        gesture = self._gesture
        if gesture is None:
            # The gesture already ended: the UP branch or a cancel_gesture()
            # raced this callback past the timer's cancel(). A late
            # HOLD_START must not fire at all -- and especially must not
            # live-resolve onto whatever page happens to be active now.
            return
        gesture_state, gesture_actions = gesture
        gesture_state.own_actions_event_callback_threaded(
            event=Input.Key.Events.HOLD_START,
            actions=gesture_actions,
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

        # Frame-identity fast path: a passthrough key over a video
        # background composites to exactly the shared tile, so its native
        # bytes are a pure function of the frame it came from -- no pixels
        # have to be serialized, hashed or re-encoded to know what belongs
        # on the device. Steady-state playback of a loop is then a dict
        # lookup plus the USB write.
        if self.deck_controller.native_tile_cache.enabled and self._tile_passthrough_ok(self.get_active_state()):
            identified = self.deck_controller.background.get_identified_tile(self.index)
            if identified is not None:
                self._update_from_tile_identity(identified, page, config_gen, force)
                return

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
                rgb_image = self._to_rotated_rgb(image)
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

    def _to_rotated_rgb(self, image: Image.Image) -> Image.Image:
        """The device-ready RGB form of a composited key image. Handles
        transparency properly - composites RGBA onto RGB to preserve smooth
        edges. Never mutates `image` (both branches build a new one), which
        is what lets the frame-identity path pass the SHARED background tile
        in without copying it first."""
        rotation = self.deck_controller.deck.get_rotation()
        if image.mode == "RGBA":
            rgb_background = Image.new("RGB", image.size, (0, 0, 0))
            rgb_background.paste(image, (0, 0), image)
            return rgb_background.rotate(rotation)
        return image.convert("RGB").rotate(rotation)

    def _update_from_tile_identity(self, identified: tuple, page, config_gen, force: bool) -> None:
        """Presents a passthrough key straight from its frame identity (see
        update()). `identified` is the (tile, (video md5, frame index)) pair
        Background handed out as one read."""
        tile, (video_md5, frame_index) = identified

        if media_prof:
            _t0 = time.perf_counter()

        # Stands in for the pixel hash wherever the present-boundary
        # bookkeeping uses one: stable for a frame, distinct across frames
        # and keys. The skip still needs BOTH the last presented hash
        # (_last_img_hash, set in the task's run()) and the last enqueued
        # one to match -- either alone can be stale (dropped paint /
        # in-flight revert) and would wrongly skip the correcting repaint.
        img_hash = hash(("vidtile", video_md5, frame_index, self.index))
        if (not force and img_hash == getattr(self, '_last_img_hash', None)
                and img_hash == getattr(self, '_last_enqueued_hash', None)):
            if media_prof:
                media_prof.count("hash_skip")
            return

        if self.deck_controller.is_visual():
            cache_key = (video_md5, frame_index, self.index,
                         self.deck_controller.deck.get_rotation(),
                         KEY_ENCODE_QUALITY,
                         self.deck_controller.native_key_format_sig())
            native_image = self.deck_controller.native_tile_cache.get(cache_key)
            if native_image is None:
                rgb_image = self._to_rotated_rgb(tile)
                native_image = encode_native_key(self.deck_controller.deck, rgb_image)
                rgb_image.close()
                self.deck_controller.native_tile_cache.put(cache_key, native_image)
                if media_prof:
                    media_prof.add("encode", time.perf_counter() - _t0)
                    media_prof.count("native_id_miss")
            elif media_prof:
                media_prof.count("native_id_hit")
            self._last_enqueued_hash = img_hash
            self.deck_controller.media_player.add_image_task(self.index, native_image, page=page, config_gen=config_gen, controller_key=self, img_hash=img_hash)

        # The in-app preview still wants a PIL image, and the tile is shared
        # with every other reader of this frame -- hand the UI its own copy.
        self.set_ui_key_image(copy(tile))

    def get_active_state(self) -> "ControllerKeyState":
        return super().get_active_state()

    def on_media_player_tick(self) -> None:
        self.media_ticks += 1

        state = self.get_active_state()
        needs_update = False

        # Rolling labels advance their state here, on the tick, whether or
        # not anything else forces a repaint (rendering is pure); the key
        # only re-renders when a scroll offset visibly moved, instead of 30x
        # a second producing frames the hash de-dup discards.
        scroll_moved = False
        if state.label_manager.get_has_scroll_labels():
            scroll_moved = state.label_manager.tick_scroll_labels()

        # Check if we need to update based on content type
        if state.key_video is not None:
            # Both InputVideo and KeyGIF now pick their current frame from
            # their own wall-clock timeline (presenter-migration-plan.md §4
            # M4); the tick just asks for whatever frame is current -- it no
            # longer needs to pre-compute whether the GIF's frame delay has
            # elapsed. This also matches how non-GIF videos were already
            # handled here (unconditional needs_update).
            needs_update = True
        elif scroll_moved:
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
            if not press_state:
                # A release swallowed by the screensaver still ends the
                # physical gesture: without this, a snapshot pinned by a
                # pre-screensaver DOWN would never be dropped and its hold
                # timer would keep running -- firing HOLD_START after the
                # finger already left. (Belt-and-braces: show() already
                # cancels gestures on the input set it stashes, so a live
                # gesture on THIS key here means the screensaver engaged
                # without the swap -- keep the two paths independent.)
                self.cancel_gesture()
            return

        pressed_page = self.deck_controller.mark_page_ready_to_clear(False)
        self.press_state = press_state

        self.update()

        active_state = self.get_active_state()
        if press_state: # Key down
            self.down_start_time = time.time()
            # Snapshot the state and its resolved actions NOW (see
            # __init__): every event of this gesture -- including this DOWN,
            # which otherwise resolves actions only when the pool worker
            # runs -- goes to the actions that were on the key when the
            # finger landed, regardless of page swaps in between.
            gesture_actions = active_state.get_own_actions()
            self._gesture = (active_state, gesture_actions)
            self.start_hold_timer()
            active_state.own_actions_event_callback_threaded(
                event=Input.Key.Events.DOWN,
                show_notifications=True,
                actions=gesture_actions
            )

        elif self.down_start_time is not None: # Key up
            gesture = self._gesture
            if gesture is not None:
                gesture_state, gesture_actions = gesture
            else:
                gesture_state, gesture_actions = active_state, None
            if time.time() - self.down_start_time >= self.deck_controller.hold_time:
                gesture_state.own_actions_event_callback_threaded(
                    event=Input.Key.Events.HOLD_STOP,
                    actions=gesture_actions
                )
            else:
                gesture_state.own_actions_event_callback_threaded(
                    event=Input.Key.Events.SHORT_UP,
                    actions=gesture_actions
                )
            self.down_start_time = None
            self.stop_hold_timer()
            gesture_state.own_actions_event_callback_threaded(
                event=Input.Key.Events.UP,
                show_notifications=False,
                actions=gesture_actions
            )
            # Gesture complete: drop the snapshot (single atomic store, see
            # __init__) so a superseded page's action objects aren't pinned
            # past their last event.
            self._gesture = None

        else: # Key up with no gesture clock
            # The matching DOWN was swallowed or its bookkeeping already
            # cleared (e.g. a screensaver show/hide cycle mid-hold resets
            # down_start_time on the live keys). Nothing to dispatch, but a
            # still-armed hold timer or pinned snapshot from that orphaned
            # DOWN must not outlive the physical release.
            self.cancel_gesture()
        # Reset the SAME page the False-call marked -- a press
        # that triggers a page change would otherwise pin the old page.
        self.deck_controller.mark_page_ready_to_clear(True, pressed_page)

    def _tile_passthrough_ok(self, state: "ControllerKeyState") -> bool:
        """Whether this key composites to exactly the shared background tile
        -- no color layer, media, labels, or markers over it. Gates both the
        composite fast path (get_current_image) and the frame-identity fast
        path (update); one definition so the two can never disagree about
        which keys are bare."""
        return (state.background_manager.get_composed_color()[-1] == 0
                and state._overlay is None
                and state.key_image is None
                and state.key_video is None
                and not state.label_manager.get_has_visible_labels()
                and not self.is_pressed()
                and not (self.has_unavailable_action() and not self.deck_controller.screen_saver.showing))

    def get_current_image(self) -> Image.Image:
        state = self.get_active_state()

        # A bare key's composite IS the shared background tile; return a copy
        # of it directly (matters per-frame over an animated background).
        if self._tile_passthrough_ok(state):
            tile = self.deck_controller.background.tiles[self.index]
            if tile is not None:
                if media_prof:
                    media_prof.count("tile_passthrough")
                return copy(tile)

        background_color = state.background_manager.get_composed_color()

        if media_prof:
            _t0 = time.perf_counter()

        background: Image.Image | None = None
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


        key_image: Image.Image | None = None
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

        # A key with no visible label gets its own composite handed straight
        # back (add_labels_to_image skips the copy), and with no media
        # key_image IS background -- so closing either unconditionally would
        # hand the media thread an image whose buffer is already released.
        if background is not None and background is not labeled_image:
            background.close()

        if key_image is not labeled_image:
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

        # create_n_states destroys every state object, closing any action-set
        # media; afterwards only on_update() can repaint, and an action that
        # dedups there never does -- the key settled permanently blank.
        # Detach action-owned media (plus its action layout) before the
        # wipe and restore it only when the exact action object that painted
        # it still drives the recreated state: a same-page reload reuses the
        # action objects (identity match -> restore, no blank), a cross-page
        # load builds new ones (mismatch -> close, no bleed -- pinned by
        # scenario_wipe_no_bleed). Under _states_lock so a concurrent
        # set_media paint lands either fully before the wipe (stash carries
        # it over) or fully after (on the recreated state) -- never on a
        # destroyed state object.
        with self._states_lock:
            stashed: dict[int, tuple] = {}
            for index, old_state in self.states.items():
                owner = old_state.media_owner_action
                if owner is None:
                    continue
                if old_state.key_image is None and old_state.key_video is None:
                    continue
                stashed[index] = (owner, old_state.key_image, old_state.key_video,
                                  old_state.layout_manager.action_layout)
                old_state.key_image = None
                old_state.key_video = None
                old_state.media_owner_action = None

            self.create_n_states(max(1, n_states))

            restored: set[int] = set()
            for index, (owner, key_image, key_video, action_layout) in stashed.items():
                new_state = self.states.get(index)
                if new_state is not None and owner in new_state.get_own_actions():
                    new_state.key_image = key_image
                    new_state.key_video = key_video
                    new_state.media_owner_action = owner
                    new_state.layout_manager.set_action_layout(action_layout, update=False)
                    restored.add(index)
                else:
                    if key_image is not None:
                        key_image.close()
                    if key_video is not None:
                        key_video.close()

        old_state_index = self.state

        self.state = 0

        #TODO: Reset states
        for state_key in input_dict.get("states", {}):
            state = self.states.get(int(state_key))
            if state is None:
                continue

            state_dict = input_dict["states"][str(state.state)]

            if load_labels:
                state.label_manager.clear_labels()

            # Reset action layout -- except for a state whose action-owned
            # media was just restored above: its action layout belongs to the
            # same still-present action, and resetting it would half-restore
            # the paint (image back, alignment/size lost).
            if state.state not in restored:
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
                media = MediaConfig.from_dict(state_dict.get("media", {}))
                path = media.path
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
                        key_gif = None
                        if os.path.splitext(path)[1].lower() == ".gif":
                            # KeyGIF parses eagerly and RAISES on a corrupt or
                            # truncated GIF, where InputVideo's detached cv2
                            # builder fails soft. Unguarded, one bad asset in a
                            # page's config took the whole page load down with
                            # it. The set_media route already had this
                            # try/except; this one did not. Same policy,
                            # same fallback: the opaque cv2 path.
                            #
                            # Scope, stated honestly: this contains the
                            # GIF-SPECIFIC parse/decode failures. It does not
                            # make page load total -- InputVideo's own
                            # constructor stats and hashes the file, so the
                            # EACCES/EIO/ENOENT class still escapes from the
                            # fallback itself, exactly as it does for every
                            # non-GIF video on this route (pre-existing; the
                            # whole media block would need the guard to close
                            # that, which is not this issue's scope).
                            try:
                                key_gif = KeyGIF(
                                    controller_key=self,
                                    gif_path=path,
                                    loop=media.loop,
                                    fps=media.fps
                                )
                            except Exception:
                                log.opt(exception=True).warning(
                                    f"GIF decode failed during page load, falling "
                                    f"back to the opaque cv2 path: {path}")
                        if key_gif is not None:
                            state.set_video(key_gif) # GIFs always update
                        else:
                            state.set_video(InputVideo(
                                controller_input=self,
                                video_path=path,
                                loop=media.loop,
                                fps=media.fps,
                                # User-assigned media plays at the source's
                                # speed; the dict fps (sidebar FPS row) is a
                                # render cap. Plugin media via set_media keeps
                                # fps-as-playback-rate -- an explicit API arg.
                                natural_speed=True,
                            )) # Videos always update
                    # No further elif here on purpose: two action-count
                    # branches used to hang off this chain calling
                    # self.set_key_image(...), which ControllerKey does not
                    # have. That branch was NOT unreachable -- it fired on the
                    # normal load_media=True path whenever `path` was a
                    # non-empty string that is not a valid image/svg/video
                    # (e.g. a stale/dangling config path), raising
                    # AttributeError. Dropped in 0d10fb3b so a bad path is a
                    # benign no-op instead of a crash; don't re-add without a
                    # real set_key_image.

                layout = ImageLayout(
                    fill_mode=media.fill_mode,
                    size=media.size,
                    valign=media.valign,
                    halign=media.halign,
                )
                state.layout_manager.set_page_layout(layout, update=False)

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

        if not ui_port.get().push_input_image(self.deck_controller, self.identifier, image):
            # Refused (no UI, window unmapped, grid mid-rebuild) or the push
            # raised: mark dirty only (P5.4) -- KeyGrid.load_from_changes
            # recomposites a fresh image on map instead of replaying `image`.
            # A frame the port ACCEPTS but later drops marks itself; see
            # ui_adapter.mark_dirty.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True


    def get_own_ui_key(self):
        """Deprecated in-process shim: the attached UI resolves its own
        widget for this input. None when headless."""
        return ui_port.get().query_input_widget(self.deck_controller, self.identifier)
    
    def get_image_size(self) -> tuple[int, int]:
        return self.deck_controller.get_key_image_size()

class ControllerTouchScreen(ControllerInput["ControllerTouchScreenState"]):
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

    def get_image_size(self) -> tuple[int, int]:
        # InputVideo sizes its frame cache from this (KeyVideo.py) -- for the
        # touchscreen that is the full strip.
        return self.get_screen_dimensions()

    def on_media_player_tick(self) -> bool:
        # A per-touchscreen background video advances on the media tick like
        # dial content does; the caller re-composites the shared touchscreen
        # once per frame. The screensaver owns the strip while it is showing.
        if self.deck_controller.screen_saver.showing:
            return False
        state = self.get_active_state()
        # Snapshot: _release_background_video() nulls
        # this from compositing threads between the check and the .fps read.
        bg_video = None if state is None else state.background_video
        if bg_video is None:
            return False
        # The configured fps is a RENDER cap: playback position is wall-clock
        # at the source's native fps (InputVideo natural_speed), so skipping
        # ticks here drops frames without slowing the video down.
        cap_fps = min(self.deck_controller.media_player.FPS, max(1, bg_video.fps or 30))
        now = time.time()
        if now - state._last_background_video_render < 1.0 / cap_fps:
            return False
        state._last_background_video_render = now
        return True

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
        if not ui_port.get().push_input_image(self.deck_controller, self.identifier, image):
            # Mark dirty only (P5.4) -- ScreenBar.load_from_changes
            # recomposites a fresh image on map instead of replaying `image`.
            # The preview throttle (and its tail flush, which re-marks a frame
            # the window unmapped out from under) lives in the adapter now.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True

    def get_current_image(self) -> Image.Image:
        active_state = self.get_active_state()
        return active_state.get_current_image()

    def event_callback(self, event_type, value):
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if event_type in (TouchscreenEventType.SHORT, TouchscreenEventType.LONG, TouchscreenEventType.DRAG):
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            return
        
        # Touchscreen events arrive pre-classified from the library (SHORT/
        # LONG/DRAG, single events -- no DOWN/UP tail, so no gesture snapshot
        # to keep). But the default dispatch resolves the target actions
        # against active_page when the pool worker runs, so a page swap in
        # the event->worker window used to redirect the event to the new
        # page's actions (the same window as the dial TURN case). Resolve
        # at READ time instead, here on the deck's input thread.
        active_state = self.get_active_state()
        if event_type == TouchscreenEventType.DRAG:
            drag_actions = active_state.get_own_actions()
            # Check if from left to right or the other way
            if value['x'] > value['x_out']:
                active_state.own_actions_event_callback_threaded(
                    Input.Touchscreen.Events.DRAG_LEFT,
                    actions=drag_actions
                )
            else:
                active_state.own_actions_event_callback_threaded(
                    Input.Touchscreen.Events.DRAG_RIGHT,
                    actions=drag_actions
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

                    touch_actions = dial_active_state.get_own_actions()
                    dial_active_state.own_actions_event_callback_threaded(
                        event,
                        data={"x": value['x'], "y": value['y']},
                        show_notifications=True,
                        actions=touch_actions
                    )

    def get_dial_for_touch_x(self, touch_x: float) -> "ControllerDial | None":
        screen_width = self.deck_controller.get_touchscreen_image_size()[0]
        n_dials = len(self.deck_controller.inputs[Input.Dial])
        dial_index = int((touch_x / screen_width) * n_dials)

        return self.deck_controller.get_input(Input.Dial(str(dial_index)))
    
    def get_screen_dimensions(self) -> tuple[int, int]:
        return self.deck_controller.get_touchscreen_image_size()

class ControllerDial(ControllerInput["ControllerDialState"]):
    def __init__(self, deck_controller: DeckController, ident: InputIdentifier):
        super().__init__(deck_controller, ControllerDialState, ident)

        self.down_start_time: float | None = None

        # DOWN-time gesture snapshot -- the dial twin of
        # ControllerKey._gesture (see its __init__ for the full
        # rationale): a (state, actions) pair captured when the dial went
        # down, or None outside a gesture. The gesture tail (HOLD_START,
        # HOLD_STOP/SHORT_UP, UP) dispatches to this snapshot, not to
        # whatever the dial resolves to at release time -- a ChangePage on
        # this dial's DOWN swaps active_page mid-gesture, which used to send
        # the tail to the NEW page's dial actions (jamming EasyCommand's
        # registered_down latch the same way). Single attribute
        # so writers clear it in one atomic store and the hold-timer callback
        # reads a coherent pair or None, never a torn half.
        self._gesture: tuple | None = None

    def cancel_gesture(self) -> None:
        """Ends an in-flight gesture without dispatching its release events:
        drops the DOWN-time snapshot, the gesture clock, and the pending
        hold timer. Same contract as ControllerKey.cancel_gesture -- for
        paths where the physical release can never reach this dial
        (ScreenSaver.show() confiscates the whole input set mid-hold; the
        release then lands on the replacement dial and is swallowed)."""
        self.down_start_time = None
        self.stop_hold_timer()
        self._gesture = None

    def on_hold_timer_end(self):
        gesture = self._gesture
        if gesture is None:
            # The gesture already ended: the UP branch or a cancel_gesture()
            # raced this callback past the timer's cancel(). A late
            # HOLD_START must not fire at all -- and especially must not
            # live-resolve onto whatever page happens to be active now.
            return
        gesture_state, gesture_actions = gesture
        gesture_state.own_actions_event_callback_threaded(
            event=Input.Dial.Events.HOLD_START,
            actions=gesture_actions,
        )

    def get_touch_screen(self) -> "ControllerTouchScreen | None":
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
            if event_type == DialEventType.PUSH and not value:
                # A release swallowed by the screensaver still ends the
                # physical gesture (see ControllerKey.event_callback's
                # matching branch). Belt-and-braces: show() already cancels
                # gestures on the input set it stashes.
                self.cancel_gesture()
            return

        active_state = self.get_active_state()
        if event_type == DialEventType.PUSH:
            if value:
                self.down_start_time = time.time()
                # Snapshot the state and its resolved actions NOW (see
                # __init__): every event of this gesture -- including this
                # DOWN, which otherwise resolves actions only when the pool
                # worker runs -- goes to the actions that were on the dial
                # when it was pressed, regardless of page swaps in between.
                gesture_actions = active_state.get_own_actions()
                self._gesture = (active_state, gesture_actions)
                self.start_hold_timer()
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.DOWN,
                    show_notifications=True,
                    actions=gesture_actions
                )
            elif self.down_start_time is not None:
                gesture = self._gesture
                if gesture is not None:
                    gesture_state, gesture_actions = gesture
                else:
                    gesture_state, gesture_actions = active_state, None
                self.stop_hold_timer()
                if time.time() >= self.down_start_time + self.deck_controller.hold_time:
                    gesture_state.own_actions_event_callback_threaded(
                        event=Input.Dial.Events.HOLD_STOP,
                        actions=gesture_actions
                    )
                else:
                    gesture_state.own_actions_event_callback_threaded(
                        event=Input.Dial.Events.SHORT_UP,
                        actions=gesture_actions
                    )
                self.down_start_time = None
                gesture_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.UP,
                    actions=gesture_actions
                )
                # Gesture complete: drop the snapshot (single atomic store,
                # see __init__) so a superseded page's action objects aren't
                # pinned past their last event.
                self._gesture = None
            else:
                # Release with no gesture clock: the matching DOWN was
                # swallowed or its bookkeeping already cleared. Nothing to
                # dispatch, but a still-armed hold timer or pinned snapshot
                # from that orphaned DOWN must not outlive the release.
                self.cancel_gesture()

        elif event_type == DialEventType.TURN:
            # Resolve the target actions at READ time: a turn is a
            # single event, but the default dispatch resolves against
            # active_page when the pool worker runs -- a page swap in that
            # window used to redirect the turn to the new page's actions.
            turn_actions = active_state.get_own_actions()
            # value is the HID report's signed detent count — fast rotation
            # coalesces several detents into one report, so forward the
            # magnitude instead of collapsing it to a single event.
            if value < 0:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.TURN_CCW,
                    data={"ticks": -value},
                    actions=turn_actions
                )
            else:
                active_state.own_actions_event_callback_threaded(
                    event=Input.Dial.Events.TURN_CW,
                    data={"ticks": value},
                    actions=turn_actions
                )

    def load_from_input_dict(self, page_dict, update: bool = True):
        n_states = len(page_dict.get("states", {}))
        self.create_n_states(max(1, n_states))

        old_state_index = self.state

        self.state = 0

        for state_key in page_dict.get("states", {}):
            state = self.states.get(int(state_key))
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
            media = MediaConfig.from_dict(state_dict.get("media", {}))
            path = media.path
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
                            loop=media.loop,
                            fps=media.fps
                        )) # GIFs always update
                    else:
                        state.set_video(InputVideo(
                            controller_input=self,
                            video_path=path,
                            loop=media.loop,
                            fps=media.fps,
                            # User-assigned media plays at the source's speed;
                            # the dict fps (sidebar FPS row) is a render cap.
                            # Plugin media via set_media keeps
                            # fps-as-playback-rate -- an explicit API arg.
                            natural_speed=True,
                        )) # Videos always update

            layout = ImageLayout(
                fill_mode=media.fill_mode,
                size=media.size,
                valign=media.valign,
                halign=media.halign,
            )
            state.layout_manager.set_page_layout(layout, update=False)

            state.background_manager.set_page_color(state_dict.get("background", {}).get("color", [0, 0, 0, 0]), update=False)

        if update:
            self.set_state(old_state_index)
            self.update()

    def update(self):
        if self.deck_controller.deck.is_touch():
            touch_screen = self.get_touch_screen()
            if touch_screen is not None:
                touch_screen.update()

    def get_active_state(self) -> "ControllerDialState":
        return super().get_active_state()

    def on_media_player_tick(self) -> bool:
        # Advance the animation clock and report whether a redraw is needed;
        # the caller renders the shared touchscreen once per frame.
        self.media_ticks += 1

        state = self.get_active_state()
        if state is None:
            return False
        # Rolling labels advance here on the tick (rendering is pure); the
        # strip only re-renders when a scroll offset visibly moved.
        scroll_moved = False
        if state.label_manager.get_has_scroll_labels():
            scroll_moved = state.label_manager.tick_scroll_labels()
        return state.video is not None or scroll_moved

    def get_image_size(self) -> tuple[int, int]:
        if self.deck_controller.deck.is_touch():
            touch_screen = self.get_touch_screen()
            if touch_screen is not None:
                return touch_screen.get_dial_image_area_size()
        # (0, 0) is the established "no visual target" answer for a dial with
        # no strip -- KeyImage._budget_size keys off exactly this.
        return (0, 0)
    

class ControllerTouchScreenState(ControllerInputState):
    # Created lazily by set_current_image() -- close_resources() getattr-guards
    # exactly because a state closed before its first render never has one.
    # Declared (not assigned) so that contract is unchanged at runtime.
    current_image: Image.Image | None

    def __init__(self, controller_touch: "ControllerTouchScreen", state: int):
        super().__init__(controller_touch, state)

        self.controller_touch = controller_touch

        # (key, fitted-image-or-None) for _get_fitted_background_image.
        self._fitted_background_cache: "tuple[tuple | None, Image.Image | None]" = (None, None)

        # Playback state for a VIDEO configured as this touchscreen's
        # background: an InputVideo over a strip-sized shared frame cache,
        # advanced by the media tick (see ControllerTouchScreen.
        # on_media_player_tick). Managed by _get_background_video_frame;
        # get_current_image releases it when the background stops being a
        # video. The lock covers create/release -- composites can run on the
        # media thread and on load/UI threads concurrently.
        # Either provider: the .gif route builds a GifBackground, everything
        # else an InputVideo. Both answer the get_next_frame/close/video_path
        # surface _get_background_video_frame drives them through.
        self.background_video: "InputVideo | GifBackground | None" = None
        self._background_video_failed: str | None = None
        self._background_video_lock = threading.Lock()
        # The display-saturation factor background_video was constructed
        # (and its shared tile cache acquired) at. Part of the keep-check in
        # _get_background_video_frame: the factor is baked into the cache at
        # construction and set_playback never revisits it, so reusing the
        # video across a saturation change would keep serving frames
        # enhanced at the old factor.
        self._background_video_saturation: float | None = None
        # Timestamp gate for the fps render cap in on_media_player_tick.
        self._last_background_video_render: float = 0.0

    def set_current_image(self, image: Image.Image):
        self.current_image = image

        self.update()

    def _get_fitted_background_image(self, path: str, size: tuple[int, int]) -> Image.Image | None:
        # Decode + fit once per (path, mtime, size, saturation) and cache:
        # this runs on every composite (30/s while a background video plays),
        # and a failed decode must not log per frame. Videos take the playback
        # path in _get_background_video_frame instead.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None

        # The saturation boost is baked into the cached fitted image (same
        # one-time contract as BackgroundImage for the key grid), so the
        # factor is part of the cache key -- a saturation change must not
        # keep serving the stale enhancement from before it. Rounded to the
        # persisted 2-decimal precision (set_display_saturation stores
        # round(v, 2)) so a future unrounded caller can't mint a near-
        # duplicate float key that misses the cache every composite.
        saturation = round(self.controller_touch.deck_controller.get_display_saturation(), 2)

        key = (path, mtime, size, saturation)
        cached_key, cached_image = self._fitted_background_cache
        if cached_key == key:
            # Callers paste dial images onto the returned image in place --
            # hand out a copy so the cache stays pristine.
            return cached_image.copy() if cached_image is not None else None

        image = None
        try:
            with Image.open(path) as img:
                image = img.copy()
        except Exception as e:
            log.error(f"Error loading touchscreen background image {path}: {e}")

        fitted = None
        if image is not None:
            fitted = ImageOps.fit(image, size, Image.Resampling.LANCZOS).convert("RGBA")
            if abs(saturation - 1.0) > 0.001:
                fitted = ImageEnhance.Color(fitted).enhance(saturation)

        # Failures are cached too: a bad file logs once, not every frame.
        self._fitted_background_cache = (key, fitted)
        return fitted.copy() if fitted is not None else None

    def _get_background_video_frame(self, path: str, fps: int = 30, loop: bool = True) -> Image.Image | None:
        # The InputVideo owns a strip-sized shared frame cache
        # (mp4_tile_cache); frame picking is wall-clock, gap-clamped, and --
        # natural_speed -- runs at the SOURCE's fps, so neither composite
        # rate nor the fps setting changes playback speed. fps/loop come from
        # the page's background settings (sidebar background editor): loop
        # wraps playback, fps only caps the strip's re-render rate (see
        # ControllerTouchScreen.on_media_player_tick).
        with self._background_video_lock:
            if path == self._background_video_failed:
                return None

            # Saturation is part of the keep-check: the factor
            # is baked into the video's shared tile cache at construction
            # (mp4_tile_cache.acquire) and set_playback only updates
            # fps/loop, so a factor change must rebuild even for the same
            # path -- mirroring the key-grid BackgroundVideo keep-check and
            # the fitted-IMAGE cache key one method up. Same 0.001 tolerance
            # as the BackgroundVideo check.
            saturation = self.controller_touch.deck_controller.get_display_saturation()

            video = self.background_video
            # Both reads stay INSIDE the short-circuit: _background_video_saturation
            # is only guaranteed present once a video has been attached (the
            # keepcheck scenario builds this state via __new__ and sets only the
            # attributes the no-video path touches).
            if (video is None or video.video_path != path
                    or self._background_video_saturation is None
                    or abs(self._background_video_saturation - saturation) > 0.001):
                if video is not None:
                    video.close()
                video = None
                if os.path.splitext(path)[1].lower() == ".gif":
                    # .gif diverts to the PIL provider: frames
                    # are fitted to EXACTLY the strip size -- the
                    # alpha_composite in get_current_image needs same-size
                    # RGBA -- and alpha + per-frame delays survive. Budget/
                    # decode failure falls back to the InputVideo path below
                    # (opaque, source-fps -- today's behavior), parity with
                    # the deck-background route in prebuild_from_path.
                    try:
                        video = GifBackground(
                            self.controller_touch.deck_controller, path,
                            loop=loop, fps=fps,
                            canvas_size=self.controller_touch.get_screen_dimensions(),
                        )
                    except GifBudgetExceeded as e:
                        log.warning(f"GIF strip background over budget, falling back to the opaque cv2 path: {e}")
                    except Exception:
                        log.opt(exception=True).warning(f"GIF strip background decode failed, falling back to the opaque cv2 path: {path}")
                if video is None:
                    video = InputVideo(
                        controller_input=self.controller_touch,
                        video_path=path,
                        fps=fps,
                        loop=loop,
                        natural_speed=True,
                    )
                self.background_video = video
                self._background_video_saturation = saturation
            else:
                video.set_playback(fps=fps, loop=loop)

            frame = video.get_next_frame()
            if frame is None:
                # n_frames is known from construction (the reader opens its
                # source eagerly), so <=0 is a deterministically bad file:
                # fail it once instead of retrying (and logging) per frame.
                # A transient miss on a healthy file just retries next tick.
                # InputVideo only: GifBackground has no video_cache (a bad
                # GIF already fell back at construction; post-close None is
                # transient and self-heals on the rebuild above).
                if hasattr(video, "video_cache") and (video.video_cache is None or video.video_cache.n_frames <= 0):
                    log.error(f"Could not decode touchscreen background video {path}")
                    video.close()
                    self.background_video = None
                    self._background_video_failed = path
                return None

            # convert() copies -- dial images get pasted onto the returned
            # composite in place, and the cache's payload must stay pristine.
            return frame.convert("RGBA")

    def _release_background_video(self) -> None:
        with self._background_video_lock:
            if self.background_video is not None:
                self.background_video.close()
                self.background_video = None

    def get_current_image(self) -> Image.Image:
        screen_width, screen_height = self.controller_touch.get_screen_dimensions()

        # Start with background image if set
        background: Image.Image | None = None
        # Snapshot + guard: load_page(None) and close()
        # step 8 null active_page from other threads while the writer
        # composites; a blank strip is the only sensible frame then.
        active_page = self.controller_touch.deck_controller.active_page
        if active_page is None:
            return Image.new("RGBA", (screen_width, screen_height), (0, 0, 0, 255))
        background_image_path = active_page.get_background_image(
            identifier=self.controller_touch.identifier, 
            state=self.state
        )
        
        has_video_background = bool(
            background_image_path
            and os.path.isfile(background_image_path)
            and is_video(background_image_path)
        )
        if not has_video_background:
            # The background stopped being a video (cleared or swapped to an
            # image): detach its frame cache so the tick predicate goes quiet.
            self._release_background_video()

        if background_image_path and os.path.isfile(background_image_path):
            if has_video_background:
                background = self._get_background_video_frame(
                    background_image_path,
                    fps=active_page.get_background_fps(identifier=self.controller_touch.identifier, state=self.state),
                    loop=active_page.get_background_loop(identifier=self.controller_touch.identifier, state=self.state),
                )
            else:
                background = self._get_fitted_background_image(background_image_path, (screen_width, screen_height))

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
        # Detach the background video's shared-cache reader like
        # ControllerKeyState/ControllerDialState release their videos.
        self._release_background_video()

class ControllerDialState(ControllerInputState):
    def __init__(self, dial: "ControllerDial", state: int):
        self.dial = dial

        self.image: InputImage | None = None
        # Typed to the base protocol's provider union (see
        # ControllerInputState.set_video). Only the KEY route constructs a
        # KeyGIF today -- ActionCore's .gif branch is ControllerKey-guarded --
        # but the slot and the render path (get_next_frame) handle either.
        self.video: "InputVideo | KeyGIF | None" = None

        self.touch_image: Image.Image | None = None

        super().__init__(dial, state)

    def set_image(self, image: "InputImage | None", update: bool = True) -> None:
        if self.image is not None:
            self.image.close()

        self.image = image

        if update:
            self.update()

    def set_video(self, video: "InputVideo | KeyGIF") -> None:
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
        if touch_screen is None:
            # A dial without a strip has nowhere to render; get_image_size()
            # reports (0, 0) for exactly this deck shape.
            return Image.new("RGBA", self.dial.get_image_size(), (0, 0, 0, 0))

        background: Image.Image | None = None

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
        

        if background is None:
            # Unreachable: every 0..255 alpha satisfies one of the two branches
            # above. Mirrors ControllerKey.get_current_image's same fallback so
            # the composite below always has a canvas.
            background = touch_screen.get_empty_dial_image()

        image: Image.Image | None = None
        if self.video is not None:
            image = self.video.get_next_frame()
        elif self.image is not None:
            image = self.image.image

        # rotation = self.deck_controller.get_deck_settings().get("rotation", {}).get("value", 0)

        composed = self.layout_manager.add_image_to_background(image, background)
        return self.label_manager.add_labels_to_image(composed)

class ControllerKeyState(ControllerInputState):
    def __init__(self, controller_key: "ControllerKey", state: int):
        super().__init__(controller_key, state)

        self.key_image: InputImage | None = None
        # Either provider: a .gif key media builds a KeyGIF, everything else an
        # InputVideo. Both expose the get_raw_image/close surface the key paint
        # path and close_resources drive them through.
        self.key_video: "InputVideo | KeyGIF | None" = None
        # The ActionCore that set the current key_image/key_video via
        # set_media(), or None when the media is page/user-owned. Every other
        # media writer resets it to None; set_media() re-stamps it after the
        # write. ControllerKey.load_from_input_dict uses it to carry
        # action-owned media across the create_n_states wipe.
        self.media_owner_action = None

    def close_resources(self) -> None:
        if self.key_image is not None:
            self.key_image.close()
            self.key_image = None
        if self.key_video is not None:
            self.key_video.close()
            self.key_video = None
        self.media_owner_action = None

    def set_image(self, key_image: "InputImage | None", update: bool = True) -> None:
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
        self.media_owner_action = None

        if update:
            self.update()

    def set_video(self, key_video: "InputVideo | KeyGIF") -> None:
        if self.key_video is not None:
            # Design doc bug 18: the previous video was never closed before
            # being overwritten (InputVideo.close() is now real).
            self.key_video.close()
        self.key_video = key_video
        if self.key_image is not None:
            self.key_image.close()
        self.key_image = None
        self.media_owner_action = None

    def clear(self):
        if self.key_video is not None:
            # Design doc bug 18: clear() dropped key_video without closing
            # it (InputVideo.close() is now real).
            self.key_video.close()
        self.key_image = None
        self.key_video = None
        self.media_owner_action = None
        self.label_manager.clear_labels()
        self.layout_manager.clear()
        self.background_manager.set_page_color(None)