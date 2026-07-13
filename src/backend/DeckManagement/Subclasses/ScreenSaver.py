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
# Import python modules
import time
from loguru import logger as log
from copy import copy

# Import typing
from typing import TYPE_CHECKING

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend import timer_wheel
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import DeckController, ControllerKey, Background

class ScreenSaver:
    def __init__(self, deck_controller: "DeckController"):
        self.deck_controller: "DeckController" = deck_controller

        # Init vars
        self.original_inputs = []
        self.original_background: "Background" = None
        self.original_brightness: int = 0

        # Time when last key state changed
        self.last_key_change_time = time.time()

        # Time delay
        self.time_delay = 5

        self.enable: bool = False
        self.showing: bool = False

        self.media_path: str = None
        self.brightness: int = 25
        self.fps: int = 30
        self.loop: bool = True
        # timer_wheel.TimerHandle | None -- non-None only while actually
        # armed (enabled and not showing); see set_time/set_enable.
        self.timer: "timer_wheel.TimerHandle" = None
        # True once set_time() has run at least once. DeckController's
        # config load calls set_enable() BEFORE set_time() (P1's own
        # apply_config order), so set_enable(True) at that point must be a
        # no-op rather than arming a timer against the not-yet-loaded
        # time_delay default -- set_time() is what actually arms it.
        self._timer_initialized: bool = False

    def _arm_timer(self) -> None:
        # *60 to go from minutes (how it is stored) to seconds (how the
        # timer needs it).
        self.timer = timer_wheel.schedule(self.time_delay * 60, self.on_timer_end, name="ScreenSaverTimer")

    def set_time(self, time_delay: int) -> None:
        time_delay = max(1, time_delay) # Min 1 minute - too small values leading to instant load if the screensaver lead to errors
        if time_delay != self.time_delay:
            log.info(f"Setting screen saver time delay to {time_delay} minutes")
        self.time_delay = time_delay
        self._timer_initialized = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.enable and not self.showing:
            self._arm_timer()

    def set_media_path(self, media_path: str) -> None:
        self.media_path = media_path

        if self.showing:
            self.deck_controller.background.set_from_path(self.media_path)

    def set_enable(self, enable: bool) -> None:
        self.enable = enable

        if not self._timer_initialized:
            return

        # Hide if showing
        if self.showing and not enable:
            self.hide()

        # Stop timer if enable == False
        if enable:
            if self.timer is None and not self.showing:
                self._arm_timer()
        else:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None

    def on_timer_end(self) -> None:
        self.show()

    def show(self):
        """Serialized show transition (docs/presenter-migration-plan.md §4 M3).

        Three phases:
          1. OUTSIDE any lock: pre-resolve the screensaver's background
             object. Constructing a BackgroundVideo hashes the whole source
             file and opens a capture -- can take seconds -- so this must
             happen before touching _load_page_lock; otherwise every USB-
             event/GTK/action-pool-thread caller of show()/hide()/load_page
             would stall for the duration (G-B1/C-F7).
          2. Under _load_page_lock: coalesce, flip `showing`, swap inputs,
             bump the generation, submit brightness/clear control messages,
             then swap the pre-built background in under
             _background_load_lock with a generation re-check inside (C-F6:
             this is what stops a straggling load_background() worker,
             already blocked on this same lock from an older load_page,
             from overwriting the screensaver's background after we
             release). No plugin callbacks, no GTK marshaling, no file I/O
             happen in this phase -- it's all pure object bookkeeping plus
             the (cheap, no-file-I/O) per-key composite/encode that
             update_all_inputs() already did here before this refactor.
          3. show() has no phase 3: unlike hide(), it never calls load_page.
        """
        if getattr(self.deck_controller, "_closing", False):
            # The deck is tearing down (plan P1.3): a straggling timer fire
            # racing close() must not resurrect the screensaver's transient
            # inputs/background on a controller that's mid-sweep.
            return
        log.info("Showing screen saver")

        # Phase 1 -- outside any lock.
        kind, payload = self.deck_controller.background.prebuild_from_path(
            self.media_path, fps=self.fps, loop=self.loop
        )

        with self.deck_controller._load_page_lock:
            # Coalesce: a concurrent second show() (e.g. a manual show()
            # racing the timer, or two of the six requesters firing at once)
            # is a no-op once we're already showing.
            if self.showing:
                if payload is not None and hasattr(payload, "close"):
                    payload.close()
                return

            # Bump the generation atomically, same pattern as load_page (bump
            # only -- active_page is untouched, this is not a page switch) so
            # post-transition frames outrank pre-transition stragglers
            # (docs/presenter-migration-plan.md §4 M1, pulled forward from M4).
            with self.deck_controller._page_gen_lock:
                self.deck_controller._page_load_generation += 1
                gen = self.deck_controller._page_load_generation

            # Stop timer - in case this method is called manually
            if self.timer:
                self.timer.cancel()
            # Set showing = True - in case this method is called manually
            self.showing = True

            self.original_inputs = self.deck_controller.inputs
            # No `inputs = {}` pre-clear (issue #1 vector a): init_inputs is
            # build-then-swap, so the concurrent media writer sees the old
            # complete dict or the new complete dict, never empty/partial.
            # In-flight key/dial gestures die with the stash: once the swap
            # below lands, the physical release arrives on the REPLACEMENT
            # input set (and is swallowed by the showing-screensaver guard),
            # so a stashed input's hold timer would stay armed and fire
            # HOLD_START into its pinned DOWN-time action snapshot after the
            # finger already left -- mid-screensaver. Cancel them here, while
            # the stashed inputs are still the ones a racing input event
            # would reach. Pure bookkeeping (attribute stores + timer
            # cancels); no callbacks, no locks beyond the ones already held.
            # The touchscreen keeps no gesture state (its events arrive
            # pre-classified, single-shot) -- nothing to cancel there.
            for key in self.original_inputs.get(Input.Key, []):
                key.cancel_gesture()
            for dial in self.original_inputs.get(Input.Dial, []):
                dial.cancel_gesture()
            self.deck_controller.init_inputs()

            self.original_background = self.deck_controller.background
            self.original_brightness = self.deck_controller.brightness

            self.deck_controller.set_brightness(self.brightness)

            self.deck_controller.clear()
            # The seq-stamped ClearMsg (just submitted) wipes image/
            # touchscreen slots; it does not touch the generic `tasks` list
            # (e.g. a straggling load_all_inputs/
            # _update_all_inputs_awaiting_background from an in-flight
            # load_page). That generic wipe is the piece
            # clear_media_player_tasks() still owns here (plan §3.1) -- kept
            # unconditional (no gen arg) since we're inside _load_page_lock
            # and our own gen cannot be superseded while we hold it.
            self.deck_controller.clear_media_player_tasks()

            # Swap the pre-built background in under _background_load_lock,
            # matching load_page's lock order (_load_page_lock ->
            # _background_load_lock, never reversed) with a generation
            # re-check inside (plan §4 M3, C-F6).
            with self.deck_controller._background_load_lock:
                if self.deck_controller._page_is_current(gen):
                    self.deck_controller.background.apply_prebuilt(
                        kind, payload, fps=self.fps, loop=self.loop, update=True
                    )
                elif payload is not None and hasattr(payload, "close"):
                    # Superseded before we could apply it (shouldn't happen
                    # in practice -- gen was bumped by us, under the same
                    # lock hold, just above -- but close it defensively
                    # rather than leaking a cv2 capture handle).
                    payload.close()

            # Release keys
            for key in self.deck_controller.inputs[Input.Key]:
                key.down_start_time = None
                key.press_state = False

            # Capture the just-stashed input set for the release below,
            # still inside the lock so it can't be reassigned by a
            # coalesced concurrent show() first.
            stashed_inputs = self.original_inputs

        # mem-plan P2.6: the previous page's input set -- and whatever media
        # it was holding (key/dial videos, GIFs, images) -- sits pinned in
        # self.original_inputs for the screensaver's entire duration and is
        # then discarded uncleaned by hide() (`original_inputs.clear()`,
        # never a close/close_resources). That's the design doc's bug 8:
        # 50-150MB of stashed media memory idle behind the screensaver.
        # Release it now instead of waiting for hide().
        #
        # Deliberately NOT self.original_background: it aliases
        # self.deck_controller.background (the very same object, mutated in
        # place by apply_prebuilt()/set_video()/set_image() above) -- it is
        # the screensaver's OWN live background now, not a stashed copy of
        # the old one. Closing it here would close what's currently on
        # screen.
        #
        # Routed through the media player's CONTROL queue (a
        # ReleaseStashedInputsMsg), not closed inline and not a generic
        # add_task(): the lock above only guarantees `deck_controller.inputs`
        # itself was swapped to a fresh dict -- a tick begun just before
        # that swap can still be mid-render against the OLD input objects
        # (get_current_image()/get_raw_image() reading key_image/key_video),
        # so this must be serialized behind the writer, not run inline.
        # Control messages (unlike add_task's MediaPlayerTask) have no
        # active-page affinity check, so a hide()-triggered load_page()
        # landing before this drains can't cause it to be silently dropped
        # -- see ReleaseStashedInputsMsg's docstring in DeckController.py.
        if stashed_inputs:
            # Local import: DeckController imports ScreenSaver at module
            # level (for self.screen_saver = ScreenSaver(self)), so a
            # top-level import here would be circular.
            from src.backend.DeckManagement.DeckController import ReleaseStashedInputsMsg
            self.deck_controller.media_player.submit_control(
                ReleaseStashedInputsMsg(stashed_inputs)
            )

    def hide(self):
        """Serialized hide transition (docs/presenter-migration-plan.md §4 M3).

        Phase 2 (under _load_page_lock) does the coalesce/flip/restore; phase
        3 (load_page + set_time) runs AFTER the lock is released. This is the
        G-B1 fix: _load_page_lock is an RLock, so calling load_page from
        INSIDE this transition's hold would re-enter it and run
        initialize_actions/ChangePage -- deliberately kept outside
        load_page's own hold (DeckController.load_page, see the comment
        above its `page.initialize_actions()` call) -- under this
        transition's OUTER hold instead, re-arming the run_on_main/pulsectl
        deadlock this codebase already froze on once. follow-up work is
        returned as a closure and must only ever be invoked after the `with`
        block below has exited.
        """
        if getattr(self.deck_controller, "_closing", False):
            # The deck is tearing down (plan P1.3): hide()'s phase 3 calls
            # load_page(), which would resurrect a controller mid-close --
            # this is the exact bug the _closing gate exists to prevent.
            return
        log.info("Hiding screen saver")

        follow_up = None
        with self.deck_controller._load_page_lock:
            # Coalesce: a concurrent second hide() (e.g. on_key_change racing
            # set_enable(False) or LockScreenManager.unlock()) is a no-op
            # once we're already hidden.
            if not self.showing:
                return

            # Same atomic bump-only pattern as show() -- see its comment.
            with self.deck_controller._page_gen_lock:
                self.deck_controller._page_load_generation += 1

            self.original_inputs.clear()
            # Ensures that the first image visable is from the page not the
            # screensaver if the brightness on the saver is 0.
            self.deck_controller.clear()
            self.showing = False

            # A page change requested while the screensaver was showing sits in
            # the controller's pending slot (switching immediately would freeze
            # the screensaver video -- see load_page's guard): load it now,
            # falling back to whatever page is active. Consumed here under
            # _load_page_lock but loaded by follow_up after release (phase 3):
            # a page change landing in that gap is overwritten by this older
            # load -- the same window the plain active_page reload always had.
            pending = self.deck_controller.take_pending_screensaver_page()
            active_page = pending if pending is not None else self.deck_controller.active_page
            time_delay = self.time_delay
            follow_up = lambda: self._hide_followup(active_page, time_delay)

        # Phase 3 -- outside the lock. Never move this inside the `with`
        # block above (see the docstring).
        follow_up()

    def _hide_followup(self, active_page, time_delay) -> None:
        if active_page:
            self.deck_controller.load_page(active_page, allow_reload=True)
        else:
            self.deck_controller.load_default_page()
        self.set_time(time_delay)

    def on_key_change(self):
        if getattr(self.deck_controller, "_closing", False):
            # The deck is tearing down (plan P1.3): a straggling input event
            # (already in flight when step 3 stopped the reader thread) must
            # not re-arm the screensaver timer or trigger hide()'s
            # load_page().
            return
        self.last_key_change_time = time.time()
        if self.showing:
            self.hide()
        else:
            self.set_time(self.time_delay)

    def set_brightness(self, brightness: int) -> None:
        self.brightness = int(brightness)

        if self.showing:
            self.deck_controller.set_brightness(self.brightness)

    def set_fps(self, fps: int) -> None:
        self.fps = fps
        if not self.showing:
            return
        if self.deck_controller.background.video is not None:
            self.deck_controller.background.video.fps = fps

    def set_loop(self, loop: bool) -> None:
        self.loop = loop
        if not self.showing:
            return
        if self.deck_controller.background.video is not None:
            self.deck_controller.background.video.loop = loop