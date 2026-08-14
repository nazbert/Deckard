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

The controller inputs are the objects a deck's keys, dials and touchscreen
are made of, plus the per-state content each of them carries.

Two hierarchies pair one to one. ControllerInput owns the hardware-facing
half, which is the identifier, the event callbacks the HID reader drives, and
the paint path that composites, encodes and hands a frame to the media
thread. ControllerInputState owns the content half, which is media, labels,
layout and background, plus the action dispatch that turns a physical event
into plugin callbacks. An input keeps one state object per configured state
index and delegates to whichever is current. StateT keeps that delegation
typed, so ControllerKey.get_active_state() gives a ControllerKeyState with no
cast at the call site.

Nothing here runs on a thread it owns. The HID reader delivers key, dial and
touchscreen events, the media thread drives on_media_player_tick, plugin
callbacks arrive on the action pool, and page loads arrive on the loader
pool. The DOWN-time gesture snapshot, the dual-hash dedup slots and
_states_lock all exist for that, each documented at the code it protects.
Nothing here writes to the deck either. A paint is encoded and enqueued for
the media thread, which is the sole writer.
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

        # True while this state's on_tick is still running. The next tick is
        # dropped and not queued. See own_actions_tick_threaded.
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
            # Cancel any in-flight hide timer first, so a repeated overlay
            # does not orphan its thread.
            self.stop_overlay_timer()
            self._overlay = image
            self.update()
            self.hide_overlay_timer = timer_wheel.schedule(duration, self.hide_error, name="OverlayHideTimer")
        else:
            self._overlay = image
            self.update()

    def hide_overlay(self):
        # Set None, not False. The tile-passthrough fast path in
        # ControllerKey.get_current_image tests state._overlay is None.
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
        # Snapshot once and use the snapshot throughout. Other threads null
        # or swap active_page, from close() and from load_page, so a re-read
        # of the live attribute after the None check races that window and
        # raises AttributeError out of every own_actions_ caller.
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
            # Gate on ready_finished, not on ready_called. The default
            # on_update calls on_ready for compatibility, so a dispatch here
            # during initialization runs a second on_ready beside the pool's
            # first one, which duplicates backend processes. A skip loses
            # nothing, because the initial ready sequence ends with its own
            # on_update.
            if not action.on_ready_finished:
                continue
            action.on_update()

    @log.catch
    def own_actions_tick(self) -> None:
        for action in self.get_own_actions():
            if not isinstance(action, ActionCore):
                continue
            # on_ready_called is true from schedule time, so a tick must wait
            # for on_ready to finish.
            if not action.on_ready_finished:
                continue
            action.on_tick()

    @log.catch
    def own_actions_event_callback(self, event: InputEvent, data: dict = None, show_notifications: bool = False, actions: list = None) -> None:
        # actions lets the caller pin the dispatch to a list resolved
        # earlier, such as the DOWN-time gesture snapshot of ControllerKey. By
        # default it resolves here, when the pool worker runs, which reads
        # deck_controller.active_page and so tracks any page swap between the
        # event and this dispatch.
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

            # A pinned snapshot, the DOWN-time gesture list of ControllerKey,
            # can outlive its page's cache entry. The key handler's hold on
            # the pressed page ends when the DOWN callback returns, not at
            # gesture end, so a mid-hold eviction, a remove_page or a
            # reload-diff can run ActionCore.teardown on a snapshot member
            # while its UP is still owed. Never dispatch into a torn-down
            # action. _cleaned_up is the idempotency marker of clean_up(), set
            # under _cleanup_lock. The lock-free read here is benign. At
            # worst one event reaches an action during teardown, the same
            # envelope live resolution always had.
            if getattr(action, "_cleaned_up", False):
                continue

            # Isolate each action. The method-level @log.catch aborts this
            # whole loop at the first raiser and starves every later action in
            # the list of its event.
            try:
                action._raw_event_callback(event, data)
            except Exception:
                log.opt(exception=True).error(
                    f"Action {getattr(action, 'action_id', action)} raised handling {event}"
                )

    def _submit_action_callback(self, fn, *args) -> "Future | None":
        """Route an action callback through the deck's bounded thread pool.
        Returns the Future, or None when the executor is gone because the deck
        is tearing down.
        """
        executor = getattr(self.deck_controller, "action_executor", None)
        if executor is None:
            return None
        try:
            future = executor.submit(fn, *args)
        except RuntimeError:
            # The executor already shut down; the deck disconnected mid-call.
            return None
        future.add_done_callback(self._log_callback_exception)
        return future

    def own_actions_update_threaded(self) -> None:
        self._submit_action_callback(self.own_actions_update)

    def own_actions_tick_threaded(self) -> None:
        # Drop this tick, and do not queue it, while the previous one still
        # runs, so a slow plugin on_tick() cannot pile up callbacks.
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
        """Attach this state's still media, or clear it with None.

        This is the media protocol that ActionCore.set_media drives an input
        state through. ControllerKeyState and ControllerDialState implement
        it, and ControllerTouchScreenState does not. Nothing reaches this base
        body, because ActionCore.set_media returns early for any identifier
        outside Input.Key and Input.Dial. The declaration exists so the
        protocol is checkable at that call site. A touchscreen media route
        must override it rather than inherit this.
        """
        raise NotImplementedError

    def set_video(self, video: "InputVideo | KeyGIF", /) -> None:
        """Attach this state's animated media. See set_image for who
        implements it and why the base body is unreachable. It accepts both
        providers; the .gif route builds a KeyGIF and every other route builds
        an InputVideo."""
        raise NotImplementedError

    def remove_media(self) -> None:
        page = self.controller_input.deck_controller.active_page
        if page is None:
            return

        # A None path clears the media.
        page.set_media_path(identifier=self.controller_input.identifier, state=self.state, path=None)  # type: ignore[arg-type]  # root cause: Page.set_media_path declares path: str while None is the clear-media value (PageManagement/Page.py)

        self.update()


#: The state class an input owns. Each ControllerInput subclass pins exactly
#: one, so ControllerKey pins ControllerKeyState. That lets the shared state
#: plumbing below stay in the base class without erasing the subclass's state
#: type at every get_active_state() call.
StateT = TypeVar("StateT", bound="ControllerInputState")


class ControllerInput(Generic[StateT]):
    # Per-input dedup slots, which the paint path creates lazily. update()
    # reads them through getattr with a None default before the first paint
    # writes them. They are declared and not assigned, so the annotation adds
    # no attribute at runtime and the lazy-creation contract stands.
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
        # Generation of the content this input holds. A paint tags it at
        # render start, and the write boundary drops that paint once a newer
        # generation supersedes it.
        self.config_gen: int = 0

        self.is_visual: bool = True

        self.enable_states: bool = True

        # Serializes state-object replacement, from create_n_states during a
        # load, against an action media write from ActionCore.set_media. A
        # paint must land fully before the wipe, so the load's stash and
        # restore carries it over, or fully after it, on the recreated state
        # object. It must never land on a destroyed state.
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
            # No page is loaded, at boot or during teardown, so there is
            # nothing to persist the new state onto.
            return
        d = self.identifier.get_config(page)

        self.states[len(self.states)] = self.ControllerStateClass(self, len(self.states))
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
            # As in add_new_state, no page means nothing to edit.
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
        """Kept as the plugin-facing name; the widget work belongs to the
        adapter. The adapter guards the sidebar reach and marshals it to the
        main thread, so a plugin or action thread cannot raise AttributeError
        before the window exists, or mutate a widget off main after it.
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
        """Kept as the plugin-facing name; the widget work belongs to the
        adapter. The visible-child read runs inside the adapter's idle
        callback, together with the refresh, so no caller reads GTK off the
        main thread.
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
        # This is abstract by convention. ControllerKeyState and
        # ControllerTouchScreenState define clear(), so a dial raises
        # AttributeError here. That defect is open.
        active_state.clear()  # type: ignore[attr-defined]  # root cause: ControllerDialState has no clear()
        if update:
            self.update()

    def close_resources(self) -> None:
        """Framework teardown hook that releases every state's media
        resources. It serves the input's own end of life, at a deck close or
        a screensaver-stash sweep, and not a fresh page load as clear() does,
        so it never triggers a repaint."""
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
        # No ControllerInput subclass overrides this, so every caller gets
        # the base None. KeyImage tolerates it.
        return None

    def get_image_size(self) -> tuple[int, int]:
        # ControllerKey, ControllerTouchScreen and ControllerDial each
        # override this, so the base never answers.
        raise NotImplementedError

class ControllerKey(ControllerInput["ControllerKeyState"]):
    def __init__(self, deck_controller: "DeckController", ident: Input.Key):
        super().__init__(deck_controller, ControllerKeyState, ident)
        self.index = ident.get_index(deck_controller)
        # Seed the cached press state from the device so event_callback can
        # compare against it. key_states() is indexed logically, with the
        # rotation applied there, so self.index selects this key's own state.
        self.press_state: bool = self.deck_controller.deck.key_states()[self.index]

        self.down_start_time: float | None = None

        # DOWN-time gesture snapshot, a (state, actions) pair captured when
        # the key went down, or None outside a gesture. The rest of the
        # gesture dispatches to this snapshot, and not to whatever the key
        # resolves to at release time. A ChangePage action on this key swaps
        # active_page, and rebuilds this key's states, synchronously during
        # the DOWN dispatch. Live resolution would then send the UP to the new
        # page's actions, so the old page's actions never see their release
        # and a registered-down latch jams shut, while the new page's actions
        # get a SHORT_UP for a press that was not theirs.
        #
        # It is one attribute and not one per field, so a writer clears it in
        # one atomic store and the hold-timer callback, which can race the UP
        # branch past its cancel(), reads a coherent pair or None and never a
        # torn half. The deck's serialized input-callback path writes it, and
        # so does the cancel_gesture sweep of ScreenSaver.show(), which runs
        # under _load_page_lock after this key left the live input set and can
        # receive no further event.
        self._gesture: tuple | None = None

    def cancel_gesture(self) -> None:
        """End an in-flight gesture without a dispatch of its release
        events. It drops the DOWN-time snapshot, the gesture clock and the
        pending hold timer. It serves the paths where the physical release
        can never reach this key. ScreenSaver.show() confiscates the whole
        input set mid-hold, and the release then lands on the replacement key
        and is swallowed. Without this call, the hold timer stays armed, fires
        HOLD_START into the pinned snapshot after the finger left, and pins
        that snapshot's action objects forever."""
        self.down_start_time = None
        self.stop_hold_timer()
        self._gesture = None

    def on_hold_timer_end(self):
        gesture = self._gesture
        if gesture is None:
            # The gesture already ended. The UP branch or a cancel_gesture()
            # raced this callback past the timer's cancel(). A late
            # HOLD_START must not fire, and must never live-resolve onto
            # whatever page is active now.
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
        # Capture the page and the generation before the render, so a switch
        # mid-render invalidates this paint at the write boundary.
        page = self.deck_controller.active_page
        config_gen = self.config_gen

        # Frame-identity fast path. A passthrough key over a video background
        # composites to exactly the shared tile, so its native bytes are a
        # pure function of the frame they came from, and nothing has to
        # serialize, hash or re-encode a pixel to know what belongs on the
        # device. Steady-state playback of a loop then costs one dict lookup
        # and the USB write.
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

        # Quick hash check. Skip the expensive conversion only when the image
        # matches both the last presented hash, which the task's run() sets,
        # and the last enqueued hash. Either one alone can be stale, after a
        # dropped paint or an in-flight revert, and would wrongly skip the
        # correcting repaint.
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
        """The device-ready RGB form of a composited key image. It
        composites RGBA onto RGB to keep the edges smooth. It never mutates
        image, because both branches build a new one, which lets the
        frame-identity path pass the shared background tile in with no copy
        first."""
        rotation = self.deck_controller.deck.get_rotation()
        if image.mode == "RGBA":
            rgb_background = Image.new("RGB", image.size, (0, 0, 0))
            rgb_background.paste(image, (0, 0), image)
            return rgb_background.rotate(rotation)
        return image.convert("RGB").rotate(rotation)

    def _update_from_tile_identity(self, identified: tuple, page, config_gen, force: bool) -> None:
        """Present a passthrough key straight from its frame identity; see
        update(). identified is the (tile, (video md5, frame index)) pair that
        Background handed out as one read."""
        tile, (video_md5, frame_index) = identified

        if media_prof:
            _t0 = time.perf_counter()

        # This stands in for the pixel hash wherever the write-boundary
        # bookkeeping needs one. It is stable for a frame and distinct across
        # frames and keys. The skip still needs both the last presented hash,
        # which the task's run() sets, and the last enqueued one to match.
        # Either one alone can be stale, after a dropped paint or an in-flight
        # revert, and would wrongly skip the correcting repaint.
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

        # The in-app preview wants a PIL image, and every other reader of
        # this frame shares the tile, so hand the UI its own copy.
        self.set_ui_key_image(copy(tile))

    def get_active_state(self) -> "ControllerKeyState":
        return super().get_active_state()

    def on_media_player_tick(self) -> None:
        self.media_ticks += 1

        state = self.get_active_state()
        needs_update = False

        # A rolling label advances its state here, on the tick, whether or
        # not anything else forces a repaint, because rendering is pure. The
        # key re-renders only when a scroll offset visibly moved, instead of
        # producing 30 frames a second that the hash de-dup discards.
        scroll_moved = False
        if state.label_manager.get_has_scroll_labels():
            scroll_moved = state.label_manager.tick_scroll_labels()

        # Decide on an update from the content type.
        if state.key_video is not None:
            # InputVideo and KeyGIF both pick their current frame from their
            # own wall-clock timeline, so the tick asks for whatever frame is
            # current and computes no GIF frame delay of its own.
            needs_update = True
        elif scroll_moved:
            needs_update = True
        elif self.deck_controller.background.video is not None:
            # An opaque background color hides the video tile, as
            # get_current_image shows, so that key cannot change per frame.
            if state.background_manager.get_composed_color()[-1] < 255:
                needs_update = True

        if needs_update:
            self.update()

    def event_callback(self, press_state):
        screensaver_was_showing = self.deck_controller.screen_saver.showing
        if press_state:
            # Only on key down. This lets a plugin control the screensaver
            # without a direct deactivation.
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            if not press_state:
                # A release the screensaver swallows still ends the physical
                # gesture. Without this, a snapshot pinned by a DOWN before
                # the screensaver is never dropped and its hold timer keeps
                # running, so HOLD_START fires after the finger left. show()
                # already cancels gestures on the input set it stashes, so a
                # live gesture on this key means the screensaver engaged
                # without the swap. Keep the two paths independent.
                self.cancel_gesture()
            return

        # Hold the page this press landed on for the whole callback. A press
        # that changes pages still owes its remaining events to the page it
        # was pressed on. The context manager releases it, because a raising
        # body that skipped a hand-written release would pin that page against
        # eviction for the life of the process, once per press.
        with page_pins.holding(self.deck_controller.active_page):
            self.press_state = press_state

            self.update()

            active_state = self.get_active_state()
            if press_state: # Key down
                self.down_start_time = time.time()
                # Snapshot the state and its resolved actions here; see
                # __init__. Every event of this gesture goes to the actions
                # that were on the key when the finger landed, whatever page
                # swaps happen in between. That includes this DOWN, which
                # otherwise resolves actions when the pool worker runs.
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
                # The gesture is complete. Drop the snapshot in one atomic
                # store, so a superseded page's action objects are not pinned
                # past their last event.
                self._gesture = None

            else: # Key up with no gesture clock
                # The screensaver swallowed the matching DOWN, or something
                # already cleared its bookkeeping; a screensaver show and hide
                # cycle mid-hold resets down_start_time on the live keys.
                # There is nothing to dispatch, but a hold timer still armed,
                # or a snapshot still pinned, from that orphaned DOWN must not
                # outlive the physical release.
                self.cancel_gesture()

    def _tile_passthrough_ok(self, state: "ControllerKeyState") -> bool:
        """Whether this key composites to exactly the shared background
        tile, with no color layer, media, label or marker over it. It gates
        the composite fast path in get_current_image and the frame-identity
        fast path in update(). One definition keeps the two from disagreeing
        about which keys are bare."""
        return (state.background_manager.get_composed_color()[-1] == 0
                and state._overlay is None
                and state.key_image is None
                and state.key_video is None
                and not state.label_manager.get_has_visible_labels()
                and not self.is_pressed()
                and not (self.has_unavailable_action() and not self.deck_controller.screen_saver.showing))

    def get_current_image(self) -> Image.Image:
        state = self.get_active_state()

        # A bare key's composite is the shared background tile, so return a
        # copy of it directly. That saves work per frame over an animated
        # background.
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
        # Load the background image only when the background color does not
        # hide it.
        if background_color[-1] < 255:
            background = copy(self.deck_controller.background.tiles[self.index])

        if background_color[-1] > 0:
            background_color_img = Image.new("RGBA", self.deck_controller.get_key_image_size(), color=tuple(background_color))
            
            if background is None:
                # Use the color as the only background. This happens at a
                # background color alpha of 255.
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
                # A static asset has a cacheable resize; a video or GIF does
                # not.
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

        # A key with no visible label gets its own composite back, because
        # add_labels_to_image skips the copy, and with no media key_image is
        # background. An unconditional close on either would hand the media
        # thread an image whose buffer is already released.
        if background is not None and background is not labeled_image:
            background.close()

        if key_image is not labeled_image:
            key_image.close()

        return labeled_image
    
    def add_warning_point(self, image: Image.Image, margin: int = 10, size: int = 10, color: tuple = (255, 150, 80)) -> Image.Image:
        draw = ImageDraw.Draw(image)

        # Find the coordinates of the top right circle.
        width, height = image.size
        top_right_x = width - margin - size
        top_right_y = margin

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
        Disabling load_media can also disable custom user assets.
        """
        n_states = len(input_dict.get("states", {}))

        # create_n_states destroys every state object and closes any
        # action-set media. Only on_update() can repaint afterwards, and an
        # action that dedups there never does, so the key settles permanently
        # blank. Detach the action-owned media, and its action layout, before
        # the wipe. Restore it only when the exact action object that painted
        # it still drives the recreated state. A same-page reload reuses the
        # action objects, so the identity matches and the paint returns. A
        # cross-page load builds new ones, so the identity differs, the media
        # closes and nothing bleeds. This runs under _states_lock, so a
        # concurrent set_media paint lands fully before the wipe, where the
        # stash carries it over, or fully after it, on the recreated state,
        # and never on a destroyed state object.
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

            # Reset the action layout, except on a state whose action-owned
            # media the block above restored. Its action layout belongs to the
            # same action, which is still present, and a reset would restore
            # the image but lose the alignment and the size.
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
                            # KeyGIF parses eagerly and raises on a corrupt or
                            # truncated GIF, where the detached cv2 builder of
                            # InputVideo fails soft. Without this guard, one
                            # bad asset in a page's config takes the whole
                            # page load down. The fallback is the opaque cv2
                            # path, as on the set_media route.
                            #
                            # This contains the GIF-specific parse and decode
                            # failures only. It does not make the page load
                            # total. The InputVideo constructor stats and
                            # hashes the file, so an EACCES, EIO or ENOENT
                            # still escapes from the fallback itself, as it
                            # does for every non-GIF video on this route.
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
                                # speed, and the dict fps from the sidebar FPS
                                # row is a render cap. Plugin media through
                                # set_media keeps fps as the playback rate, an
                                # explicit API argument.
                                natural_speed=True,
                            )) # Videos always update
                    # This chain ends here. Do not add an elif that calls
                    # self.set_key_image(), which ControllerKey does not
                    # define. Such a branch fires on the normal
                    # load_media=True path whenever path is a non-empty string
                    # that is not a valid image, svg or video, such as a
                    # dangling config path, and it raises AttributeError. A
                    # bad path must stay a no-op.

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
            # The port refused the push, because there is no UI, the window
            # is unmapped or the grid is mid-rebuild, or the push raised. Mark
            # the input dirty only. KeyGrid.load_from_changes recomposites a
            # fresh image on map instead of replaying this one. A frame the
            # port accepts and later drops marks itself; see
            # ui_adapter.mark_dirty.
            self.deck_controller.ui_image_changes_while_hidden[self.identifier] = True


    def get_own_ui_key(self):
        """Deprecated in-process shim. The attached UI resolves its own
        widget for this input. Returns None when headless."""
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

        # Quick hash check. Skip the expensive encode and enqueue only when
        # the image matches both the last presented hash, which the task's
        # run() sets, and the last enqueued hash. Either one alone can be
        # stale, after a dropped paint or an in-flight revert, and would
        # wrongly skip the correcting repaint. ControllerKey.update uses the
        # same dual-hash guard. It saves a redundant 800x100 JPEG write on an
        # unchanged composite.
        img_hash = hash(image.tobytes())
        if (img_hash == getattr(self, '_last_img_hash', None)
                and img_hash == getattr(self, '_last_enqueued_hash', None)):
            image.close()
            return

        # Finish the device work with image before the UI mirror gets it, so
        # the media thread does not read it while GTK copies it. The
        # touchscreen supports JPEG only, so composite RGBA onto black.
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
        # InputVideo sizes its frame cache from this. For the touchscreen
        # that is the full strip.
        return self.get_screen_dimensions()

    def on_media_player_tick(self) -> bool:
        # A per-touchscreen background video advances on the media tick, as
        # dial content does, and the caller re-composites the shared
        # touchscreen once per frame. The screensaver owns the strip while it
        # shows.
        if self.deck_controller.screen_saver.showing:
            return False
        state = self.get_active_state()
        # Snapshot it, because _release_background_video() nulls this from a
        # compositing thread between the check and the fps read.
        bg_video = None if state is None else state.background_video
        if bg_video is None:
            return False
        # The configured fps is a render cap. The playback position follows
        # the wall clock at the source's native fps, so a skipped tick here
        # drops a frame and does not slow the video down.
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
            # Mark the input dirty only. ScreenBar.load_from_changes
            # recomposites a fresh image on map instead of replaying this one.
            # The adapter owns the preview throttle and its tail flush, which
            # re-marks a frame the window unmapped out from under.
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
        
        # Touchscreen events arrive pre-classified from the library, as
        # SHORT, LONG or DRAG. They are single events with no DOWN and UP
        # tail, so there is no gesture snapshot to keep. The default dispatch
        # resolves the target actions against active_page when the pool worker
        # runs, so a page swap between the event and the worker redirects the
        # event to the new page's actions, as it does for a dial TURN.
        # Resolve at read time instead, here on the deck's input thread.
        active_state = self.get_active_state()
        if event_type == TouchscreenEventType.DRAG:
            drag_actions = active_state.get_own_actions()
            # Check whether the drag went left to right, or the other way.
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

        # DOWN-time gesture snapshot, the dial twin of ControllerKey._gesture;
        # see its __init__ for the full reasoning. It is a (state, actions)
        # pair captured when the dial went down, or None outside a gesture.
        # The gesture tail dispatches to this snapshot, and not to whatever
        # the dial resolves to at release time. A ChangePage on this dial's
        # DOWN swaps active_page mid-gesture, and live resolution would send
        # the tail to the new page's dial actions and jam a registered-down
        # latch. It is one attribute, so a writer clears it in one atomic
        # store and the hold-timer callback reads a coherent pair or None,
        # never a torn half.
        self._gesture: tuple | None = None

    def cancel_gesture(self) -> None:
        """End an in-flight gesture without a dispatch of its release
        events. It drops the DOWN-time snapshot, the gesture clock and the
        pending hold timer, on the same contract as
        ControllerKey.cancel_gesture. It serves the paths where the physical
        release can never reach this dial; ScreenSaver.show() confiscates the
        whole input set mid-hold, and the release then lands on the
        replacement dial and is swallowed."""
        self.down_start_time = None
        self.stop_hold_timer()
        self._gesture = None

    def on_hold_timer_end(self):
        gesture = self._gesture
        if gesture is None:
            # The gesture already ended. The UP branch or a cancel_gesture()
            # raced this callback past the timer's cancel(). A late
            # HOLD_START must not fire, and must never live-resolve onto
            # whatever page is active now.
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
            # Only on push, not on hold. That lets an action enable the
            # screensaver without waking it again at once.
            self.deck_controller.screen_saver.on_key_change()
        if screensaver_was_showing:
            if event_type == DialEventType.PUSH and not value:
                # A release the screensaver swallows still ends the physical
                # gesture; see the matching branch in
                # ControllerKey.event_callback. show() already cancels
                # gestures on the input set it stashes, and this covers the
                # case where the screensaver engaged without the swap.
                self.cancel_gesture()
            return

        active_state = self.get_active_state()
        if event_type == DialEventType.PUSH:
            if value:
                self.down_start_time = time.time()
                # Snapshot the state and its resolved actions here; see
                # __init__. Every event of this gesture goes to the actions
                # that were on the dial when it was pressed, whatever page
                # swaps happen in between. That includes this DOWN, which
                # otherwise resolves actions when the pool worker runs.
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
                # The gesture is complete. Drop the snapshot in one atomic
                # store, so a superseded page's action objects are not pinned
                # past their last event.
                self._gesture = None
            else:
                # Release with no gesture clock. Something swallowed the
                # matching DOWN, or already cleared its bookkeeping. There is
                # nothing to dispatch, but a hold timer still armed, or a
                # snapshot still pinned, from that orphaned DOWN must not
                # outlive the release.
                self.cancel_gesture()

        elif event_type == DialEventType.TURN:
            # Resolve the target actions at read time. A turn is a single
            # event, but the default dispatch resolves against active_page
            # when the pool worker runs, and a page swap in that window
            # redirects the turn to the new page's actions.
            turn_actions = active_state.get_own_actions()
            # value is the signed detent count of the HID report. A fast
            # rotation coalesces several detents into one report, so forward
            # the magnitude instead of one single event.
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
                            # User-assigned media plays at the source's
                            # speed, and the dict fps from the sidebar FPS row
                            # is a render cap. Plugin media through set_media
                            # keeps fps as the playback rate, an explicit API
                            # argument.
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
        # Advance the animation clock and report whether a redraw is needed.
        # The caller renders the shared touchscreen once per frame.
        self.media_ticks += 1

        state = self.get_active_state()
        if state is None:
            return False
        # A rolling label advances here on the tick, because rendering is
        # pure. The strip re-renders only when a scroll offset visibly moved.
        scroll_moved = False
        if state.label_manager.get_has_scroll_labels():
            scroll_moved = state.label_manager.tick_scroll_labels()
        return state.video is not None or scroll_moved

    def get_image_size(self) -> tuple[int, int]:
        if self.deck_controller.deck.is_touch():
            touch_screen = self.get_touch_screen()
            if touch_screen is not None:
                return touch_screen.get_dial_image_area_size()
        # (0, 0) is the established answer for a dial with no strip, which
        # means no visual target. KeyImage._budget_size keys off exactly it.
        return (0, 0)
    

class ControllerTouchScreenState(ControllerInputState):
    # set_current_image() creates this lazily, so close_resources() guards it
    # with getattr; a state closed before its first render never has one. It
    # is declared and not assigned, so that contract stands at runtime.
    current_image: Image.Image | None

    def __init__(self, controller_touch: "ControllerTouchScreen", state: int):
        super().__init__(controller_touch, state)

        self.controller_touch = controller_touch

        # (key, fitted-image-or-None) for _get_fitted_background_image.
        self._fitted_background_cache: "tuple[tuple | None, Image.Image | None]" = (None, None)

        # Playback state for a video configured as this touchscreen's
        # background. It is an InputVideo over a strip-sized shared frame
        # cache, which the media tick advances.
        # _get_background_video_frame manages it, and get_current_image
        # releases it once the background stops being a video. The lock
        # covers the create and the release, because a composite can run on
        # the media thread and on a load or UI thread at the same time. The
        # .gif route builds a GifBackground and every other route builds an
        # InputVideo. Both answer the get_next_frame, close and video_path
        # surface that _get_background_video_frame drives them through.
        self.background_video: "InputVideo | GifBackground | None" = None
        self._background_video_failed: str | None = None
        self._background_video_lock = threading.Lock()
        # The display-saturation factor that background_video was built at,
        # and that acquired its shared tile cache. The keep-check in
        # _get_background_video_frame uses it. The factor bakes into the cache
        # at construction and set_playback never revisits it, so a reuse of
        # the video across a saturation change keeps serving frames enhanced
        # at the old factor.
        self._background_video_saturation: float | None = None
        # Timestamp gate for the fps render cap in on_media_player_tick.
        self._last_background_video_render: float = 0.0

    def set_current_image(self, image: Image.Image):
        self.current_image = image

        self.update()

    def _get_fitted_background_image(self, path: str, size: tuple[int, int]) -> Image.Image | None:
        # Decode and fit once per (path, mtime, size, saturation), then
        # cache. This runs on every composite, 30 times a second while a
        # background video plays, and a failed decode must not log per frame.
        # A video takes the playback path in _get_background_video_frame.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None

        # The saturation boost bakes into the cached fitted image, on the
        # same one-time contract BackgroundImage uses for the key grid, so
        # the factor is part of the cache key. A saturation change must not
        # keep serving the stale enhancement. The value rounds to the
        # persisted 2-decimal precision, because set_display_saturation stores
        # round(v, 2), so an unrounded caller cannot mint a near-duplicate
        # float key that misses the cache on every composite.
        saturation = round(self.controller_touch.deck_controller.get_display_saturation(), 2)

        key = (path, mtime, size, saturation)
        cached_key, cached_image = self._fitted_background_cache
        if cached_key == key:
            # A caller pastes dial images onto the returned image in place,
            # so hand out a copy and keep the cached one clean.
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

        # The cache also holds a failure, so a bad file logs once and not on
        # every frame.
        self._fitted_background_cache = (key, fitted)
        return fitted.copy() if fitted is not None else None

    def _get_background_video_frame(self, path: str, fps: int = 30, loop: bool = True) -> Image.Image | None:
        # The InputVideo owns a strip-sized shared frame cache. It picks
        # frames by wall clock, clamps a gap, and runs at the source fps, so
        # neither the composite rate nor the fps setting changes playback
        # speed. fps and loop come from the page's background settings. loop
        # wraps playback, and fps only caps the strip's re-render rate; see
        # ControllerTouchScreen.on_media_player_tick.
        with self._background_video_lock:
            if path == self._background_video_failed:
                return None

            # The saturation is part of the keep-check. The factor bakes into
            # the video's shared tile cache at construction, and set_playback
            # only updates fps and loop, so a factor change forces a rebuild
            # for the same path. The key-grid BackgroundVideo keep-check and
            # the fitted-image cache key one method up work the same way, and
            # this uses the same 0.001 tolerance.
            saturation = self.controller_touch.deck_controller.get_display_saturation()

            video = self.background_video
            # Both reads stay inside the short-circuit.
            # _background_video_saturation exists only once a video attaches;
            # the keepcheck scenario builds this state through __new__ and
            # sets only the attributes the no-video path touches.
            if (video is None or video.video_path != path
                    or self._background_video_saturation is None
                    or abs(self._background_video_saturation - saturation) > 0.001):
                if video is not None:
                    video.close()
                video = None
                if os.path.splitext(path)[1].lower() == ".gif":
                    # A .gif goes to the PIL provider. It fits each frame to
                    # exactly the strip size, because the alpha_composite in
                    # get_current_image needs same-size RGBA, and alpha and
                    # the per-frame delays survive. A budget or decode failure
                    # falls back to the opaque source-fps InputVideo path
                    # below, as the deck-background route in
                    # prebuild_from_path does.
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
                # n_frames is known from construction, because the reader
                # opens its source eagerly, so a value of 0 or less names a
                # bad file. Fail it once instead of a retry and a log per
                # frame. A transient miss on a healthy file retries on the
                # next tick. This applies to InputVideo only. GifBackground
                # has no video_cache, because a bad GIF already fell back at
                # construction, and a None after close is transient and
                # self-heals on the rebuild above.
                if hasattr(video, "video_cache") and (video.video_cache is None or video.video_cache.n_frames <= 0):
                    log.error(f"Could not decode touchscreen background video {path}")
                    video.close()
                    self.background_video = None
                    self._background_video_failed = path
                return None

            # convert() copies. A caller pastes dial images onto the returned
            # composite in place, and the cached payload must stay clean.
            return frame.convert("RGBA")

    def _release_background_video(self) -> None:
        with self._background_video_lock:
            if self.background_video is not None:
                self.background_video.close()
                self.background_video = None

    def get_current_image(self) -> Image.Image:
        screen_width, screen_height = self.controller_touch.get_screen_dimensions()

        # Start with the background image, when one is set.
        background: Image.Image | None = None
        # Snapshot it and guard it. load_page(None) and close() null
        # active_page from other threads while the writer composites, and a
        # blank strip is the only sensible frame then.
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
            # The background stopped being a video, because something cleared
            # it or swapped an image in. Detach its frame cache so the tick
            # predicate goes quiet.
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

        # A deck background extended onto the strip is the bottom layer. An
        # explicit per-touchscreen background image takes precedence over it.
        if background is None:
            deck_background = self.controller_touch.deck_controller.background.get_touchscreen_image()
            if deck_background is not None:
                # convert() copies, because the slice is shared and a caller
                # pastes dial images onto the returned image in place. It also
                # normalizes an RGB video-frame slice for the
                # alpha_composite below.
                background = deck_background.convert("RGBA")

        # Take the background color from the state's background_manager.
        background_color = self.background_manager.get_composed_color()
        
        # With no background image, start empty or colored.
        if background is None:
            # A background color with alpha below 255 starts transparent.
            if background_color[-1] < 255:
                background = self.controller_touch.generate_empty_image()
            
            # A background color with alpha above 0 gets a colored canvas.
            if background_color[-1] > 0:
                background_color_img = Image.new("RGBA", (screen_width, screen_height), color=tuple(background_color))
                
                if background is None:
                    # Use the color as the only background. This happens at
                    # a background color alpha of 255.
                    background = background_color_img
                else:
                    # Paste the color onto the transparent background.
                    background.paste(background_color_img, (0, 0), background_color_img)
            
            # With no background color, use the empty image.
            if background is None:
                background = self.controller_touch.generate_empty_image()
        else:
            # A background image exists, so apply the color overlay if set.
            if background_color[-1] > 0:
                background_color_img = Image.new("RGBA", (screen_width, screen_height), color=tuple(background_color))
                # Blend the color over the image.
                background = Image.alpha_composite(background, background_color_img)

        # Paste the dial images on top of the background.
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

        # Clear the area under the dial image.
        empty_dial = self.get_empty_dial_image()
        # Use the alpha mask when empty_dial has transparency, to stop edge
        # artifacts.
        if empty_dial.has_transparency_data:
            self.current_image.paste(empty_dial, area, empty_dial)
        else:
            self.current_image.paste(empty_dial, area)

        # Contain the image inside the area.
        image = ImageOps.contain(image, (width, height), Image.Resampling.HAMMING)

        # Find the x and y of the centered position.
        x = area[0] + int((width - image.width) / 2)
        y = area[1] + int((height - image.height) / 2)

        self.current_image.paste(image, (x, y), image)

        self.current_image.save("sd.png")

        if update:
            self.update()


    def clear(self):
        self.set_current_image(self.controller_touch.generate_empty_image())

    def close_resources(self) -> None:
        # Only set_current_image() sets current_image. A touchscreen state
        # closed before its first render never gets one, such as a
        # screensaver-stash sweep of a page that never painted, or a fresh
        # state right after create_n_states(). An unconditional dereference
        # raises AttributeError, so the getattr and the None guard make this
        # safe to call any number of times.
        current_image = getattr(self, "current_image", None)
        if current_image is not None:
            current_image.close()
        self.current_image = None
        # Detach the background video's shared-cache reader, as
        # ControllerKeyState and ControllerDialState release their videos.
        self._release_background_video()

class ControllerDialState(ControllerInputState):
    def __init__(self, dial: "ControllerDial", state: int):
        self.dial = dial

        self.image: InputImage | None = None
        # Typed to the provider union of the base protocol; see
        # ControllerInputState.set_video. Only the key route builds a KeyGIF,
        # because the .gif branch of ActionCore guards on ControllerKey, but
        # the slot and the render path handle either provider.
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
        # The base class default does nothing, so this override releases a
        # dial's InputImage and InputVideo from
        # ControllerInput.close_resources(), as
        # ControllerKeyState.close_resources does for a key.
        if self.image is not None:
            self.image.close()
            self.image = None
        if self.video is not None:
            self.video.close()
            self.video = None


    def get_rendered_touch_image(self) -> Image.Image:
        touch_screen = self.dial.get_touch_screen()
        if touch_screen is None:
            # A dial without a strip has nowhere to render. get_image_size()
            # reports (0, 0) for exactly this deck shape.
            return Image.new("RGBA", self.dial.get_image_size(), (0, 0, 0, 0))

        background: Image.Image | None = None

        background_color = self.background_manager.get_composed_color()

        if background_color[-1] < 255:
            background = touch_screen.get_empty_dial_image()
        if background_color[-1] > 0:
            background_color_img = Image.new("RGBA", self.dial.get_image_size(), color=tuple(background_color))

            if background is None:
                # Use the color as the only background. This happens at a
                # background color alpha of 255.
                background = background_color_img
            else:
                background.paste(background_color_img, (0, 0), background_color_img)
        

        if background is None:
            # Unreachable, because every alpha from 0 to 255 satisfies one of
            # the two branches above. ControllerKey.get_current_image keeps
            # the same fallback, so the composite below always has a canvas.
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
        # A .gif key media builds a KeyGIF and every other media builds an
        # InputVideo. Both expose the get_raw_image and close surface that the
        # key paint path and close_resources drive them through.
        self.key_video: "InputVideo | KeyGIF | None" = None
        # The ActionCore that set the current key_image or key_video through
        # set_media(), or None when the page or the user owns the media. Every
        # other media writer resets it to None, and set_media() stamps it
        # again after the write. ControllerKey.load_from_input_dict uses it to
        # carry action-owned media across the create_n_states wipe.
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
            # A drop of key_video here without a close leaks its tile-cache
            # registry attachment and its VideoCapture on every switch from a
            # video to an image.
            self.key_video.close()

        self.key_image = key_image
        self.key_video = None
        self.media_owner_action = None

        if update:
            self.update()

    def set_video(self, key_video: "InputVideo | KeyGIF") -> None:
        if self.key_video is not None:
            # Close the previous video before this one overwrites it.
            self.key_video.close()
        self.key_video = key_video
        if self.key_image is not None:
            self.key_image.close()
        self.key_image = None
        self.media_owner_action = None

    def clear(self):
        if self.key_video is not None:
            # Close key_video here; a bare drop leaks its capture.
            self.key_video.close()
        self.key_image = None
        self.key_video = None
        self.media_owner_action = None
        self.label_manager.clear_labels()
        self.layout_manager.clear()
        self.background_manager.set_page_color(None)
