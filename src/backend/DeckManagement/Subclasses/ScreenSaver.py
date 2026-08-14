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

# Import typing
from typing import TYPE_CHECKING

import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend import timer_wheel
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.background_media import Background
    from src.backend.DeckManagement.deck_controller.controller import DeckController

class ScreenSaver:
    def __init__(self, deck_controller: "DeckController"):
        self.deck_controller: "DeckController" = deck_controller

        # original_inputs is the stashed deck_controller.inputs mapping (input
        # type to list of inputs), not a list. show() assigns the dict, hide()
        # and close() call .clear() and .values() on it, and close() compares
        # it against {}. A list is the wrong shape for a close() that runs
        # before any show().
        self.original_inputs: dict = {}
        self.original_background: "Background | None" = None
        self.original_brightness: int = 0

        # Time when last key state changed
        self.last_key_change_time = time.time()

        self.time_delay = 5

        self.enable: bool = False
        self.showing: bool = False

        self.media_path: str | None = None
        # The screensaver brightness default, the same number the deck-settings
        # schema carries. A config reaches here through a load. If no load
        # runs, the value must degrade to the documented default and not to a
        # fifth number nothing else knows.
        self.brightness: int = 30
        self.fps: int = 30
        self.loop: bool = True
        # Non-None only while armed, that is enabled and not showing. See
        # set_time and set_enable.
        self.timer: "timer_wheel.TimerHandle | None" = None
        # True once set_time() runs. DeckController's config load calls
        # set_enable() before set_time(), so set_enable(True) at that point
        # must do nothing rather than arm a timer against the not-yet-loaded
        # time_delay default. set_time() arms the timer.
        self._timer_initialized: bool = False

    def _arm_timer(self) -> None:
        # *60 converts minutes, the stored unit, into the seconds the timer
        # needs.
        self.timer = timer_wheel.schedule(self.time_delay * 60, self.on_timer_end, name="ScreenSaverTimer")

    def set_time(self, time_delay: int) -> None:
        time_delay = max(1, time_delay) # Minimum 1 minute. A smaller value shows the screensaver instantly and causes errors
        if time_delay != self.time_delay:
            log.info(f"Setting screen saver time delay to {time_delay} minutes")
        self.time_delay = time_delay
        self._timer_initialized = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.enable and not self.showing:
            self._arm_timer()

    def set_media_path(self, media_path: str | None) -> None:
        # None is the ordinary case. Every config without a chosen media says
        # None, and the background layer reads it as "blank".
        self.media_path = media_path

        if self.showing:
            self.deck_controller.background.set_from_path(self.media_path)

    def set_enable(self, enable: bool) -> None:
        self.enable = enable

        if not self._timer_initialized:
            return

        if self.showing and not enable:
            self.hide()

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
        """Serialized show transition (docs/presenter-migration-plan.md).

        Phase 1 pre-resolves the screensaver background outside any lock.
        Phase 2 installs it under _load_page_lock, and runs no plugin callback,
        no GTK marshaling and no file I/O. show() has no phase 3, because
        unlike hide() it never calls load_page.
        """
        if getattr(self.deck_controller, "_closing", False):
            # The deck is tearing down. A straggling timer fire that races
            # close() must not resurrect the screensaver's transient inputs or
            # background on a controller that is mid-sweep.
            return
        log.info("Showing screen saver")

        # Phase 1 runs outside any lock. A BackgroundVideo constructor hashes
        # the whole source file and opens a capture, which takes seconds, so it
        # must finish before this method takes _load_page_lock. Otherwise every
        # USB event, GTK and action-pool caller of show(), hide() and load_page
        # stalls for that time.
        kind, payload = self.deck_controller.background.prebuild_from_path(
            self.media_path, fps=self.fps, loop=self.loop
        )
        if kind == "noop":
            # A configured screensaver media file that is deleted, moved, or
            # carried on a config to another machine prebuilds as "noop", and
            # apply_prebuilt() returns early on "noop" without touching the
            # background. The underlying page's video capture then stays open
            # behind the showing screensaver and keeps decoding and
            # compositing at full rate for the whole duration, which is what
            # showing the screensaver must stop. Nothing renderable exists
            # here, so blank is both what the user sees and what releases the
            # old page's media.
            kind = "blank"

        with self.deck_controller._load_page_lock:
            # A concurrent second show() does nothing once the screensaver
            # already shows. That covers a manual show() that races the timer,
            # and two of the six requesters firing at once.
            if self.showing:
                if payload is not None and hasattr(payload, "close"):
                    payload.close()
                return

            # Bump the generation atomically, the same pattern as load_page.
            # This is a bump only, and active_page stays untouched, because
            # this is not a page switch. Post-transition frames then outrank
            # pre-transition stragglers (docs/presenter-migration-plan.md).
            with self.deck_controller._page_gen_lock:
                self.deck_controller._page_load_generation += 1
                gen = self.deck_controller._page_load_generation

            # Stop the timer, because a caller can invoke show() manually.
            if self.timer:
                self.timer.cancel()
            self.showing = True

            self.original_inputs = self.deck_controller.inputs
            # Do not pre-clear with inputs = {}. init_inputs builds then
            # swaps, so the concurrent media writer sees the old complete dict
            # or the new complete dict, never an empty or partial one.
            #
            # Key and dial gestures in flight die with the stash. After the
            # swap the physical release arrives on the replacement input set,
            # where the showing-screensaver guard swallows it, so a stashed
            # input's hold timer stays armed and fires HOLD_START into its
            # pinned down-time action snapshot mid-screensaver. Cancel the
            # gestures here, while a racing input event still reaches the
            # stashed inputs. This does bookkeeping only, with attribute
            # stores and timer cancels. The touchscreen keeps no gesture
            # state, because its events arrive pre-classified and single-shot.
            for key in self.original_inputs.get(Input.Key, []):
                key.cancel_gesture()
            for dial in self.original_inputs.get(Input.Dial, []):
                dial.cancel_gesture()
            self.deck_controller.init_inputs()

            self.original_background = self.deck_controller.background
            self.original_brightness = self.deck_controller.brightness

            self.deck_controller.set_brightness(self.brightness)

            # expects_repaint is True, because the paints that install the
            # screensaver background follow immediately below. These blanks
            # are a transition and not the intended end state.
            self.deck_controller.clear(expects_repaint=True)
            # The seq-stamped ClearMsg just submitted wipes the image and
            # touchscreen slots. It does not touch the generic tasks list,
            # e.g. a straggling load_all_inputs or
            # _update_all_inputs_awaiting_background from a load_page in
            # flight. clear_media_player_tasks() owns that generic wipe. It
            # takes no gen argument, because this code holds _load_page_lock
            # and nothing can supersede its own gen during the hold.
            self.deck_controller.clear_media_player_tasks()

            # Swap the pre-built background in under _background_load_lock.
            # The lock order matches load_page, which takes _load_page_lock
            # first and then _background_load_lock, never the reverse. The
            # generation re-check inside stops a straggling load_background()
            # worker, already blocked on this same lock from an older
            # load_page, from overwriting the screensaver background after
            # this method releases.
            with self.deck_controller._background_load_lock:
                if self.deck_controller._page_is_current(gen):
                    self.deck_controller.background.apply_prebuilt(
                        kind, payload, fps=self.fps, loop=self.loop, update=True
                    )
                elif payload is not None and hasattr(payload, "close"):
                    # Superseded before the apply. This code bumped gen under
                    # the same lock hold just above, so this branch is not
                    # expected. Close the payload here rather than leak a cv2
                    # capture handle.
                    payload.close()

            # Release keys
            for key in self.deck_controller.inputs[Input.Key]:
                key.down_start_time = None
                key.press_state = False

            # Capture the just-stashed input set for the release below, still
            # inside the lock, so a coalesced concurrent show() cannot
            # reassign it first.
            stashed_inputs = self.original_inputs

        # The previous page's input set, and the media it holds (key and dial
        # videos, GIFs, images), stays pinned in self.original_inputs for the
        # whole screensaver duration. hide() then discards it with
        # original_inputs.clear() and never closes it, which idles 50-150 MB
        # of stashed media memory behind the screensaver. Release it here.
        #
        # Do not release self.original_background. It aliases
        # self.deck_controller.background, the same object that
        # apply_prebuilt(), set_video() and set_image() mutate in place above.
        # It is the screensaver's own live background, not a stashed copy of
        # the old one, and closing it here closes what is on screen.
        #
        # Route the release through the media player control queue as a
        # ReleaseStashedInputsMsg. Do not close it inline and do not use a
        # generic add_task(). The lock above only guarantees that
        # deck_controller.inputs points at a fresh dict. A tick that began
        # just before that swap can still render against the old input
        # objects, through get_current_image() and get_raw_image() reading
        # key_image and key_video, so the writer must serialize this. A
        # control message carries no active-page affinity check, unlike
        # add_task's MediaPlayerTask, so a load_page() from hide() that lands
        # before this drains cannot drop it. See ReleaseStashedInputsMsg's
        # docstring in DeckManagement/deck_controller/media_writer.py.
        if stashed_inputs:
            # Import at call time. The deck controller package imports this
            # module at module level, because each controller builds a
            # ScreenSaver. A lazy dependency on that package here keeps the
            # edge between the two one-directional.
            from src.backend.DeckManagement.deck_controller.media_writer import (
                ReleaseStashedInputsMsg,
            )
            self.deck_controller.media_player.submit_control(
                ReleaseStashedInputsMsg(stashed_inputs)
            )

    def hide(self):
        """Serialized hide transition (docs/presenter-migration-plan.md).

        Phase 2 runs under _load_page_lock and does the coalesce, the flip and
        the restore. Phase 3 runs load_page and set_time after the release,
        through a closure invoked below the with block.
        """
        if getattr(self.deck_controller, "_closing", False):
            # The deck is tearing down. hide()'s phase 3 calls load_page(),
            # which resurrects a controller mid-close. The _closing gate
            # exists to stop that.
            return
        log.info("Hiding screen saver")

        follow_up = None
        with self.deck_controller._load_page_lock:
            # A concurrent second hide() does nothing once the screensaver is
            # already hidden. That covers on_key_change racing set_enable(False)
            # and LockScreenManager.unlock().
            if not self.showing:
                return

            # Same atomic bump-only pattern as show(). See its comment.
            with self.deck_controller._page_gen_lock:
                self.deck_controller._page_load_generation += 1

            self.original_inputs.clear()
            # The first visible image must come from the page and not from the
            # screensaver when the saver brightness is 0. expects_repaint is
            # True, because phase 3's load_page repaints the restored page.
            # These blanks are a transition too.
            self.deck_controller.clear(expects_repaint=True)
            self.showing = False

            # A page change requested while the screensaver showed sits in the
            # controller's pending slot, because an immediate switch freezes
            # the screensaver video (see load_page's guard). Load it now, and
            # fall back to the active page. This code takes the pending page
            # under _load_page_lock, but follow_up loads it after the release
            # in phase 3. A page change that lands in that gap loses to this
            # older load, which is the same window the plain active_page
            # reload always has.
            pending = self.deck_controller.take_pending_screensaver_page()
            # Taking the page ends its only protection. From here until phase
            # 3 installs it, the page is neither pending nor active, so cache
            # pressure can tear it down and hand the load a corpse. Reserve it
            # for that gap. Installing it releases the reservation.
            if pending is not None and gl.page_manager is not None:
                gl.page_manager.pins.reserve_fetch(pending, self.deck_controller)
            active_page = pending if pending is not None else self.deck_controller.active_page
            time_delay = self.time_delay
            follow_up = lambda: self._hide_followup(active_page, time_delay)

        # Phase 3 runs outside the lock. Never move this inside the with block
        # above. _load_page_lock is an RLock, so a load_page call from inside
        # the hold re-enters it and runs initialize_actions and ChangePage
        # under the outer hold. load_page keeps those two outside its own hold,
        # because that combination arms a run_on_main and pulsectl deadlock.
        follow_up()

    def _hide_followup(self, active_page, time_delay) -> None:
        if active_page:
            self.deck_controller.load_page(active_page, allow_reload=True)
        else:
            self.deck_controller.load_default_page()
        self.set_time(time_delay)

    def on_key_change(self):
        if getattr(self.deck_controller, "_closing", False):
            # The deck is tearing down. A straggling input event, already in
            # flight when the reader thread stopped, must not re-arm the
            # screensaver timer or trigger hide()'s load_page().
            return
        self.last_key_change_time = time.time()
        # Deck presses never reach the compositor, so this funnel is the only
        # thing that can tell the presence monitor a user is drumming on the
        # deck. Every key, dial and touch interaction passes through it. The
        # None guard is necessary, because the unit-tier harness installs no
        # presence monitor.
        if gl.presence_monitor is not None:
            gl.presence_monitor.notify_activity()
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