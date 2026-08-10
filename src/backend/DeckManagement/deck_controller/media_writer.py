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

---

The media writer: one MediaPlayerThread per deck, and the units it
executes. That thread is the sole writer to its device -- every key image,
touchscreen image, brightness change and blank reaches the hardware from
inside its loop -- so the ordering between them is decided here and nowhere
else. Paints carry the page and generation they were rendered for and are
judged stale at the present boundary; control messages (brightness, clear,
clear-and-close, stashed-input release) have no such page affinity, drain
first on every wake, and always execute, FIFO.

Also here: the native JPEG encoders every paint funnels through, and the
FIFO transport lock that keeps a write burst from starving the device's HID
read poll.

A leaf of the deck_controller package: it imports nothing from its
siblings. The controller a writer serves is duck-typed attribute access,
which is why the type-only imports below are the whole of its knowledge
about it.
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
    from src.backend.DeckManagement.DeckController import (
        ControllerDial,
        ControllerKey,
        ControllerTouchScreen,
        DeckController,
    )


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
