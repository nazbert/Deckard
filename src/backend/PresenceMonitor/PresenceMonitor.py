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
  object (see `LogindIdleDetector` below), plus a configurable residual
  delay counted from `IdleSinceHint`.
* **deck activity** -- `notify_activity()` from `ScreenSaver.on_key_change()`,
  the funnel every key/dial/touch interaction already passes through. Deck
  presses never reach the compositor, so without this the session would
  stay "idle" while the user is actively drumming on the deck.

The rule (evaluated on every input change):

    quiescent := mode == "system-idle" and (
        (screen_locked and now - last_deck_activity >= DECK_ACTIVITY_GRACE_S)
        or (idle_hint and now - max(idle_since, last_deck_activity) >= minutes*60)
    )

Deck activity outranks the lock for a short grace on purpose: with
`lock-on-lock-screen` off the deck stays live and usable while the screen is
locked, and a user drumming on it is present at it whatever the monitor says.

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
import os
import threading
import time
from typing import Any

from loguru import logger as log

import globals as gl
from src.backend import timer_wheel

from gi.repository import Gio, GLib


# The two `performance.animation-pause-mode` values. "screensaver" is the
# conservative default: nothing new engages.
MODE_SCREENSAVER = "screensaver"
MODE_SYSTEM_IDLE = "system-idle"

# Mirrors the SettingsManager DEFAULTS entries; used when settings are not
# reachable yet (early startup, unit-tier harness).
FALLBACK_MODE = MODE_SCREENSAVER
FALLBACK_IDLE_MINUTES = 5

LOGIND_BUS_NAME = "org.freedesktop.login1"
LOGIND_MANAGER_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIND_SESSION_IFACE = "org.freedesktop.login1.Session"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


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

    # How long a deck press keeps this deck live even while the screen is
    # locked. The lock is the strongest away signal there is -- but it is a
    # signal about the MONITOR, and `lock-on-lock-screen` off means the deck
    # deliberately stays live and usable while the screen is locked. Without
    # this grace such a user drums on a working deck whose animations are
    # frozen and nothing they do un-freezes them, because the lock term
    # short-circuits before any activity is considered. Long enough that
    # ordinary use never trips the gate, short enough that walking away from a
    # locked screen still saves the CPU within the minute. Class-level so the
    # harness can tighten it.
    DECK_ACTIVITY_GRACE_S = 30.0

    def __init__(self, mode: str = None, minutes: int = None,
                 idle_detector: bool = True, bus=None):
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
        self._idle_since: float | None = None
        # 0.0 == "no deck input observed yet". Deliberately NOT time.time():
        # process start is not deck activity, and seeding it with `now` would
        # postpone the first gate by the full residual delay after every
        # restart -- including a restart into a session that logind already
        # reports as hours idle.
        self._last_deck_activity: float = 0.0
        self._deadline: "timer_wheel.TimerHandle | None" = None

        # The logind detector is built ON DEMAND, only for the mode that
        # reads it: in the default pause mode it would open a system-bus
        # connection (on a startup thread), resolve the session and hold a
        # PropertiesChanged subscription for every user who never opted in --
        # surface and cost for a signal nothing consumes. Deferring is
        # lossless because the detector seeds the monitor from the session's
        # CURRENT IdleHint whenever it is built (setup_dbus ->
        # read_initial_state), not only at process start.
        #
        # `idle_detector` is the harness's opt-out and `bus` the detector's
        # test seam; both are captured here for the deferred build. Built
        # before the seeding evaluation below so an already-idle session is
        # reflected in the very first verdict.
        self.idle_detector: "LogindIdleDetector | None" = None
        self._idle_detector_enabled: bool = bool(idle_detector)
        self._idle_detector_bus = bus
        self._detector_lock = threading.Lock()
        if self._mode == MODE_SYSTEM_IDLE:
            self._ensure_idle_detector()

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
        if not active:
            # An unlock is a person at the machine, and it is the only signal
            # that says so on a session whose idle agent sets `IdleHint` but
            # never clears it (no resume command configured -- swayidle's
            # `idlehint` without a matching `resume`). Without this the idle
            # term would still be measuring from a STALE IdleSinceHint minutes
            # in the past, so the deck would stay frozen straight through the
            # unlock, and stay frozen until the next deck press.
            self._last_deck_activity = time.time()
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
        compositor AND to the lock state, so this is the only thing that can
        speak for a user who is present *at the deck*: it clears an idle hint,
        and it outranks a locked screen for DECK_ACTIVITY_GRACE_S."""
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
            mode_now = self._mode
        if mode_now == MODE_SYSTEM_IDLE:
            # The opt-in is where the detector gets built (see __init__).
            # Outside the lock: with an injected bus the build wires up inline
            # and calls straight back into _evaluate(). Switching BACK to the
            # default mode deliberately keeps the detector -- the rule already
            # ignores it there, and tearing it down and up again on every
            # toggle would churn the bus for nothing.
            self._ensure_idle_detector()
        self._evaluate()

    def _ensure_idle_detector(self) -> None:
        """Builds the logind idle detector once, if it is wanted at all.
        Idempotent and safe from any thread. Never call it holding
        `self._lock` -- an injected bus makes the build call back into
        `_evaluate()`, which takes it."""
        if not self._idle_detector_enabled:
            return
        with self._detector_lock:
            if self.idle_detector is not None:
                return
            self.idle_detector = LogindIdleDetector(self, bus=self._idle_detector_bus)

    def stop(self) -> None:
        """Releases the idle deadline and the D-Bus subscription. Nothing in
        the app calls this (the monitor lives for the process); it exists so
        scenarios can leave no timers behind."""
        with self._lock:
            self._cancel_deadline_locked()
        if self.idle_detector is not None:
            self.idle_detector.stop()

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
        deadline that will make the verdict change on its own -- the residual
        idle delay, or the deck-activity grace under a locked screen. Safe to
        call from any thread and as often as inputs arrive."""
        with self._lock:
            was = self.quiescent
            now = time.time()
            quiescent = False
            rearm_in = None

            if self._mode == MODE_SYSTEM_IDLE:
                since_activity = now - self._last_deck_activity
                if bool(getattr(gl, "screen_locked", False)):
                    # The strongest away signal there is, and the one that
                    # works without any idle agent. Deliberately independent
                    # of `lock-on-lock-screen`: that setting decides whether
                    # the deck shows its screensaver on lock, not whether the
                    # user is at the machine.
                    #
                    # It still yields to a RECENT deck press for
                    # DECK_ACTIVITY_GRACE_S: a locked screen over a live deck
                    # (lock-on-lock-screen off) is a supported configuration,
                    # and the person pressing its keys is at it. The seed
                    # `_last_deck_activity = 0.0` means "no press observed",
                    # which lands here as an elapsed grace -- so a process
                    # that starts into an already-locked session still gates
                    # immediately.
                    if since_activity >= self.DECK_ACTIVITY_GRACE_S:
                        quiescent = True
                    else:
                        # Nothing else will call back: the lock state is not
                        # changing and logind never sees deck presses. Arm the
                        # grace's own expiry or the gate would stay off until
                        # the next unrelated input.
                        rearm_in = self.DECK_ACTIVITY_GRACE_S - since_activity
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


class LogindIdleDetector:
    """Feeds `PresenceMonitor.on_idle_hint_changed()` from logind's session
    `IdleHint`/`IdleSinceHint` properties.

    Deliberately shaped like `LockScreenManager/Detectors/Logind.py` (issue
    #195) -- same system-bus `Gio` connection, same `resolve_session_path()`
    (`GetSession($XDG_SESSION_ID)` with a `GetSessionByPID` fallback), same
    `bus=` test seam, same inert-on-`GLib.Error` posture -- so the two can be
    deduplicated onto one shared resolver once both have landed.

    `IdleHint` is a hint the *desktop environment* maintains: GNOME and KDE
    set it from their own idle policy; Niri/Sway/river need a user-side agent
    (`swayidle idlehint N`). Where nothing sets it, this component stays
    permanently false and the monitor degrades to lock-only gating.
    """

    def __init__(self, monitor: PresenceMonitor, bus=None):
        self.monitor = monitor
        self.bus: Gio.DBusConnection | None = None
        self.session_path: str | None = None
        self._subscription_id: int | None = None
        if bus is not None:
            # An injected bus is an in-process double: there is no I/O to get
            # stuck on, so wire up inline and keep scenarios deterministic.
            self.setup_dbus(bus)
        else:
            # The real system bus goes on a daemon thread, exactly like
            # LockScreenManager.__init__ ("in case something gets stuck"):
            # this object is constructed on main()'s startup path, and
            # Gio.bus_get_sync + a synchronous GetSession round trip would
            # otherwise hold app startup hostage to a wedged logind for the
            # call's full timeout.
            threading.Thread(target=self.setup_dbus, name="PresenceIdleSetup",
                             daemon=True).start()

    def setup_dbus(self, bus=None) -> None:
        if gl.IS_MAC:
            return
        try:
            # logind lives on the system bus. `bus` is a test seam; production
            # passes None. Compared against None rather than truthiness so a
            # falsy-but-valid double cannot silently pull in the real bus.
            self.bus = bus if bus is not None else Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.session_path = self.resolve_session_path()

            # This runs on a plain daemon thread, which has no thread-default
            # main context, so GDBus dispatches this callback on the global
            # default one -- the GTK main loop, same as every lock detector.
            self._subscription_id = self.bus.signal_subscribe(
                LOGIND_BUS_NAME,
                PROPERTIES_IFACE,
                "PropertiesChanged",
                self.session_path,
                LOGIND_SESSION_IFACE,
                Gio.DBusSignalFlags.NONE,
                self.on_properties_changed,
            )

            self.read_initial_state()
        except GLib.Error as e:
            # House posture for detectors: report once at info and stay
            # inert. The lock input keeps working; only the idle half of the
            # rule goes dark.
            log.info(f"Presence: logind IdleHint unavailable, idle gating inert ({e})")
        except Exception:
            log.opt(exception=True).warning(
                "Presence: unexpected failure wiring up the logind idle detector; "
                "idle gating inert"
            )

    def resolve_session_path(self) -> str:
        bus = self.bus
        if bus is None:
            # Unreachable in practice: setup_dbus assigns self.bus immediately
            # before calling this. Raised as GLib.Error so setup_dbus's
            # "unavailable, stay inert" branch handles it, which is what an
            # unusable connection means.
            raise GLib.Error("logind system bus unavailable")

        session_id = os.getenv("XDG_SESSION_ID")
        if session_id:
            method = "GetSession"
            args = GLib.Variant("(s)", (session_id,))
        else:
            # PID 0 means "resolve the CALLER, from its bus credentials".
            # os.getpid() would be a sandbox-namespace number under flatpak,
            # which the host's logind reads as a host PID -- either unknown or,
            # worse, some unrelated process's session. Verified against a live
            # logind (systemd 261): pid 0 takes the caller-credentials branch,
            # answering "Caller does not belong to any known session" where a
            # numeric pid answers "PID <n> does not belong to any known
            # session"; it is not rejected as an invalid argument. Same shape
            # the #195 logind lock detector wants, for the shared resolver the
            # two are meant to be deduplicated onto.
            method = "GetSessionByPID"
            args = GLib.Variant("(u)", (0,))

        reply = bus.call_sync(
            LOGIND_BUS_NAME,
            LOGIND_MANAGER_PATH,
            LOGIND_MANAGER_IFACE,
            method,
            args,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return reply.unpack()[0]

    def read_property(self, name: str) -> Any:
        bus = self.bus
        session_path = self.session_path
        if bus is None or session_path is None:
            # setup_dbus never completed (or never ran): there is nothing to
            # read from. GLib.Error is the "logind unavailable" channel both
            # callers already handle.
            raise GLib.Error("logind session properties unavailable")

        reply = bus.call_sync(
            LOGIND_BUS_NAME,
            session_path,
            PROPERTIES_IFACE,
            "Get",
            GLib.Variant("(ss)", (LOGIND_SESSION_IFACE, name)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return reply.unpack()[0]

    def read_initial_state(self) -> None:
        """Seeds the monitor from the session's current properties: a
        process started (or restarted) into an already-idle session must gate
        without waiting for the next PropertiesChanged, which may never
        come."""
        hint = bool(self.read_property("IdleHint"))
        self.monitor.on_idle_hint_changed(hint, self._read_idle_since() if hint else None)

    def _read_idle_since(self, changed: dict = None):
        """`IdleSinceHint` as wall-clock seconds, or None when logind has no
        usable timestamp (it reports 0 when the session was never idle).
        Prefers the value carried by the signal; falls back to a property
        read, since logind does not always emit it alongside IdleHint."""
        raw = None
        if changed is not None:
            raw = changed.get("IdleSinceHint")
        if raw is None:
            try:
                raw = self.read_property("IdleSinceHint")
            except GLib.Error:
                return None
        try:
            usec = int(raw)
        except (TypeError, ValueError):
            return None
        return usec / 1_000_000 if usec > 0 else None

    def on_properties_changed(self, connection, sender_name, object_path,
                              interface_name, signal_name, parameters) -> None:
        try:
            iface, changed, _invalidated = parameters.unpack()
            if iface != LOGIND_SESSION_IFACE or "IdleHint" not in changed:
                return
            hint = bool(changed["IdleHint"])
            self.monitor.on_idle_hint_changed(
                hint, self._read_idle_since(changed) if hint else None
            )
        except Exception:
            # This runs on the GTK main context; an escaping exception there
            # is swallowed by GLib with no useful attribution.
            log.opt(exception=True).warning(
                "Presence: failed to handle a logind PropertiesChanged signal"
            )

    def stop(self) -> None:
        if self.bus is not None and self._subscription_id is not None:
            try:
                self.bus.signal_unsubscribe(self._subscription_id)
            except Exception:
                log.opt(exception=True).debug(
                    "Presence: failed to unsubscribe the logind idle signal"
                )
        self._subscription_id = None
