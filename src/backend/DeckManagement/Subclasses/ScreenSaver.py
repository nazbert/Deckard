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
import threading
from loguru import logger as log
from copy import copy

# Import typing
from typing import TYPE_CHECKING

from src.backend.DeckManagement.InputIdentifier import Input
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
        self.timer: threading.Timer = None

    def set_time(self, time_delay: int) -> None:
        time_delay = max(1, time_delay) # Min 1 minute - too small values leading to instant load if the screensaver lead to errors
        if time_delay != self.time_delay:
            log.info(f"Setting screen saver time delay to {time_delay} minutes")
        self.time_delay = time_delay
        if self.timer:
            self.timer.cancel()
        # *60 to go from minuts (how it is stored) to seconds (how the timer needs it)
        self.timer = threading.Timer(time_delay*60, self.on_timer_end)
        self.timer.setDaemon(True)
        self.timer.setName("ScreenSaverTimer")
        if self.enable and not self.showing:
            self.timer.start()

    def set_media_path(self, media_path: str) -> None:
        self.media_path = media_path

        if self.showing:
            self.deck_controller.background.set_from_path(self.media_path)

    def set_enable(self, enable: bool) -> None:
        self.enable = enable

        if not self.timer:
            return
        
        # Hide if showing
        if self.showing and not enable:
            self.hide()
        
        # Stop timer if enable == False
        if enable:
            # A threading.Timer can't be restarted once fired/cancelled, so recreate it.
            if not self.timer.is_alive() and not self.showing:
                self.timer = threading.Timer(self.time_delay * 60, self.on_timer_end)
                self.timer.setDaemon(True)
                self.timer.setName("ScreenSaverTimer")
                self.timer.start()
        else:
            self.timer.cancel()

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
            self.deck_controller.inputs = {}
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

            active_page = self.deck_controller.active_page
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