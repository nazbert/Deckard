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
import gc
import math
import os
import threading
import time
# Import Python modules
from concurrent.futures import ThreadPoolExecutor, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from copy import copy
from threading import Thread, Timer

import psutil
from PIL import Image, ImageDraw, ImageEnhance
from StreamDeck.Devices import StreamDeck
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from StreamDeck.Devices.StreamDeckPlus import StreamDeckPlus
from loguru import logger as log

# Import own modules
from src.backend.DeckManagement.BetterDeck import BetterDeck
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
from src.backend.DeckManagement.Subclasses import cache_budget
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache
from src.backend.DeckManagement.Subclasses.encoded_image_cache import EncodedImageCache
from src.backend.DeckManagement.Subclasses.native_tile_cache import NativeTileCache, native_tile_cache_max_bytes
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof

# The GIF pipeline lives in deck_controller/gif_pipeline.py. Re-imported here
# so this module's own call sites -- and every importer that has always
# reached these names through it -- keep resolving them exactly as before.
from src.backend.DeckManagement.deck_controller.gif_pipeline import (  # noqa: F401
    BOUNDED_TILE_VARIANT,
    GIF_BG_BUDGET_MB,
    GIF_KEY_BUDGET_MB,
    GifBackground,
    GifBudgetExceeded,
    GifTimeline,
    KeyGIF,
    _STRIP_GEOMETRY_MISSING,
    contained_size,
    cumulative_gif_delays,
    decode_gif_frames,
    frame_has_alpha,
    gif_frame_walk,
    gif_header_geometry,
    gif_key_budget_bytes,
    normalize_gif_delay,
    probe_gif_timeline,
    tile_video_size,
)

# The label/layout/background composition engine lives in
# deck_controller/label_engine.py. Re-imported here so this module's own
# call sites -- and every importer that has always reached these names
# through it -- keep resolving them exactly as before.
from src.backend.DeckManagement.deck_controller.label_engine import (  # noqa: F401
    BackgroundManager,
    LabelManager,
    LayoutManager,
    _BitmapRecorder,
    _RecordingTooLarge,
    _label_measure_draw,
)

# The media thread -- the sole writer to a deck -- and the tasks and control
# messages it executes live in deck_controller/media_writer.py. Re-imported
# here so this module's own call sites -- and every importer that has always
# reached these names through it -- keep resolving them exactly as before.
from src.backend.DeckManagement.deck_controller.media_writer import (  # noqa: F401
    KEY_ENCODE_QUALITY,
    ClearAndCloseMsg,
    ClearMsg,
    MediaPlayerSetImageTask,
    MediaPlayerSetTouchscreenImageTask,
    MediaPlayerTask,
    MediaPlayerThread,
    ReleaseStashedInputsMsg,
    SetBrightnessMsg,
    _env_float,
    _install_fair_transport_lock,
    encode_native_key,
    encode_native_touchscreen,
)
from src.backend.mem_telemetry import page_switches
from src.backend import timer_wheel
from src.backend.PageManagement.Page import ActionOutdated, Page, NoActionHolderFound
from src.api import notify_active_page_changed
from src.backend import ui_port

process = psutil.Process()

# Import signals
from src.Signals import Signals

# Import typing
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Generic, TypeVar, overload

from src.backend.PluginManager.ActionCore import ActionCore
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager

# Import globals
import globals as gl


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


# Every input identifier class paired with the controller class that drives it.
# Lives below those classes because the values are the class objects themselves;
# init_inputs and Page.load_action_objects both read it at call time. The value
# type names the three concretes, not their ControllerInput base: only they take
# the (controller, identifier) constructor both call sites use.
CONTROLLER_CLASSES: dict[type[InputIdentifier], type[ControllerKey | ControllerDial | ControllerTouchScreen]] = {
    Input.Key: ControllerKey,
    Input.Dial: ControllerDial,
    Input.Touchscreen: ControllerTouchScreen,
}

# An input type missing here fails at import instead of raising a KeyError deep
# inside DeckController.__init__, where it reads as a silently skipped device.
assert set(CONTROLLER_CLASSES) == set(Input.All)
