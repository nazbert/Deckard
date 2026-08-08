"""
Quiescence / presence signal (issue #144).

One process-wide object (`gl.presence_monitor`) that answers a single
question for every deck's media loop: *is the user away?* When it says yes,
`DeckController.animations_gated()` turns true and the media loop skips its
whole animation section -- no background-video decode, no key/dial/
touchscreen tick, no scroll-label advance -- while the control queue and
queued interactive paints keep running at full speed.

Inputs, all event-driven (no polling anywhere):

* **screen lock** -- `LockScreenManager.lock()` publishes `gl.screen_locked`
  unconditionally (before its `lock_on_lock_screen` early-return) and calls
  `on_lock_changed()` right after, so lock is a usable presence input even
  for users who deliberately keep their decks live on lock.
* **system idle** -- logind's `IdleHint`/`IdleSinceHint` on the session
  object, plus a configurable residual delay counted from `IdleSinceHint`
  (the detector that feeds `on_idle_hint_changed()` lands separately).
* **deck activity** -- `notify_activity()` from `ScreenSaver.on_key_change()`,
  the funnel every key/dial/touch interaction already passes through. Deck
  presses never reach the compositor, so without this the session would
  stay "idle" while the user is actively drumming on the deck.

The rule (evaluated on every input change):

    quiescent := mode == "system-idle" and (
        screen_locked
        or (idle_hint and now - max(idle_since, last_deck_activity) >= minutes*60)
    )

Mode `"screensaver"` -- the default -- makes this object report `False`
forever, which is bit-for-bit today's behavior: the deck screensaver's own
transition already releases the underlying page's media, so there is nothing
extra to gate for users who have not opted in.

Threading: `is_quiescent()` is a bare attribute read -- GIL-atomic, lock
free, safe to call from the media thread's critical path 30 times a second.
Transitions (rare) hold `self._lock`; the wake fan-out runs *outside* it.
Inputs arrive on three different threads (the GLib default main context for
Gio callbacks, timer_wheel dispatch threads for the idle deadline, deck
reader threads for `notify_activity`) and no GTK call is made anywhere in
this module.
"""
import threading
import time

from loguru import logger as log

import globals as gl
from src.backend import timer_wheel


# The two `performance.animation-pause-mode` values. "screensaver" is the
# conservative default: nothing new engages.
MODE_SCREENSAVER = "screensaver"
MODE_SYSTEM_IDLE = "system-idle"

# Mirrors the SettingsManager DEFAULTS entries; used when settings are not
# reachable yet (early startup, unit-tier harness).
FALLBACK_MODE = MODE_SCREENSAVER
FALLBACK_IDLE_MINUTES = 5


def _settings_seed() -> tuple[str, int]:
    """Reads the persisted mode/minutes. Falls back to the conservative
    defaults if settings are not reachable (the monitor is constructed early
    in startup, and the unit-tier harness installs a stub settings manager)
    -- an unreadable setting must never leave gating silently *on*."""
    try:
        app = gl.settings_manager.app()
        return str(app.animation_pause_mode), int(app.animation_idle_minutes)
    except Exception:
        log.debug("PresenceMonitor: falling back to default pause mode "
                  "(app settings not readable yet)")
        return FALLBACK_MODE, FALLBACK_IDLE_MINUTES


class PresenceMonitor:
    """The quiescence signal. See the module docstring for the rule."""

    def __init__(self, mode: str = None, minutes: int = None):
        # Hot read (media thread, every tick): a plain bool attribute, never
        # guarded by the lock below. A torn read is impossible and a stale
        # one costs a single tick of animation either way.
        self.quiescent: bool = False

        self._lock = threading.Lock()

        seed_mode, seed_minutes = _settings_seed()
        self._mode: str = mode if mode is not None else seed_mode
        self._minutes: int = max(1, int(minutes if minutes is not None else seed_minutes))

        self._idle_hint: bool = False
        # Wall clock (time.time() domain, same as logind's IdleSinceHint,
        # which is CLOCK_REALTIME microseconds).
        self._idle_since: float = None
        # 0.0 == "no deck input observed yet". Deliberately NOT time.time():
        # process start is not deck activity, and seeding it with `now` would
        # postpone the first gate by the full residual delay after every
        # restart -- including a restart into a session that logind already
        # reports as hours idle.
        self._last_deck_activity: float = 0.0
        self._deadline: "timer_wheel.TimerHandle" = None

        # Evaluate once at construction: without this, `system-idle` mode
        # would be silently off after every restart until the first lock or
        # idle transition happened to arrive (and a restart while already
        # locked would never gate at all).
        self._evaluate()

    # -- reads ----------------------------------------------------------

    def is_quiescent(self) -> bool:
        """The media loop's question. Lock-free by contract."""
        return self.quiescent

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def idle_minutes(self) -> int:
        return self._minutes

    # -- inputs ---------------------------------------------------------

    def on_lock_changed(self, active: bool) -> None:
        """Called from `LockScreenManager.lock()` immediately after it
        publishes `gl.screen_locked`. `active` is that same value and is
        logged only -- the evaluation re-reads `gl.screen_locked` so this
        object and the rest of the app can never disagree about lock state
        (the constructor's seeding evaluation has no argument to read)."""
        log.debug(f"PresenceMonitor: screen lock -> {active}")
        self._evaluate()

    def on_idle_hint_changed(self, idle_hint: bool, idle_since: float = None) -> None:
        """logind session `IdleHint` flipped. `idle_since` is the wall-clock
        time the session went idle (from `IdleSinceHint`); None means "as of
        now", which is what the arithmetic assumes when logind reports no
        usable timestamp."""
        with self._lock:
            self._idle_hint = bool(idle_hint)
            self._idle_since = idle_since if idle_hint else None
        self._evaluate()

    def notify_activity(self) -> None:
        """A deck input happened. Deck presses are invisible to the
        compositor, so this is the only thing that can clear an idle hint
        for a user who is present *at the deck*."""
        self._last_deck_activity = time.time()
        # Fast path for the default mode: nothing can be gated, so an input
        # never needs to touch the lock or the timer wheel. `_mode` is
        # rebound atomically by set_mode(), which re-evaluates itself.
        if self._mode != MODE_SYSTEM_IDLE:
            return
        self._evaluate()

    def set_mode(self, mode: str, minutes: int = None) -> None:
        """Runtime push from the Settings dialog."""
        with self._lock:
            self._mode = mode if mode in (MODE_SCREENSAVER, MODE_SYSTEM_IDLE) else MODE_SCREENSAVER
            if minutes is not None:
                self._minutes = max(1, int(minutes))
        self._evaluate()

    def stop(self) -> None:
        """Releases the idle deadline. Nothing in the app calls this (the
        monitor lives for the process); it exists so scenarios can leave no
        timers behind."""
        with self._lock:
            self._cancel_deadline_locked()

    # -- evaluation -----------------------------------------------------

    def _cancel_deadline_locked(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None

    def _on_deadline(self) -> None:
        """The residual idle delay elapsed (timer_wheel dispatch thread)."""
        self._evaluate()

    def _evaluate(self) -> None:
        """Recomputes `quiescent` from the current inputs, (re-)arming the
        residual idle deadline as needed. Safe to call from any thread and
        as often as inputs arrive."""
        with self._lock:
            was = self.quiescent
            now = time.time()
            quiescent = False
            rearm_in = None

            if self._mode == MODE_SYSTEM_IDLE:
                if bool(getattr(gl, "screen_locked", False)):
                    # The strongest away signal there is, and the one that
                    # works without any idle agent. Deliberately independent
                    # of `lock-on-lock-screen`: that setting decides whether
                    # the deck shows its screensaver on lock, not whether the
                    # user is at the machine.
                    quiescent = True
                elif self._idle_hint:
                    since = self._idle_since if self._idle_since is not None else now
                    remaining = (max(since, self._last_deck_activity)
                                 + self._minutes * 60) - now
                    if remaining <= 0:
                        quiescent = True
                    else:
                        rearm_in = remaining

            self._cancel_deadline_locked()
            if rearm_in is not None:
                self._deadline = timer_wheel.schedule(
                    rearm_in, self._on_deadline, name="PresenceIdleDeadline"
                )

            self.quiescent = quiescent
            changed = quiescent != was

        if changed:
            log.info(f"Presence: deck animations {'gated' if quiescent else 'live'}")
            # Outside the lock on purpose: the fan-out touches every
            # controller's media thread, and nothing about that iteration
            # needs this module's state held.
            self._wake_media_threads()

    def _wake_media_threads(self) -> None:
        """Cuts short every media loop's inter-tick wait so a presence
        transition takes effect on the next tick instead of after up to half
        a second of gated cadence."""
        deck_manager = getattr(gl, "deck_manager", None)
        if deck_manager is None:
            return
        # Snapshot: remove_controller() mutates this list from unplug/close
        # threads while we iterate.
        for controller in list(getattr(deck_manager, "deck_controller", None) or []):
            try:
                controller.media_player.wake()
            except Exception:
                log.opt(exception=True).warning(
                    "PresenceMonitor: failed to wake a deck's media thread"
                )
