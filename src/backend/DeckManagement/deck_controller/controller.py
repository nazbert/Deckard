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

One DeckController per physical deck, plus the table that maps an identifier
type to the controller-input class that drives it.

A controller owns the device handle and everything hung off it, and it owns
the lifecycle. It opens the deck with its transport lock already FIFO,
builds the inputs, starts the media writer, loads a page, and tears all of
that down in a fixed order at close(). It writes nothing to the device
itself. An input composes every paint and enqueues it for the media writer,
which is the sole writer.

Page loading is the busiest seam. load_page() serializes switches under
_load_page_lock, then bumps _page_load_generation and stamps every input
with the new generation inside one hold of _page_gen_lock. A paint queued
from another thread then carries the generation it was rendered for, and the
write boundary judges it against the present one. The writer reads that same
pair, and the screensaver bumps it too, because it swaps this object's
inputs and background out and puts them back on hide().

This module imports the media writer, the background media group and the
inputs, and none of them imports it back.
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


# Every input identifier class paired with the controller class that drives
# it. init_inputs below and Page.load_action_objects both read the table at
# call time. The value type names the three concrete classes, not their
# ControllerInput base, because only they take the (controller, identifier)
# constructor that both call sites use.
CONTROLLER_CLASSES: dict[type[InputIdentifier], type[ControllerKey | ControllerDial | ControllerTouchScreen]] = {
    Input.Key: ControllerKey,
    Input.Dial: ControllerDial,
    Input.Touchscreen: ControllerTouchScreen,
}

# An input type missing here fails at import, instead of a KeyError deep
# inside DeckController.__init__ that reads as a silently skipped device.
assert set(CONTROLLER_CLASSES) == set(Input.All)


class DeckController:
    # Bound on the close() wait for plugin teardown hooks. It sits on the
    # class so the harness can tighten it.
    TEARDOWN_JOIN_TIMEOUT_S = 10.0

    def __init__(self, deck_manager: "DeckManager", deck: StreamDeck.StreamDeck):
        self.deck_manager: "DeckManager" = deck_manager

        # Per-instance memo for stable deck properties. An lru_cache on an
        # instance method pins every self on the class and never evicts.
        self._serial_number: str | None = None
        self._key_image_size: tuple[int, int] | None = None
        self._touchscreen_image_size: tuple[int, int] | None = None
        self._native_key_format_sig: tuple | None = None

        # Store the raw handle as self.deck so get_alive() returns True inside
        # get_deck_settings. The raw handle answers is_open() the same way the
        # BetterDeck wrapper does, and the wrapper replaces it a few lines
        # below.
        self.deck: BetterDeck = deck
        # Order the transport mutex FIFO before open() starts the reader
        # thread. The order of these two lines matters; see
        # _install_fair_transport_lock in deck_controller/media_writer.py.
        _install_fair_transport_lock(deck)
        # Resume-from-suspend handle reopen is the library's only mode, and it
        # is always on. Call it on the raw handle, because the wrapper's
        # open() takes no arguments.
        deck.open(True)

        rotation = gl.settings_manager.deck_view(self.get_deck_settings()).get("rotation")
        self.deck = BetterDeck(deck, rotation)

        try:
            # Clear the deck through the direct body, not the queue-routed
            # clear(). media_player does not exist yet, and this is a liveness
            # probe, so its exception must abort construction here instead of
            # getting lost in an async queue.
            self._clear_direct()
        except Exception as e:
            log.error(f"Failed to clear deck, maybe it's already connected to another instance? Skipping... Error: {e}")
            # Release the handle and raise, so the caller does not register a
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

        # Per-deck saturation boost, a PIL ImageEnhance.Color factor over the
        # UI range 1.0 to 1.5. It is read once at boot and refreshed by
        # set_display_saturation(), so every per-frame call site does one
        # attribute read instead of a settings lookup, and skips all
        # enhancement work with one float comparison at the default 1.0.
        self.display_saturation: float = self._read_display_saturation()

        # {identifier: True} while the main window is hidden. This is a dirty
        # marker, not a stashed PIL image. The device composite runs every
        # tick whatever the window does, so a retained copy holds a big object
        # alive for nothing. On map, KeyGrid.load_from_changes and
        # ScreenBar.load_from_changes recomposite the current frame for each
        # dirty identifier and push it through the same set-image path a live
        # update uses.
        self.ui_image_changes_while_hidden: dict = {}

        # close() sets this once and never clears it. It gates the re-entrant
        # producer paths, ScreenSaver.show, hide, on_key_change and load_page,
        # which otherwise resurrect a controller during teardown, and it makes
        # close() idempotent against a second call. The transition runs under
        # _close_lock, because the unplug thread and the app-quit teardown
        # race, and an unlocked check-then-set lets both run the sweep at
        # once, with duplicate plugin hooks and a double device close.
        self._closing: bool = False
        self._close_lock = threading.Lock()

        # Timestamp of the last post-load GC (see maybe_collect_garbage).
        self._last_gc_time: float = 0.0

        self.active_page: Page | None = None

        # Bumped on every load_page, so an overlapping load can tell whether
        # its queued paints are still current. See _page_is_current.
        self._page_load_generation: int = 0
        self._page_gen_lock = threading.Lock()
        # Serializes load_page's switch body so racing switches cannot
        # interleave. An older switch can cancel the newer one's background
        # future or strand its queued work. It is an RLock, because a
        # ChangePage handler nests a load_page.
        self._load_page_lock = threading.RLock()
        # Page recorded by load_page's screensaver guard, consumed by
        # ScreenSaver.hide() via take_pending_screensaver_page().
        self._screensaver_pending_page: "Page | None" = None
        # Serializes background loads on the pool. A superseded load must not
        # overwrite a newer page's background.
        self._background_load_lock = threading.Lock()
        self._bg_future = None

        # Native encoded key image caches. Build them before the inputs and
        # the background, because the paint path dereferences both directly
        # and a background change clears them, so neither may lag behind
        # anything that can paint.
        #
        # encode_memo keys on (composite hash, rotation), so a repeated frame
        # from a looping background video skips the conversion and the JPEG
        # encode.
        self.encode_memo = EncodedImageCache(max_bytes=32 * 1024 * 1024)
        # native_tile_cache holds the same natives keyed by frame identity for
        # the passthrough path. A bare key over a video background then skips
        # the tobytes and the hash, so a warmed loop costs one dict lookup per
        # key.
        self.native_tile_cache = NativeTileCache(max_bytes=native_tile_cache_max_bytes())
        # Enrol both in the process-wide image-cache budget. Each cache's own
        # cap bounds it, and the budget bounds the sum across decks. Without
        # it, total image-cache RAM scales with the deck count and a cold
        # deck's full memo never yields a byte to a hot one. The registry is
        # weak, so close() needs no matching unregister. This wraps register()
        # a second time, because DeckManager reports anything raised out of
        # __init__ as a failed deck and skips the whole device.
        try:
            self._register_image_caches()
        except Exception as e:
            log.warning(f"Could not register the image caches with the budget: {e}")

        # Heterogeneous registry. The key fixes the element type of the value
        # list, so Input.Key gives ControllerKey, Input.Dial gives
        # ControllerDial and Input.Touchscreen gives ControllerTouchScreen.
        # The type system cannot express that for a plain dict, so list[Any]
        # states it, instead of naming one element type and misdeclaring the
        # other two. get_inputs() is the annotated base-typed view.
        self.inputs: dict[type[InputIdentifier], list[Any]] = {}
        for i in Input.All:
            self.inputs[i] = []
        self.init_inputs()

        self.background = Background(self)

        self.deck.set_key_callback(self.key_event_callback)
        self.deck.set_dial_callback(self.dial_event_callback)
        self.deck.set_touchscreen_callback(self.touchscreen_event_callback)

        # Write-error and resume-repaint state. Only the media thread touches
        # it, through _on_write_result and _run_pending_repaint, so it needs
        # no lock. It must exist before the media thread starts, because the
        # first iteration dereferences _full_repaint_pending. The loop guards
        # that, but every tick fails until this assignment lands, so keep the
        # order.
        self._had_write_failure: bool = False
        self._full_repaint_pending: bool = False
        self._last_full_repaint_ts: float = 0.0

        self.media_player = MediaPlayerThread(deck_controller=self)
        self.media_player.start()

        # Everything below can still fail, and holds threads only this half-built object owns.
        try:
            # Register the sole expected device writer for the
            # owner-assertion tooling in BetterDeck.py. It does nothing unless
            # DECKARD_ASSERT_DEVICE_OWNER is set.
            self.deck.set_expected_writer(self.media_player)

            # Bounded thread pool for the action callbacks, sized so every
            # input runs its on_tick concurrently.
            total_inputs = sum(len(inputs) for inputs in self.inputs.values())
            # close() sets this to None, so every reader either
            # getattr-defaults or None-checks before it submits.
            self.action_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
                max_workers=max(8, total_inputs + 4),
                thread_name_prefix="action_cb",
            )

            # Persistent per-deck loader pool for load_all_inputs, sized so
            # every input loads concurrently. load_all_inputs runs on the
            # media-player thread, so a small fixed pool serializes an XL's 32
            # inputs several deep there and its deadline waits block the sole
            # writer. load_all_inputs replaces this pool wholesale when the
            # deadline expires with stuck tasks.
            self.load_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
                max_workers=max(8, total_inputs),
                thread_name_prefix=f"load_{self.serial_number()}",
            )

            self.keep_actions_ticking = True
            self.TICK_DELAY = 1
            # Lets close() interrupt the tick_actions sleep at once, instead
            # of a wait of up to one full TICK_DELAY before the loop reads
            # keep_actions_ticking. close() needs a prompt, bounded join. See
            # tick_actions.
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

            # Start the screensaver when the screen is locked. This happens
            # when the deck reconnects during the screensaver.
            if gl.screen_locked and gl.settings_manager.app().lock_on_lock_screen:
                self.allow_interaction = False
                # Apply the deck's own screensaver config first. No page
                # loads on this branch, so without it the deck shows the bare
                # ScreenSaver state, with no media and the wrong dim level.
                self._apply_screensaver_config(deck_settings.section("screensaver"))
                self.screen_saver.show()
            else:
                # A transport failure says the device is not usable, and the
                # connect path owns that case. Its retry arm reopens the deck
                # and runs the whole init again, so a deck whose serial read
                # flakes during a boot storm still comes up. Any other
                # failure, from a render, a plugin or a logic error, is about
                # the page, so register the deck and let the next
                # load_default_page retry it.
                try:
                    self.load_default_page()
                except StreamDeck.TransportError:
                    raise
                except Exception as e:
                    log.error(f"Deck {self.serial_number()} registered without its boot page applied: {e}")
        except Exception:  # release them and re-raise; the connect path judges the failure
            self._teardown_failed_init()
            raise

    def _teardown_failed_init(self) -> None:
        """Release what the guarded tail of __init__ started, so a deck that
        fails to initialize leaves no writer, ticker or pool behind. This is
        not close(), which assumes a registered controller with plugin hooks,
        a UI attachment and page registration; nobody ever received this
        object. Nothing here writes to the device. The handle is released only
        after the writer stops, because a writer wedged mid-frame holds the
        device lock and a wait on it is the hang this avoids. Raises
        nothing."""
        self.keep_actions_ticking = False
        if getattr(self, "tick_thread", None) is not None and self.tick_thread.is_alive():
            self._tick_stop_event.set()  # created by the same statement pair as the thread
            self.tick_thread.join(2.0)
        self.media_player.stop(timeout=2.0)
        for pool in (getattr(self, "action_executor", None), getattr(self, "load_executor", None)):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        if not self.media_player.running:  # otherwise the handle stays open
            try:
                self.deck.stop_read_thread()  # its resume loop reopens what close() releases
                self.deck.close()
            except Exception:
                log.opt(exception=True).warning("Failed to release the deck handle after a failed init")

    def init_inputs(self):
        # Build then swap. The media writer reads self.inputs concurrently,
        # and a fill in place gives it an empty or partial view, which raises
        # a KeyError at screensaver entry. Build the whole dict, then publish
        # it with one GIL-atomic assignment.
        new_inputs = {}
        for i in Input.All:
            new_inputs[i] = []
            input_class = CONTROLLER_CLASSES[i]

            for k in input_class.Available_Identifiers(self.deck):
                controller_input = input_class(self, Input.FromTypeIdentifier(i.input_type, k))
                # Stamp with the current generation so a paint from a freshly
                # built input, such as the screensaver's, is not dropped as
                # stale.
                controller_input.config_gen = self._page_load_generation
                new_inputs[i].append(controller_input)
        self.inputs = new_inputs

    def get_inputs(self, identifier: InputIdentifier) -> list["ControllerInput"]:
        input_type = type(identifier)
        if input_type not in self.inputs:
            raise ValueError(f"Unknown input type: {input_type}")
        return self.inputs[input_type]

    # The key-dependent value type of the registry is expressible here, where
    # a concrete identifier class is in hand, so a caller that asks for a dial
    # gets a ControllerDial back instead of the base class.
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
            # UI-only mirror. The in-app KeyGrid is not the video device, so
            # a push of the current composite is a widget update and never a
            # device write. The video loop skips the per-frame render for an
            # opaque key at alpha 255, so the app never repaints it after a
            # transition and the previews diverge from the deck. This bypasses
            # the device-oriented dedup of update(), because the device and
            # the UI can differ and only a re-push reconciles them.
            for i in self.inputs[Input.Key]:
                try:
                    # First device paint for an opaque key. The per-frame
                    # video loop never repaints a key whose composed color is
                    # fully opaque, because its tile hides the video and
                    # nothing changes between frames. Without this the device
                    # keeps the previous page's content there until a
                    # keypress. An opaque tile hides the video, so this write
                    # cannot disturb it. update() paints the device and the
                    # app preview, so an opaque key needs no second push
                    # below.
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
        # This runs on the media thread. Skip at once when superseded, then
        # wait out the background decode under a bound, so the keys composite
        # over the new background. The wait blocks the sole writer, and it
        # runs in slices, so a page switch that supersedes this one mid-decode
        # abandons it at the next slice instead of sitting out the rest of a
        # 10s decode for a page the deck already left.
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
        # The inputs are loaded and painted. Media tasks are FIFO, so
        # load_all_inputs finished before this task ran, and the sidebar can
        # render the new page's state objects.
        if self._page_is_current(gen):
            ui_port.get().on_page_changed(self)

    def animations_gated(self) -> bool:
        """Whether this deck's media loop skips its animation section this
        tick because nobody is looking.

        Two terms decide it. gl.presence_monitor is the process-wide presence
        signal, and it reports False forever in the default pause mode, so
        this returns False for anyone who did not opt in. The second term is
        screen_saver.showing. While the screensaver owns the deck its
        animation is the intended visible content, and the physical deck is
        visible even when the monitor is locked, so the gate never applies to
        it. show() already released the underlying page's media, so a showing
        screensaver has nothing else to decode.

        The writer reads this once per tick on its critical path. Both terms
        are plain attribute reads and neither takes a lock."""
        pm = getattr(gl, "presence_monitor", None)
        return pm is not None and pm.is_quiescent() and not self.screen_saver.showing

    def _reset_dedup_hashes(self) -> None:
        """Set _last_img_hash and _last_enqueued_hash to None on every
        current key and on the touchscreen. Clear and full-repaint scheduling
        share it. Without it, a repaint of visually identical content matches
        the stale cached hash and is wrongly skipped."""
        for key in self.inputs.get(Input.Key, []):
            key._last_img_hash = None
            key._last_enqueued_hash = None
        for touchscreen in self.inputs.get(Input.Touchscreen, []):
            touchscreen._last_img_hash = None
            touchscreen._last_enqueued_hash = None

    def _schedule_full_repaint(self) -> None:
        """Arm a pending full repaint. The media loop fires it through
        _run_pending_repaint() when the 2s rate window allows. A rate limit
        defers it and never drops it, and every write failure re-arms it. A
        repaint attempted while the library's read thread still reopens the
        handle after a suspend fails wholesale, and on a fully static page no
        later write re-triggers it, so the pending flag makes the loop retry
        every 2s until the writes stick."""
        self._full_repaint_pending = True

    def _run_pending_repaint(self) -> bool:
        """Media-loop hook that fires an armed repaint at least 2s after the
        last one. It nulls all dedup hashes, then calls update_all_inputs().
        That is safe on the media thread, because it only enqueues through
        add_image_task and add_touchscreen_task, which on_media_player_tick
        already calls from this thread. Returns whether a repaint fired."""
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
        """Unified write-error handler, called by the image and touchscreen
        task run() paths and by _exec_set_brightness after every device write
        attempt. The error policy is attempt and swallow, and only a USB
        disconnect event removes a deck. Recovery is the remaining job. Every
        failure arms the pending repaint, because content written into that
        failure window can be lost on the device, and the loop's 2s cadence
        retries until a repaint lands cleanly. Only the media thread calls
        this, so it needs no lock."""
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
            # Dead or closing deck. Return the fallback without a memo, so a
            # deck that comes back is re-queried at its real size. Return a
            # size and not None. No caller None-checks, and they unpack two
            # ints or pass the result to Image.new, so None raises TypeError
            # on a media tick that races an unplug.
            #
            # (72, 72) matches the size-unavailable branch below. It is wrong
            # for an XL, whose keys are 96x96, but this path only makes
            # throwaway tiles for a deck nothing can write to, and the real
            # size returns as soon as the deck answers again.
            return (72, 72)
        size = self.deck.key_image_format()["size"]
        if size is None:
            size = (72, 72)
        else:
            size = max(size[0], 72), max(size[1], 72)
        self._key_image_size = size
        return size

    def native_key_format_sig(self) -> tuple:
        """Hashable signature of the deck's native key image format. It is
        part of every native tile cache key, so bytes encoded for one device
        format can never be served for another. It is memoized because the
        driver's format is fixed for the life of the device. The user-facing
        rotation is not part of it; that lives on BetterDeck and keys
        separately."""
        if self._native_key_format_sig is None:
            fmt = self.deck.key_image_format()
            self._native_key_format_sig = (
                tuple(fmt["size"]), fmt["format"], tuple(fmt["flip"]), fmt["rotation"],
            )
        return self._native_key_format_sig

    def clear_encoded_key_caches(self) -> None:
        """Drop every cached native key image, both the pixel-hash encode
        memo and the frame-identity native tiles. Callers reach here wherever
        the content those entries encoded is orphaned wholesale, at a
        background content change, a rotation change or teardown. The getattr
        guard lets a close() after a half-finished __init__ still sweep what
        exists."""
        for cache_name in ("encode_memo", "native_tile_cache"):
            cache = getattr(self, cache_name, None)
            if cache is not None:
                cache.clear()

    def _register_image_caches(self) -> None:
        """Enrol this deck's two native-image caches with the process-wide
        budget. The labels carry the serial, so a multi-deck rig's eviction
        and thrash logs are attributable. totals() sums per group, so the
        telemetry columns stay deck-agnostic."""
        serial = self.serial_number()
        cache_budget.register(self.encode_memo, label=f"encode_memo:{serial}")
        cache_budget.register(self.native_tile_cache, label=f"native_tiles:{serial}")

    def refresh_tile_cache_min_age(self, video: "BackgroundVideo" = None) -> None:
        """Retune how long the native tile cache shields its entries from
        global eviction, to the duration of the background video now playing,
        clamped to DEFAULT_MIN_AGE_S..MAX_MIN_AGE_S. It returns to the
        default when no video plays.

        A native tile entry is keyed per frame, so an entry is re-touched
        once per loop of the content, not once per media tick. Under a
        binding ceiling a flat 2 s min-age makes a playing video's whole
        frame set eligible for eviction exactly one loop before it is needed
        again, which reinstates the per-frame encode this cache removes. A
        closed video's frames become eligible again at once, which is
        correct, because they are stale.

        frames/source_fps is the loop period only once the tile cache is
        built. While it builds, get_next_tiles() advances the frame
        sequentially, one frame per media tick, so no frame is skipped and no
        seek is forced, and the media tick can run slower than the source
        fps. The real loop period is then unknown and strictly longer than
        frames/fps, so that number under-protects exactly the frame set the
        build is racing to fill. The clamp maximum goes in instead, and the
        first tick past completion calls back in here with the real value.
        Err long, not short. Over-protection costs one stale entry surviving
        a pass, and under-protection costs the re-encode."""
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
            # The same dead-deck contract, and the same fallback caveat, as
            # get_key_image_size. Callers unpack two ints and none None-checks.
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

        # A page change parked by the CLI for this serial. A claim removes it,
        # because the request is one-shot. See src/backend/startup_queue.py.
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

        # Handle a state change request. This peeks now and resolves at the
        # tail, so a failure in between leaves the request parked. The control
        # plane owns the rules, shared with every other transport that asks
        # for a state change. Only an unexpected exception escapes, and that
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
            # Set the flag first, with no repaint, so set_from_path renders
            # the tiles and the touchscreen slice at the correct geometry.
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
        arrives with DECK_DEFAULTS already filled in. The literals below
        belong to the page arm and the nothing-configured arm, because a page
        that overwrites the screensaver describes it itself."""
        self.screen_saver.set_media_path(config.get("media-path"))
        self.screen_saver.set_enable(config.get("enable", False))
        self.screen_saver.set_time(config.get("time-delay", 5))
        self.screen_saver.set_loop(config.get("loop", True))
        self.screen_saver.set_fps(config.get("fps", 30))
        self.screen_saver.set_brightness(config.get("brightness", 30))

    def _page_is_current(self, gen) -> bool:
        # gen is None for a caller outside the page-load path, which always
        # runs. A paint that load_page issued is stale once a newer load_page
        # bumped the generation.
        return gen is None or gen == self._page_load_generation

    # Deadline for load_all_inputs. An input load runs plugin callbacks that
    # can block forever, and none of them may wedge the media-player thread.
    LOAD_INPUTS_TIMEOUT = 10.0

    @log.catch
    def load_all_inputs(self, page: Page, update: bool = True, gen=None):
        if not self._page_is_current(gen):
            return
        start = time.time()
        # Use the persistent per-deck pool, not a throwaway
        # ThreadPoolExecutor per call. This runs on the media-player thread,
        # so a pool built and torn down on every page switch is churn on the
        # sole writer's path.
        executor = self.load_executor
        if executor is None:
            # close() sets the pools to None, so a load that started before
            # it has nothing left to submit onto. Return like the per-submit
            # RuntimeError guard below does. A .submit() on None raises
            # AttributeError, which that guard does not catch.
            return
        pending = []
        for t in self.inputs:
            for controller_input in self.inputs[t]:
                try:
                    future = executor.submit(self._load_input_if_current, controller_input, page, update, gen)
                except RuntimeError:
                    # The pool already shut down, because the deck is closing.
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
            # Do not wait. A stuck task may never return, so cancel what can
            # be cancelled and abandon the rest to this pool's leaked threads.
            old_executor.shutdown(wait=False, cancel_futures=True)
        log.info(f"Loading all inputs took {time.time() - start} seconds")

    def _load_input_if_current(self, controller_input: "ControllerInput", page: Page, update: bool = True, gen=None):
        # A slower in-flight page load must not paint the previous page's
        # images onto the current page's keys, so skip when a newer load
        # superseded this one. This does not stamp config_gen. load_page
        # stamps every input under _page_gen_lock, and a second stamp on this
        # pool can interleave with a newer load's stamp and regress an input
        # to an older generation.
        if not self._page_is_current(gen):
            return
        self.load_input(controller_input, page, update)

    def take_pending_screensaver_page(self) -> "Page | None":
        """Pop the page that load_page's screensaver guard recorded. None
        when no page change arrived while the screensaver was showing."""
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
        """Release every input's media, the key and dial images and videos,
        plus the background image and video. close() calls this from its
        resource sweep."""
        for t in self.inputs:
            for i in self.inputs[t]:
                i.close_resources()

        # Sweep the background under _background_load_lock. A load_background
        # inside set_from_path holds this lock while it attaches a fresh
        # BackgroundVideo. Without the lock the sweep can run between that
        # load's generation gate and its attach, and the fresh cv2 capture
        # stays on self.background.video and leaks until process exit. The
        # lock makes the sweep wait for an in-flight attach, and the _closing
        # re-check in apply_prebuilt suppresses that attach, so the sweep sees
        # and releases the final background object.
        with self._background_load_lock:
            if self.background.video is not None:
                self.background.video.close()
                self.background.video = None
            if self.background.image is not None:
                self.background.image.close()
                self.background.image = None

    # page is optional because None means clear the deck, which is the branch
    # a few lines down. The page store also answers None for a page it could
    # not build, and every caller hands that answer straight here.
    @log.catch
    def load_page(self, page: Page | None, load_brightness: bool = True, load_screensaver: bool = True, load_background: bool = True, load_inputs: bool = True, allow_reload: bool = True):
        if not self.get_alive(): return
        if self._closing:
            # A straggling caller raced close(), from a screensaver
            # follow-up, a plugin hook or a DBus request. Do not resurrect the
            # deck during teardown.
            return

        start = time.time()

        # Serialize the whole switch body. See _load_page_lock. The
        # plugin-facing tail, the ChangePage signal and DBus, stays outside,
        # so a slow handler cannot block other callers on this lock.
        with self._load_page_lock:
            if not allow_reload:
                if self.active_page is page:
                    return

            # A page change requested while the screensaver owns the deck must
            # not load or paint the new page now. That replaces the
            # screensaver on the device and leaks the page's icons onto the
            # deck and into the app previews. Record it as pending and return;
            # hide() loads it when the screensaver is dismissed.
            #
            # Do not touch active_page here. The media player gates the
            # screensaver's own background-video animation on
            # background.video.page is active_page, so a change to active_page
            # during the screensaver freezes the screensaver video until
            # active_page returns to the screensaver's page. Leaving
            # active_page alone keeps that gate open and the video playing.
            if self.screen_saver.showing:
                if page is not None:
                    self._screensaver_pending_page = page
                # A clear request, page=None, is dropped and not deferred.
                # The pending slot has no clear value, because None means no
                # pending, and a clear removes the showing screensaver from
                # the deck.
                return

            # A monotonic counter that the mem_telemetry idle and trim gate
            # reads. Bump it once this call is a real switch, and not the
            # no-op reload above.
            page_switches.bump()

            old_path = self.active_page.flush() if self.active_page is not None else None

            # Reset every key's pressed visual before the generation bump.
            # press_state lives on the reused ControllerKey and survives the
            # page swap, so a key that is still physically down composes every
            # new-page render through is_pressed() and shrink_image(). The
            # release's repaint can lose the enqueue race against a loader
            # render that read press_state just before the release landed,
            # which leaves the new page's key stuck pressed. The order carries
            # the fix. A render reads config_gen at the start of update() and
            # press_state later, at composite time, so a write of False before
            # the bump makes every render at the new generation compose
            # unpressed. The gesture bookkeeping stays untouched, because the
            # physical release must still dispatch its events.
            for controller_key in self.inputs.get(Input.Key, []):
                controller_key.press_state = False

            # Set active_page and bump the generation together. A concurrent
            # switch must never leave active_page on one page while the newest
            # generation belongs to another, or a stale paint matches both
            # checks and bleeds through.
            with self._page_gen_lock:
                self.active_page = page
                self._page_load_generation += 1
                gen = self._page_load_generation

                # Stamp every input with the new generation now, under the
                # same lock as the bump. Threads outside the load pool trigger
                # paints, from the action pool, the tick loop and
                # update_all_inputs, and they read
                # controller_input.config_gen directly. Any window between the
                # bump and the stamp lets such a paint carry the previous
                # generation and be dropped as stale at the write boundary,
                # which blanks the newly loaded page's own keys. The separate
                # page-identity check still catches stale cross-page content.
                # This must stay the only stamp on the load path. See
                # _load_input_if_current.
                for input_type in self.inputs:
                    for controller_input in self.inputs[input_type]:
                        controller_input.config_gen = gen

            # active_page protects the page now, so the fetch pin can
            # release. The screensaver branch skips this, because its page
            # reaches no deck yet and the reservation carries it to hide().
            if (manager := gl.page_manager) is not None:
                manager.pins.release_fetch(self)

            if page is None:
                # Clear deck
                self.clear()
                return

            log.info(f"Loading page {page.get_name()} on deck {self.deck.get_serial_number()}")

            # Stop the queued tasks. A newer switch that superseded this one
            # skips the stop.
            self.clear_media_player_tasks(gen)

            # Do not trigger the UI sync here. The new page's input states and
            # actions do not exist yet, so a sidebar rebuild renders the old
            # page's data and nothing corrects it later. It fires from the
            # load-completion side instead, at the end of the awaited input
            # load on the media thread, and after initialize_actions below.

            bg_future = None
            if load_background:
                # Decode the background off the media thread so it overlaps
                # the input loading. The update task below waits for it before
                # the keys composite.
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
                # No content reloads, but the generation bumped. Advance each
                # input's config_gen so its unchanged content is not dropped
                # as stale.
                for input_type in self.inputs:
                    for controller_input in self.inputs[input_type]:
                        controller_input.config_gen = gen

            # Load the page onto the deck, after the background decode.
            self.media_player.add_task(self._update_all_inputs_awaiting_background, bg_future, gen)

        # This must stay outside _load_page_lock. initialize_actions can block
        # on a run_on_main marshal and deadlock against a main-thread
        # load_page. Use page, not active_page. A newer switch can already
        # own active_page, and initializing a superseded page is harmless
        # because on_ready_called de-dupes.
        page.initialize_actions()

        # Second completion signal. action_objects exist now, so the sidebar's
        # ActionManager can render the new page's actions. The port coalesces
        # this with the media-thread trigger above, and each callback renders
        # the live state, so the later completion wins.
        ui_port.get().on_page_changed(self)

        # Notify the plugin actions. Use page.json_path, not active_page, for
        # the reason initialize_actions gives above. A racing switch or a
        # close() can swap or null active_page after the lock releases, and
        # the dereference raises AttributeError into @log.catch, which skips
        # this signal and the DBus notify for a switch that did happen.
        gl.signal_manager.trigger_signal(Signals.ChangePage, self, old_path, page.json_path)

        # Notify DBus API of the page change
        notify_active_page_changed(self.serial_number(), page.get_name())

        log.info(f"Loaded page {page.get_name()} on deck {self.deck.get_serial_number()}")
        self.maybe_collect_garbage()

    # Minimum seconds between post-load garbage collections, so rapid page
    # switching does not pay a full GC pause on every switch.
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
            # The value is unchanged, so skip the queued device write. The
            # device stalls noticeably on a brightness write during an
            # image-write burst.
            return
        # Route this through the media thread's control queue, so the device
        # write runs on the sole writer and not on the calling thread.
        # self.brightness holds the last commanded value, not a
        # hardware-confirmed one.
        self.brightness = value
        self.media_player.submit_control(SetBrightnessMsg(value))

    def set_rotation(self, value):
        self.deck.set_rotation(value)
        # Both native cache keys hold the rotation, so nothing stale can be
        # served. This clear is memory hygiene, because every entry encoded
        # for the old rotation is dead as soon as the rotation changes.
        self.clear_encoded_key_caches()

        # The UI rebuilds its key grid for the new geometry. This is
        # synchronous on the main loop, where the only caller runs, so the
        # load_page below repaints into the new grid and not the transposed
        # old one.
        ui_port.get().on_deck_layout_changed(self)

        if not self.get_alive(): return
        self.load_page(self.active_page)

    def tick_actions(self) -> None:
        # Event-based wait, as MediaPlayerThread._wake_event does. close()
        # sets _tick_stop_event beside keep_actions_ticking=False, so its
        # bounded join returns promptly instead of waiting out the rest of a
        # TICK_DELAY sleep.
        self._tick_stop_event.wait(self.TICK_DELAY)
        while self.keep_actions_ticking:
            start = time.time()
            ticked_page = self.mark_page_ready_to_clear(False)
            # A showing screensaver gets no per-input work from this loop.
            # Its imagery lives in background. The media thread advances a
            # video through update_tiles() and the per-key
            # on_media_player_tick(). Whichever thread called
            # apply_prebuilt() composites and encodes a still image, and the
            # media thread writes it once. init_inputs() builds the input set
            # that ScreenSaver.show() swaps in, with no action, no media and
            # no label on any of it, and ActionCore.get_is_present() refuses
            # plugin writes for the duration, so nothing here has state of its
            # own to advance.
            #
            # A repaint of every input once a second is not free and is not
            # this loop's job. The media loop's pending-full-repaint retry
            # recovers a lost device write, including the blank that a
            # late-executing Clear leaves at screensaver entry, and it is
            # armed at the sites that lose the write, _on_write_result and
            # _exec_clear. hide()'s load_page() restores the page on wake.
            try:
                if not self.screen_saver.showing:
                    for t in self.inputs:
                        for i in self.inputs[t]:
                            i.get_active_state().own_actions_tick_threaded()
            finally:
                # Reset the same page the False call marked. This runs in
                # finally, because a raising body pins the page forever.
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
        """Pin the page that bracketed work must outlive with False, release
        it with True, and return it. PagePins.bracket holds the pass-back
        rule."""
        page = self.active_page if page is None else page
        return page if (pm := gl.page_manager) is None else pm.pins.bracket(page, ready_to_clear)
    
    def get_deck_settings(self):
        if not self.get_alive():
            return {}
        return gl.settings_manager.get_deck_settings(self.deck.get_serial_number())

    # Display saturation.
    # DEFAULT_DISPLAY_SATURATION of 1.0 does nothing. Every application site
    # below compares against it before any ImageEnhance work and before any
    # cache filename, so the default leaves the on-disk and behavioral
    # footprint unchanged.
    DEFAULT_DISPLAY_SATURATION = 1.0
    # Valid range for the saturation factor, matching the UI scale
    # DeckGroup.Saturation from 1.0 to 1.5. A persisted value outside this
    # range is corruption or a hand-edit, so clamp it. The factor is also part
    # of the fitted-background and tile-cache keys, where a NaN or an inf
    # never matches and makes the cache re-enhance every composite.
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
        # float() accepts "nan" and "inf" without a raise. Reject a
        # non-finite value so it cannot reach an ImageEnhance factor or a
        # cache key.
        if not math.isfinite(value):
            return self.DEFAULT_DISPLAY_SATURATION
        return min(self.MAX_DISPLAY_SATURATION, max(self.MIN_DISPLAY_SATURATION, value))

    def get_display_saturation(self) -> float:
        return self.display_saturation

    def set_display_saturation(self, value: float) -> None:
        """Persist the saturation factor to deck settings, refresh the cached
        value, and reload the active page so static media re-enhances at once.
        A playing background or key video keeps showing its already-baked
        cache until the reload builds a fresh cache object under the new
        factor's cache filename, so video content upgrades on its first
        playthrough after that."""
        value = round(float(value), 2)
        if abs(value - self.display_saturation) <= 0.001:
            # Same-value echo. A persist and a page reload change nothing here
            # except a visible flicker. A caller reaches this whenever it
            # re-applies the factor it already holds, such as a drag step that
            # rounds to the same value, a plugin or a settings pane.
            return
        deck_settings = self.get_deck_settings()
        deck_settings.setdefault("display", {})["saturation"] = value
        gl.settings_manager.save_deck_settings(self.deck.get_serial_number(), deck_settings)

        self.display_saturation = value

        if self.active_page is not None:
            self.load_page(self.active_page, allow_reload=True)
    
    def get_own_deck_stack_child(self):
        """Deprecated in-process shim, kept for out-of-tree plugins.

        The engine caches and resolves no widget. The attached UI owns the
        binding from a controller to its child, by object identity at add_page
        time, never by a match of a re-read serial against a stack child's
        name. Returns None when no UI is attached.
        """
        return ui_port.get().query_deck_widget(self, "deck_stack_child")

    def _write_blank_frames(self) -> None:
        """Write blank key images, and a blank touchscreen, directly to the
        device. _clear_direct() and the media thread's Clear and
        ClearAndClose control messages share this body."""
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
        """Synchronous direct clear, only for the bootstrap liveness probe in
        __init__. media_player does not exist at that point, and the probe's
        exception must abort construction synchronously instead of getting
        lost in an async queue. This is not owner-assertion safe, because the
        assertion registers after the media thread starts, strictly after this
        runs. Do not call it anywhere else."""
        self._write_blank_frames()

    def clear(self, expects_repaint: bool = False) -> None:
        """Generation-agnostic async clear. It submits a seq-stamped ClearMsg
        to the media thread's control queue instead of a direct write. The seq
        stamp orders this against in-flight and future frame submissions. A
        task already queued with a lower submit_seq is wiped, and a task
        submitted after this call survives and paints afterward, even on the
        same tick. That keeps the caller's clear-then-paint order as
        blank-then-content on the device.

        Pass expects_repaint=True when this clear is the blank half of a
        blank-then-paint transition, so a Clear that executes after its own
        paints landed can be recovered from instead of leaving the deck blank.
        See MediaPlayerThread._exec_clear. Leave it False when a blank deck is
        the intended end state."""
        seq = self.media_player.next_submit_seq()
        self.media_player.submit_control(ClearMsg(seq=seq, expects_repaint=expects_repaint))

    def get_own_key_grid(self):
        """Deprecated in-process shim. See get_own_deck_stack_child."""
        return ui_port.get().query_deck_widget(self, "key_grid")
    
    def clear_media_player_tasks(self, gen=None):
        # Skip the clear when a newer page load superseded this one, so a late
        # clear cannot strand the newer load's freshly queued tasks. The lock
        # spans the check and the clear, so a generation bump cannot land
        # between them.
        with self._page_gen_lock:
            if gen is not None and gen != self._page_load_generation:
                return
            self.media_player.tasks.clear()
            # Take the writer's slot lock, so this cannot interleave with the
            # drain's read-then-null or with a producer's assignment.
            with self.media_player._slot_lock:
                self.media_player.image_tasks.clear()
                self.media_player.touchscreen_task = None

    def close(self, remove_media: bool, app_quit: bool = False) -> None:
        """One deterministic teardown sweep. Every unplug and replug through
        DeckManager.remove_controller, every fake-deck removal and every
        app-quit path funnels through here. delete() is a thin alias kept for
        existing callers.

        A second call from any thread does nothing, guarded by _closing.

        When app_quit is False this is expected to run off the main thread, so
        a wedged plugin teardown hook cannot freeze the UI.
        DeckManager.remove_controller dispatches it on a dedicated daemon
        thread, not the shared main_loop pool, which the quit path's
        shutdown_background_pool() would cancel mid-close. app_quit=True is
        the one case expected to run synchronously on main. It skips the
        action teardown, because there is no plugin hook to block on, and
        on_quit's 6s force-quit timer backs up everything else here.

        remove_media gates the resource sweep of the background media, the
        input media and the caches. The device, thread and registration
        teardown always runs.
        """
        # Locked compare-and-set. Two teardown callers, the USB unplug thread
        # and the app-quit main thread, both pass an unlocked check-then-set
        # and run the whole sweep at once, which duplicates the plugin
        # on_removed hooks and closes the device twice. Only the transition
        # takes the lock; the sweep stays unlocked, because it can block on
        # plugin hooks.
        with self._close_lock:
            if self._closing:
                return
            self._closing = True

        # Invalidate any in-flight page load now. A load_page that already
        # passed the _closing gate can otherwise attach a fresh
        # BackgroundVideo, with its cv2 capture, its registry reference and a
        # builder thread, after the resource sweep, and it leaks until process
        # exit. The generation bump aborts load_background, load_all_inputs
        # and the awaiting-update task at their generation checks, and the
        # cancel covers a decode that has not started.
        page_gen_lock = getattr(self, "_page_gen_lock", None)
        if page_gen_lock is not None:
            with page_gen_lock:
                self._page_load_generation += 1
        bg_future = getattr(self, "_bg_future", None)
        if bg_future is not None:
            bg_future.cancel()

        if not app_quit and threading.current_thread() is threading.main_thread():
            # A soft guard, not a hard failure. The test harness teardown()
            # helper calls close() from what that process calls the main
            # thread, where no GTK main loop runs, and that must keep working.
            # In the real app DeckManager.remove_controller always dispatches
            # onto a dedicated thread, so a warning here is a real signal.
            log.warning(
                f"DeckController.close() for "
                f"{getattr(self, '_serial_number', None) or '<unknown>'} called "
                "from the main thread with app_quit=False -- a wedged plugin "
                "teardown hook (step 6) would freeze the UI. Callers should "
                "dispatch this on its own thread."
            )

        # Step 2 defuses the screensaver directly. Never call
        # set_enable(False) or hide() here. hide() takes _load_page_lock and
        # runs a full load_page(), which resurrects the deck during close()
        # whenever the screensaver is showing at unplug.
        screen_saver = getattr(self, "screen_saver", None)
        if screen_saver is not None:
            if screen_saver.timer:
                screen_saver.timer.cancel()
            screen_saver.enable = False
            screen_saver.showing = False

        # Step 3 stops the library's read thread first, so a stray input
        # callback cannot fire into the teardown below and the
        # resume-from-suspend loop cannot reopen the device.
        if getattr(self, "deck", None) is not None:
            try:
                self.deck.stop_read_thread()
            except Exception:
                log.opt(exception=True).warning("Failed to stop the deck's read thread during close()")

        # Step 4 stops and joins the tick thread before any action teardown.
        # Its body iterates every input's active state unguarded, and a
        # concurrent clear_action_objects() can kill the loop or recomposite
        # an input that the sweep is removing.
        self.keep_actions_ticking = False
        tick_stop_event = getattr(self, "_tick_stop_event", None)
        if tick_stop_event is not None:
            tick_stop_event.set()
        tick_thread = getattr(self, "tick_thread", None)
        if tick_thread is not None and tick_thread is not threading.current_thread():
            tick_thread.join(2.0)

        # Step 5 runs a bounded terminal clear and close through the sole
        # writer. When close_all() already drove this controller through
        # ClearAndCloseMsg on the app-quit path, the loop exited and this does
        # nothing. submit_control rejects a submission after stop, and stop()
        # polling an already-dead thread returns at once.
        media_player = getattr(self, "media_player", None)
        if media_player is not None:
            try:
                media_player.submit_control(ClearAndCloseMsg())
            except Exception:
                log.opt(exception=True).warning("Failed to submit ClearAndClose during close()")
            media_player.stop(timeout=2.0)

        # Step 6 runs the action teardown, and the app-quit path skips it.
        # on_quit runs synchronously on main against a 6s force-quit deadline,
        # and a hook that marshals to main blocks it. Device hygiene matters
        # at quit, not plugin notification.
        #
        # The join is bounded. A wedged plugin teardown hook strands this
        # thread inside step 6, so steps 7 to 9 never run, the unplug leak
        # returns, and _closing=True makes a retry a permanent no-op. On
        # timeout this abandons the daemon hook thread, because finishing the
        # device and registration teardown matters more than a hook that may
        # never return.
        #
        # An abandoned thread can still run while steps 7 to 9, and a later
        # GC, proceed. The surface is narrow. The wedge is a plugin hook, and
        # plugin hooks run in the first step of _teardown_actions,
        # clear_action_objects, before its screensaver-input and background
        # cleanup. So an abandoned thread parks in clear_action_objects and,
        # while it stays wedged, does not reach the close_resources() and
        # original_inputs.clear() that step 7 also touches. The only state it
        # can still mutate is the action_objects that step 8's
        # discard_controller drops. A change that moves resource cleanup ahead
        # of the hooks in _teardown_actions widens this.
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

        # Step 7 sweeps the resources. The writer is stopped, so no
        # concurrent paint touches these caches and objects.
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
        # Fallback close. The writer normally closed the device from step 5's
        # ClearAndCloseMsg. This matters only when that writer wedged and
        # never processed it.
        if getattr(self, "deck", None) is not None:
            try:
                self.deck.close()
            except Exception:
                pass

        # Step 8 deregisters, and it also writes. It flushes every page still
        # cached for this deck before it drops the entries that hold them.
        # Otherwise the dead controller's active_page stays unevictable and
        # distorts every other deck's budget.
        if gl.page_manager is not None:
            gl.page_manager.discard_controller(self)
        self.active_page = None
        # A page change deferred while the screensaver showed otherwise pins
        # its whole page object graph on this dead controller. This runs at
        # teardown only, and it leaves the pending mechanism itself, and
        # active_page while a screensaver shows, untouched.
        self._screensaver_pending_page = None

        # Step 9 shuts down the per-deck thread pools. The object graph is
        # cyclic, from actions to pages to controller, so an explicit collect
        # reclaims it now instead of at the next generational GC pass.
        action_executor = getattr(self, "action_executor", None)
        if action_executor is not None:
            # Do not wait. A misbehaving plugin callback can block a worker
            # forever, and the app's force_quit timer is the backstop.
            action_executor.shutdown(wait=False, cancel_futures=True)
            self.action_executor = None
        load_executor = getattr(self, "load_executor", None)
        if load_executor is not None:
            load_executor.shutdown(wait=False, cancel_futures=True)
            self.load_executor = None
        gc.collect()

    def _teardown_actions(self) -> None:
        """Tear down every action this controller ever cached a page for, not
        only active_page, plus the screensaver's stashed input set and
        background when the deck closes during the screensaver. That is where
        the real page's 50-150MB of media lives then, not on active_page. No
        caller runs this under _load_page_lock, and none runs it at
        app_quit."""
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
        """Thin alias for close(), kept for existing callers such as the
        harness teardown() helper."""
        self.close(remove_media=True, app_quit=False)

    def get_alive(self) -> bool:
        try:
            return self.deck.is_open()
        except Exception as e:
            log.debug(f"Cougth dead deck error. Error: {e}")
            return False
