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

The deck controller: one DeckController per physical deck, plus the table
that says which controller-input class drives which identifier type.

A controller owns the device handle and everything hung off it -- the
inputs, the background, the screensaver, the two native image caches -- and
it owns the lifecycle: open the deck with its transport lock already made
FIFO, build the inputs, start the media writer, load a page, and tear all of
that down again in a fixed order at close(). It writes nothing to the device
itself. Every paint is composed by an input and enqueued for the media
writer, which is the sole writer.

Page loading is the busiest seam. load_page() serializes switches under
_load_page_lock, then bumps _page_load_generation and stamps every input
with the new generation inside one hold of _page_gen_lock, so a paint
queued from any other thread carries the generation it was rendered for and
is judged against the present one at the write boundary. The writer reads
that same pair, and the screensaver bumps it too -- it swaps this object's
inputs and background out from under it and puts them back on hide().

Top of the deck_controller package: it imports the media writer, the
background media group and the inputs, and none of them imports it back.
"""
import gc
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread

from PIL import Image
from StreamDeck.Devices import StreamDeck
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus
from StreamDeck.ImageHelpers import PILHelper
from loguru import logger as log

from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier
from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck
from src.backend.DeckManagement.Subclasses.ScreenSaver import ScreenSaver
from src.backend.DeckManagement.Subclasses.encoded_image_cache import EncodedImageCache
from src.backend.DeckManagement.Subclasses.native_tile_cache import NativeTileCache, native_tile_cache_max_bytes
from src.backend.DeckManagement.deck_controller.background_media import Background
from src.backend.DeckManagement.deck_controller.inputs import ControllerDial, ControllerKey, ControllerTouchScreen
from src.backend.DeckManagement.deck_controller.media_writer import (
    ClearAndCloseMsg,
    ClearMsg,
    MediaPlayerThread,
    SetBrightnessMsg,
    _install_fair_transport_lock,
)
from src.backend.PageManagement.Page import Page
from src.backend.mem_telemetry import page_switches
from src.backend import control_plane, startup_queue, ui_port
from src.api import notify_active_page_changed
from src.Signals import Signals

import globals as gl

from typing import TYPE_CHECKING, Any, overload
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.backend.DeckManagement.deck_controller.background_media import BackgroundVideo
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput


# Every input identifier class paired with the controller class that drives it.
# The values are the classes themselves, imported above from
# deck_controller/inputs.py; init_inputs below and Page.load_action_objects both
# read the table at call time. The value type names the three concretes, not
# their ControllerInput base: only they take the (controller, identifier)
# constructor both call sites use.
CONTROLLER_CLASSES: dict[type[InputIdentifier], type[ControllerKey | ControllerDial | ControllerTouchScreen]] = {
    Input.Key: ControllerKey,
    Input.Dial: ControllerDial,
    Input.Touchscreen: ControllerTouchScreen,
}

# An input type missing here fails at import instead of raising a KeyError deep
# inside DeckController.__init__, where it reads as a silently skipped device.
assert set(CONTROLLER_CLASSES) == set(Input.All)


class DeckController:
    # Bound on close() step 6's wait for plugin teardown hooks;
    # class-level so the harness can tighten it.
    TEARDOWN_JOIN_TIMEOUT_S = 10.0

    def __init__(self, deck_manager: "DeckManager", deck: StreamDeck.StreamDeck):
        self.deck_manager: "DeckManager" = deck_manager

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
        # thread -- see _install_fair_transport_lock in
        # deck_controller/media_writer.py for why the ordering of these two
        # lines is load-bearing.
        _install_fair_transport_lock(deck)
        # Resume-from-suspend handle reopen is the library's only mode now
        # (plan §9.1, decided 2026-07-04) -- always on. Called on the raw
        # handle (`self.deck is deck` here): the wrapper's open() takes no
        # arguments.
        deck.open(True)

        rotation = gl.settings_manager.deck_view(self.get_deck_settings()).get("rotation")
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

        self.media_player = MediaPlayerThread(deck_controller=self)
        self.media_player.start()

        # Everything below can still fail, and holds threads only this half-built object owns.
        try:
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

            deck_settings = gl.settings_manager.deck_view(self.get_deck_settings())

            # None so the first set_brightness() below always writes to the device,
            # even when the stored value equals the skip-write guard's default.
            self.brightness = None
            brightness = deck_settings.get("brightness", "value")
            self.set_brightness(brightness)

            # If screen is locked start the screensaver - this happens when the deck gets reconnected during the screensaver
            if gl.screen_locked and gl.settings_manager.app().lock_on_lock_screen:
                self.allow_interaction = False
                # Apply the deck's own screensaver config first: nothing has
                # loaded a page on this branch, so without it the deck shows
                # the ScreenSaver class's bare state -- no media, wrong dim.
                self._apply_screensaver_config(deck_settings.section("screensaver"))
                self.screen_saver.show()
            else:
                # Which failures may cost the deck its registration, and which
                # may not. A TRANSPORT failure says the device is not usable,
                # and the connect path owns that case: its retry arm reopens
                # the deck and runs the whole init again -- which is how a deck
                # whose serial read flakes during a boot storm still comes up.
                # Anything else (a render, a plugin, a logic error) is about the
                # PAGE: register the deck, the next load_default_page retries it.
                try:
                    self.load_default_page()
                except StreamDeck.TransportError:
                    raise
                except Exception as e:
                    log.error(f"Deck {self.serial_number()} registered without its boot page applied: {e}")
        except Exception:  # release them, then let it leave unchanged -- the connect path judges the failure
            self._teardown_failed_init()
            raise

    def _teardown_failed_init(self) -> None:
        """Releases what the guarded tail of __init__ started, so a deck that
        fails to initialize leaves no writer, ticker or pool behind. Not close():
        that assumes a REGISTERED controller (plugin hooks, UI detach, page
        deregistration), and this object was never handed to anyone. Nothing here
        writes to the device; the handle is released only once the writer is
        confirmed stopped, because a writer wedged mid-frame holds the device
        lock and waiting on it is the hang this exists to avoid. Raises nothing."""
        self.keep_actions_ticking = False
        if getattr(self, "tick_thread", None) is not None and self.tick_thread.is_alive():
            self._tick_stop_event.set()  # exists by the same statement pair as the thread
            self.tick_thread.join(2.0)
        self.media_player.stop(timeout=2.0)
        for pool in (getattr(self, "action_executor", None), getattr(self, "load_executor", None)):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        if not self.media_player.running:  # else the handle stays open, as it does today
            try:
                self.deck.stop_read_thread()  # its resume loop would reopen what close() releases
                self.deck.close()
            except Exception:
                log.opt(exception=True).warning("Failed to release the deck handle after a failed init")

    def init_inputs(self):
        # Build-then-swap: the media writer reads
        # self.inputs concurrently; filling a live dict in place gives it an
        # empty/partial view (the screensaver-entry KeyError window). Build
        # complete, then publish with one GIL-atomic assignment.
        new_inputs = {}
        for i in Input.All:
            new_inputs[i] = []
            input_class = CONTROLLER_CLASSES[i]

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

        queue = startup_queue.get()

        # A page change parked by the CLI for this serial. Claiming it removes
        # it: the request is one-shot (src/backend/startup_queue.py).
        api_page_path = queue.claim_page_request(self.serial_number())
        if api_page_path is not None:
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

        # Handle state change requests: peeked now, resolved at the tail, so a
        # failure in between leaves it parked (src/backend/startup_queue.py).
        # The rules the request is judged by are the control plane's, shared
        # with every other transport that can ask for a state change; only an
        # unexpected exception (a load that raises) escapes, which is what
        # leaves the request parked for the next load to retry.
        state_request = queue.peek_state_request(self.serial_number())
        if state_request is not None:
            result = control_plane.get().change_state_on(
                self,
                state_request["page_name"],
                state_request["coords"],
                state_request["state"],
            )
            if result.ok:
                log.info(result.message)
            else:
                log.error(f"State change failed on device {self.serial_number()}: {result.message}")

            # Remove the request after processing
            queue.resolve_state_request(self.serial_number())

    @log.catch
    def load_background(self, page: Page, update: bool = True, gen=None):
        deck_background_settings = gl.settings_manager.deck_view(self.get_deck_settings()).section("background")
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

        deck_brightness = gl.settings_manager.deck_view(self.get_deck_settings()).section("brightness")
        page_brightness = page.dict.get("settings",{}).get("brightness", {})

        if page_brightness.get("overwrite", False):
            value = page_brightness.get("value", 75)
        else:
            value = deck_brightness["value"]

        log.info(value)

        self.set_brightness(value)

    @log.catch
    def load_screensaver(self, page: Page):
        deck_screensaver_settings = gl.settings_manager.deck_view(self.get_deck_settings()).section("screensaver")
        page_screensaver_settings = page.dict.get("settings", {}).get("screensaver", {})

        log.info(f"Loading screensaver in thread: {threading.get_ident()}")
        if deck_screensaver_settings.get("enable", False) and not page_screensaver_settings.get("overwrite", False):
            config = deck_screensaver_settings
        elif page_screensaver_settings.get("overwrite", False) and page_screensaver_settings.get("enable", False):
            config = page_screensaver_settings
        else:
            config = {}

        self._apply_screensaver_config(config)

    def _apply_screensaver_config(self, config: dict) -> None:
        """Push one screensaver config onto the ScreenSaver. The deck arm
        arrives with DECK_DEFAULTS already filled in; the literals below are
        the PAGE arm's and the nothing-configured arm's -- a page that
        overwrites the screensaver is describing it itself."""
        self.screen_saver.set_media_path(config.get("media-path"))
        self.screen_saver.set_enable(config.get("enable", False))
        self.screen_saver.set_time(config.get("time-delay", 5))
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

    # `page` is Optional because None means "clear the deck" -- the branch a
    # few lines down. The page store also answers None for a page it could not
    # build, and every caller of that pair has always handed the answer
    # straight here.
    @log.catch
    def load_page(self, page: Page | None, load_brightness: bool = True, load_screensaver: bool = True, load_background: bool = True, load_inputs: bool = True, allow_reload: bool = True):
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
            # `background.video.page is active_page` (see
            # MediaPlayerThread._run_one_tick in
            # deck_controller/media_writer.py), so changing active_page
            # mid-screensaver freezes the
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

            old_path = self.active_page.flush() if self.active_page is not None else None

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
                # controller_input.config_gen directly (ControllerKey.update
                # and ControllerTouchScreen.update, both in
                # deck_controller/inputs.py -- the ControllerInput base
                # declares update() and does nothing); any window between the
                # bump and the stamp lets such a paint carry the previous
                # generation and be dropped at the present boundary as stale
                # -- blanking the newly loaded page's own keys. Stale
                # cross-page content is still caught by the separate
                # page-identity check. This must stay the ONLY stamp on the
                # load path (see _load_input_if_current).
                for input_type in self.inputs:
                    for controller_input in self.inputs[input_type]:
                        controller_input.config_gen = gen

            # Installed: active_page protects it now, so the fetch can stop
            # doing so. The screensaver branch skips this deliberately -- its
            # page reaches no deck yet; the reservation carries it to hide().
            if (manager := gl.page_manager) is not None:
                manager.pins.release_fetch(self)

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

    def tick_actions(self) -> None:
        # Event-based wait (mirrors MediaPlayerThread's _wake_event in
        # deck_controller/media_writer.py): close() sets _tick_stop_event
        # alongside keep_actions_ticking=False so its bounded join actually
        # returns promptly instead of waiting out whatever fraction of
        # TICK_DELAY this loop happened to be sleeping.
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
            try:
                if not self.screen_saver.showing:
                    for t in self.inputs:
                        for i in self.inputs[t]:
                            i.get_active_state().own_actions_tick_threaded()
            finally:
                # Reset the SAME page the False-call marked, in `finally`
                # because a raising body would otherwise pin it forever.
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
        """Pins (False) or releases (True) the page bracketed work must
        outlive, and returns it -- PagePins.bracket has the pass-back rule."""
        page = self.active_page if page is None else page
        return page if (pm := gl.page_manager) is None else pm.pins.bracket(page, ready_to_clear)
    
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
            # Same-value echo: persisting and reloading the page would be a
            # pure no-op with a visible flicker. Reached whenever a caller
            # re-applies the factor it already holds -- a repeated drag step
            # landing on the same rounded value, a plugin, a settings pane.
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
        leaving the deck blank (see MediaPlayerThread._exec_clear in
        deck_controller/media_writer.py). Leave it False when a blank deck is
        the intended end state."""
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

        # Step 8: deregistration, which also WRITES: it flushes every page
        # still cached for this deck before dropping the entries that hold
        # them. The dead controller's active_page was otherwise permanently
        # unevictable (design doc bug 1), distorting every other deck's budget.
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
