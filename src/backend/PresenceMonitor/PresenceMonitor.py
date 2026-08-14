"""Quiescence and presence signal.

One process-wide object (gl.presence_monitor) answers one question for every
deck's media loop. Is the user away? When it answers yes,
DeckController.animations_gated() turns true and the media loop skips its
whole animation section. No background-video decode, no key, dial or
touchscreen tick, and no scroll-label advance. The control queue and the
queued interactive paints keep running at full speed.

Three inputs drive it, and every one is event-driven. Nothing polls.

The screen lock. LockScreenManager.lock() publishes gl.screen_locked before
its lock_on_lock_screen early return, and calls on_lock_changed() right
after, so the lock stays a usable presence input for a user who keeps the
decks live on lock.

The system idle state. logind holds IdleHint and IdleSinceHint on the session
object (see LogindIdleDetector below). A configurable idle delay counts from
IdleSinceHint.

Deck activity. notify_activity() comes from ScreenSaver.on_key_change(), the
funnel that every key, dial and touch interaction passes. A deck press never
reaches the compositor, so without this input the session stays idle while
the user drums on the deck.

Every input change evaluates this rule.

    quiescent := mode == "system-idle" and (
        (screen_locked and now - last_deck_activity >= DECK_ACTIVITY_GRACE_S)
        or (idle_hint and now - max(idle_since, last_deck_activity) >= minutes*60)
    )

Deck activity outranks the lock for a short grace. With lock-on-lock-screen
off the deck stays live and usable while the screen is locked, and a user who
drums on it is present at it whatever the monitor reports.

The default mode "screensaver" makes this object report False forever, which
matches the behaviour of an app without this monitor. The deck screensaver's
own transition already releases the underlying page's media, so nothing more
needs a gate for a user who does not opt in.

Threading. is_quiescent() reads one attribute. The read is GIL-atomic and
lock-free, and the media thread's critical path calls it 30 times a second. A
transition is rare and holds self._lock, and the wake fan-out runs outside
that lock. Inputs arrive on three threads: the GLib default main context for
Gio callbacks, timer_wheel dispatch threads for the idle deadline, and deck
reader threads for notify_activity. This module makes no GTK call.
"""
import os
import threading
import time
from typing import Any

from loguru import logger as log

import globals as gl
from src.backend import timer_wheel

from gi.repository import Gio, GLib


# The two performance.animation-pause-mode values. "screensaver" is the
# careful default, under which nothing new engages.
MODE_SCREENSAVER = "screensaver"
MODE_SYSTEM_IDLE = "system-idle"

# Mirrors the SettingsManager DEFAULTS entries. The monitor reads these while
# the settings are still unreachable, during early startup and in the
# unit-tier harness.
FALLBACK_MODE = MODE_SCREENSAVER
FALLBACK_IDLE_MINUTES = 5

LOGIND_BUS_NAME = "org.freedesktop.login1"
LOGIND_MANAGER_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIND_SESSION_IFACE = "org.freedesktop.login1.Session"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


def _settings_seed() -> tuple[str, int]:
    """Reads the persisted mode and minutes. Falls back to the careful
    defaults while the settings stay unreachable. Startup constructs the
    monitor early, and the unit-tier harness installs a stub settings
    manager. An unreadable setting must never leave the gate on."""
    try:
        app = gl.settings_manager.app()
        return str(app.animation_pause_mode), int(app.animation_idle_minutes)
    except Exception:
        log.debug("PresenceMonitor: falling back to default pause mode "
                  "(app settings not readable yet)")
        return FALLBACK_MODE, FALLBACK_IDLE_MINUTES


class PresenceMonitor:
    """The quiescence signal. See the module docstring for the rule."""

    # How long a deck press keeps this deck live while the screen is locked.
    # The lock is the strongest away signal, but it describes the monitor
    # only. With lock-on-lock-screen off the deck stays live and usable while
    # the screen is locked. Without this grace such a user drums on a working
    # deck whose animations stay frozen, because the lock term wins before the
    # rule reads any activity. This value is long enough that ordinary use
    # never trips the gate, and short enough that a walk away from a locked
    # screen saves the CPU within the minute. It sits on the class so the
    # harness can shorten it.
    DECK_ACTIVITY_GRACE_S = 30.0

    def __init__(self, mode: str = None, minutes: int = None,
                 idle_detector: bool = True, bus=None):
        # The media thread reads this plain bool every tick, and the lock
        # below never guards it. A torn read cannot happen, and a stale read
        # costs one tick of animation.
        self.quiescent: bool = False

        self._lock = threading.Lock()

        seed_mode, seed_minutes = _settings_seed()
        self._mode: str = mode if mode is not None else seed_mode
        self._minutes: int = max(1, int(minutes if minutes is not None else seed_minutes))

        self._idle_hint: bool = False
        # Wall clock (time.time() domain, same as logind's IdleSinceHint,
        # which is CLOCK_REALTIME microseconds).
        self._idle_since: float | None = None
        # 0.0 means "no deck input yet". Never seed this with time.time().
        # Process start is not deck activity, and a seed of now postpones the
        # first gate by the full idle delay after every restart, including a
        # restart into a session that logind already reports as hours idle.
        self._last_deck_activity: float = 0.0
        self._deadline: "timer_wheel.TimerHandle | None" = None

        # Build the logind detector on demand, for the mode that reads it. In
        # the default pause mode it would open a system-bus connection on a
        # startup thread, resolve the session, and hold a PropertiesChanged
        # subscription for every user who never opted in, all for a signal
        # that nothing consumes. The deferral loses nothing, because the
        # detector seeds the monitor from the session's current IdleHint each
        # time it builds (setup_dbus calls read_initial_state), and not at
        # process start alone.
        #
        # idle_detector is the harness's opt-out, and bus is the detector's
        # test seam. Both are captured here for the deferred build, which runs
        # before the seeding evaluation below, so an already-idle session
        # reaches the first verdict.
        self.idle_detector: "LogindIdleDetector | None" = None
        self._idle_detector_enabled: bool = bool(idle_detector)
        self._idle_detector_bus = bus
        self._detector_lock = threading.Lock()
        if self._mode == MODE_SYSTEM_IDLE:
            self._ensure_idle_detector()

        # Evaluate once at construction. Without this call, system-idle mode
        # stays off after every restart until the first lock or idle
        # transition arrives, and a restart into a locked session never gates.
        self._evaluate()

    # Reads

    def is_quiescent(self) -> bool:
        """The media loop's question. Lock-free by contract."""
        return self.quiescent

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def idle_minutes(self) -> int:
        return self._minutes

    # Inputs

    def on_lock_changed(self, active: bool) -> None:
        """Called from LockScreenManager.lock() right after it publishes
        gl.screen_locked. active carries that same value, and only the log
        reads it. The evaluation re-reads gl.screen_locked, so this object and
        the rest of the app cannot disagree about the lock state. The
        constructor's seeding evaluation has no argument to read."""
        log.debug(f"PresenceMonitor: screen lock -> {active}")
        if not active:
            # An unlock means a person at the machine. On a session whose idle
            # agent sets IdleHint and never clears it (swayidle with idlehint
            # and no matching resume) the unlock is the only such signal.
            # Without this line the idle term keeps measuring from a stale
            # IdleSinceHint minutes in the past, so the deck stays frozen
            # through the unlock and until the next deck press.
            self._last_deck_activity = time.time()
        self._evaluate()

    def on_idle_hint_changed(self, idle_hint: bool, idle_since: float = None) -> None:
        """The logind session IdleHint changed. idle_since carries the
        wall-clock time the session went idle, from IdleSinceHint. None means
        "as of now", which the arithmetic assumes when logind reports no
        usable timestamp."""
        with self._lock:
            self._idle_hint = bool(idle_hint)
            self._idle_since = idle_since if idle_hint else None
        self._evaluate()

    def notify_activity(self) -> None:
        """A deck input happened. The compositor and the lock state both miss
        a deck press, so this call is the only signal for a user present at
        the deck. It clears an idle hint, and it outranks a locked screen for
        DECK_ACTIVITY_GRACE_S."""
        self._last_deck_activity = time.time()
        # Fast path for the default mode. Nothing gates there, so an input
        # needs neither the lock nor the timer wheel. set_mode() rebinds _mode
        # atomically and evaluates for itself.
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
            # The opt-in builds the detector (see __init__). Build it outside
            # the lock, because an injected bus wires up inline and calls
            # straight back into _evaluate(). A switch back to the default
            # mode keeps the detector. The rule ignores it there, and a
            # teardown and rebuild on every toggle churns the bus.
            self._ensure_idle_detector()
        self._evaluate()

    def _ensure_idle_detector(self) -> None:
        """Builds the logind idle detector once, when the mode wants one.
        Idempotent and safe from any thread. Never call it while you hold
        self._lock, because an injected bus makes the build call back into
        _evaluate(), which takes that lock."""
        if not self._idle_detector_enabled:
            return
        with self._detector_lock:
            if self.idle_detector is not None:
                return
            self.idle_detector = LogindIdleDetector(self, bus=self._idle_detector_bus)

    def stop(self) -> None:
        """Releases the idle deadline and the D-Bus subscription. The app
        never calls this, because the monitor lives for the process. It exists
        so a scenario leaves no timer behind."""
        with self._lock:
            self._cancel_deadline_locked()
        if self.idle_detector is not None:
            self.idle_detector.stop()

    # Evaluation

    def _cancel_deadline_locked(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None

    def _on_deadline(self) -> None:
        """The idle delay elapsed. Runs on a timer_wheel dispatch thread."""
        self._evaluate()

    def _evaluate(self) -> None:
        """Recomputes quiescent from the current inputs, and arms the deadline
        that changes the verdict on its own. That deadline is the idle delay,
        or the deck-activity grace under a locked screen. Safe to call from
        any thread and as often as inputs arrive."""
        with self._lock:
            was = self.quiescent
            now = time.time()
            quiescent = False
            rearm_in = None

            if self._mode == MODE_SYSTEM_IDLE:
                since_activity = now - self._last_deck_activity
                if bool(getattr(gl, "screen_locked", False)):
                    # The strongest away signal, and the one that works
                    # without an idle agent. It stays independent of
                    # lock-on-lock-screen, which decides whether the deck
                    # shows its screensaver on lock, and says nothing about
                    # the user.
                    #
                    # It still yields to a recent deck press for
                    # DECK_ACTIVITY_GRACE_S. A locked screen over a live deck
                    # (lock-on-lock-screen off) is a supported configuration,
                    # and the person who presses its keys is at it. The seed
                    # _last_deck_activity = 0.0 means "no press seen", which
                    # reads here as an elapsed grace, so a process that starts
                    # into an already-locked session gates at once.
                    if since_activity >= self.DECK_ACTIVITY_GRACE_S:
                        quiescent = True
                    else:
                        # Nothing else calls back. The lock state holds, and
                        # logind never sees a deck press. Arm the grace's own
                        # expiry, or the gate stays off until the next
                        # unrelated input.
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
            # Run outside the lock. The fan-out reaches every controller's
            # media thread, and that loop needs none of this module's state.
            self._wake_media_threads()

    def _wake_media_threads(self) -> None:
        """Cuts short every media loop's inter-tick wait so a presence
        transition takes effect on the next tick instead of after up to half
        a second of gated cadence."""
        deck_manager = getattr(gl, "deck_manager", None)
        if deck_manager is None:
            return
        # Take a snapshot. remove_controller() mutates this list from unplug
        # and close threads during the loop.
        for controller in list(getattr(deck_manager, "deck_controller", None) or []):
            try:
                controller.media_player.wake()
            except Exception:
                log.opt(exception=True).warning(
                    "PresenceMonitor: failed to wake a deck's media thread"
                )


class LogindIdleDetector:
    """Feeds PresenceMonitor.on_idle_hint_changed() from the logind session
    properties IdleHint and IdleSinceHint.

    This class keeps the shape of LockScreenManager/Detectors/Logind.py. It
    uses the same system-bus Gio connection, the same resolve_session_path()
    (GetSession($XDG_SESSION_ID) with a GetSessionByPID fallback), the same
    bus= test seam, and the same inert answer to a GLib.Error. One shared
    resolver can then replace both.

    The desktop environment maintains IdleHint. GNOME and KDE set it from
    their own idle policy, while Niri, Sway and river need a user-side agent
    (swayidle idlehint N). Where nothing sets it, this component stays false
    and the monitor gates on the lock alone.
    """

    def __init__(self, monitor: PresenceMonitor, bus=None):
        self.monitor = monitor
        self.bus: Gio.DBusConnection | None = None
        self.session_path: str | None = None
        self._subscription_id: int | None = None
        if bus is not None:
            # An injected bus is an in-process double with no I/O to block on,
            # so wire it up inline and keep a scenario deterministic.
            self.setup_dbus(bus)
        else:
            # The real system bus goes on a daemon thread, like
            # LockScreenManager.__init__ does. main() constructs this object
            # on the startup path, and Gio.bus_get_sync plus a synchronous
            # GetSession round trip would hold app startup behind a wedged
            # logind for the call's full timeout.
            threading.Thread(target=self.setup_dbus, name="PresenceIdleSetup",
                             daemon=True).start()

    def setup_dbus(self, bus=None) -> None:
        try:
            # logind lives on the system bus. bus is a test seam and
            # production passes None. Compare against None, because a falsy but
            # valid double must not pull in the real bus.
            self.bus = bus if bus is not None else Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self.session_path = self.resolve_session_path()

            # This runs on a plain daemon thread, which has no thread-default
            # main context, so GDBus dispatches this callback on the global
            # default one, the GTK main loop, like every lock detector.
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
            # Every detector answers a failure the same way. Report once at
            # info and stay inert. The lock input keeps working, and the idle
            # half of the rule goes dark.
            log.info(f"Presence: logind IdleHint unavailable, idle gating inert ({e})")
        except Exception:
            log.opt(exception=True).warning(
                "Presence: unexpected failure wiring up the logind idle detector; "
                "idle gating inert"
            )

    def resolve_session_path(self) -> str:
        bus = self.bus
        if bus is None:
            # setup_dbus assigns self.bus immediately before it calls this, so
            # nothing reaches here. Raise GLib.Error to route an unusable
            # connection into setup_dbus's stay-inert branch.
            raise GLib.Error("logind system bus unavailable")

        session_id = os.getenv("XDG_SESSION_ID")
        if session_id:
            method = "GetSession"
            args = GLib.Variant("(s)", (session_id,))
        else:
            # PID 0 tells logind to resolve the caller from its bus
            # credentials. os.getpid() gives a sandbox-namespace number under
            # flatpak, which the host's logind reads as a host PID, either
            # unknown or belonging to an unrelated process's session. A live
            # logind (systemd 261) confirms the behaviour. pid 0 takes the
            # caller-credentials branch and answers "Caller does not belong to
            # any known session", where a numeric pid answers "PID <n> does
            # not belong to any known session". logind accepts pid 0 as a
            # valid argument. The logind lock detector wants the same shape,
            # for the shared resolver that replaces both.
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
            # setup_dbus never ran or never completed, so nothing exists to
            # read from. GLib.Error is the logind-unavailable channel that
            # both callers handle.
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
        """Seeds the monitor from the session's current properties. A process
        that starts into an already-idle session must gate without a wait for
        the next PropertiesChanged, which can never arrive."""
        hint = bool(self.read_property("IdleHint"))
        self.monitor.on_idle_hint_changed(hint, self._read_idle_since() if hint else None)

    def _read_idle_since(self, changed: dict = None):
        """IdleSinceHint as wall-clock seconds, or None when logind has no
        usable timestamp. logind reports 0 for a session that was never idle.
        This prefers the value the signal carries, and falls back to a
        property read, because logind does not always send it with
        IdleHint."""
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
            # This runs on the GTK main context, where GLib swallows an
            # escaping exception and reports no useful origin.
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
