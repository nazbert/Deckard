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

The controller inputs: the objects a deck's keys, dials and touchscreen are
made of, and the per-state content each of them carries.

Two hierarchies, paired one to one. ControllerInput owns the hardware-facing
half -- the identifier, the event callbacks the HID reader drives, and the
paint path that composites, encodes and hands a frame to the media thread.
ControllerInputState owns the content half -- media, labels, layout and
background, plus the action dispatch that turns a physical event into plugin
callbacks. An input keeps one state object per configured state index and
delegates to whichever is current; StateT is what keeps that delegation
typed, so ControllerKey.get_active_state() is a ControllerKeyState with no
cast at the call site.

Nothing here runs on a thread it owns. The HID reader delivers key, dial and
touchscreen events; the media thread drives on_media_player_tick; plugin
callbacks arrive on the action pool; page loads arrive on the loader pool.
That is what the DOWN-time gesture snapshot, the dual-hash dedup slots and
_states_lock are all for, each documented at the code it protects. And
nothing here writes to the deck: a paint is encoded and enqueued for the
media thread, which is the sole writer.
"""
import os
import threading
import time
from copy import copy

from PIL import Image, ImageDraw, ImageEnhance, ImageOps
from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from loguru import logger as log

from src.backend.DeckManagement.HelperMethods import is_image, is_svg, is_video, svg_to_pil
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.backend.DeckManagement.Media.MediaConfig import MediaConfig
from src.backend.DeckManagement.Subclasses.ActionPermissionManager import ActionPermissionManager
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend.DeckManagement.deck_controller.gif_pipeline import GifBackground, GifBudgetExceeded, KeyGIF
from src.backend.DeckManagement.deck_controller.label_engine import BackgroundManager, LabelManager, LayoutManager
from src.backend.DeckManagement.deck_controller.media_writer import (
    KEY_ENCODE_QUALITY,
    encode_native_key,
    encode_native_touchscreen,
)
from src.backend.PageManagement import page_pins
from src.backend.PageManagement.Page import ActionOutdated, NoActionHolderFound, Page
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend import timer_wheel
from src.backend import ui_port
from src.Signals import Signals

import globals as gl

from typing import TYPE_CHECKING, Generic, TypeVar
if TYPE_CHECKING:
    from concurrent.futures import Future
    from threading import Timer

    from src.backend.DeckManagement.deck_controller.controller import DeckController


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

    def __init__(self, deck_controller: "DeckController", state_class: type[StateT], identifier: InputIdentifier):
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
    def __init__(self, deck_controller: "DeckController", ident: Input.Key):
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

        # Hold the page this press landed on for the whole callback: a
        # press that changes pages still owes its remaining events to the
        # page it was pressed on. Structural, because a raising body that
        # skipped a hand-written release would pin that page against
        # eviction for the life of the process, once per press.
        with page_pins.holding(self.deck_controller.active_page):
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
    def __init__(self, deck_controller: "DeckController", ident: InputIdentifier):
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
    def __init__(self, deck_controller: "DeckController", ident: InputIdentifier):
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
