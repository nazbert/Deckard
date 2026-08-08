"""
PresenceMonitor: the quiescence rule and its wake fan-out (issue #144).

The monitor is the only thing that can turn `DeckController.animations_gated()`
true, so everything the media loop's gate depends on is pinned here:

  1. mode "screensaver" (the DEFAULT) never reports quiescent, whatever the
     inputs do -- this is what makes the feature opt-in and today's behavior
     bit-for-bit unchanged for everyone who doesn't opt in,
  2. mode "system-idle": screen lock gates instantly and unlock clears,
  3. idle arithmetic: an IdleHint whose IdleSinceHint is already past the
     residual deadline gates immediately; one that isn't arms a deadline and
     gates when it elapses,
  4. deck activity (the input funnel the compositor cannot see) clears a
     pending idle and re-arms the deadline from the press,
  5. every transition -- both directions -- wakes every deck's media thread,
     over a SNAPSHOT of the controller list (unplug/close mutate it) and
     without letting one controller's failure strand the others,
  6. set_mode() re-evaluates immediately (the Settings dialog's runtime push),
  7. the constructor seeds mode/minutes from AppSettings AND evaluates against
     the current gl.screen_locked -- without that seed the setting is silently
     off after every restart.

Unit tier: no deck, no GTK, no D-Bus.
"""
import fixtures  # noqa: F401  (isolates gl.DATA_PATH before anything reads it)

import time

import globals as gl

from src.backend.PresenceMonitor.PresenceMonitor import (  # noqa: E402
    MODE_SCREENSAVER,
    MODE_SYSTEM_IDLE,
    PresenceMonitor,
)


class WakeRecorder:
    """Stands in for a deck's MediaPlayerThread on the fan-out path."""

    def __init__(self, raises: bool = False):
        self.wakes = 0
        self._raises = raises

    def wake(self) -> None:
        self.wakes += 1
        if self._raises:
            raise RuntimeError("media thread is gone")


class StubController:
    def __init__(self, raises: bool = False):
        self.media_player = WakeRecorder(raises=raises)


def install_controllers(*controllers):
    gl.deck_manager.deck_controller[:] = list(controllers)
    return controllers


def make_monitor(mode=MODE_SYSTEM_IDLE, minutes=1) -> PresenceMonitor:
    return PresenceMonitor(mode=mode, minutes=minutes)


def set_locked(monitor: PresenceMonitor, locked: bool) -> None:
    """Exactly what LockScreenManager.lock() does: publish the global, then
    notify."""
    gl.screen_locked = locked
    monitor.on_lock_changed(locked)


def check_default_mode_never_gates() -> None:
    monitor = make_monitor(mode=MODE_SCREENSAVER)
    assert monitor.is_quiescent() is False

    set_locked(monitor, True)
    assert monitor.is_quiescent() is False, "mode 'screensaver' must ignore lock"

    monitor.on_idle_hint_changed(True, idle_since=time.time() - 3600)
    assert monitor.is_quiescent() is False, "mode 'screensaver' must ignore idle"

    monitor.notify_activity()
    assert monitor.is_quiescent() is False

    set_locked(monitor, False)
    monitor.stop()
    print("PASS: default mode 'screensaver' never reports quiescent")


def check_lock_gates_and_unlock_clears() -> None:
    a, b = install_controllers(StubController(), StubController())
    monitor = make_monitor()
    assert monitor.is_quiescent() is False

    set_locked(monitor, True)
    assert monitor.is_quiescent() is True, "lock must gate in system-idle mode"
    assert a.media_player.wakes == 1 and b.media_player.wakes == 1, (
        f"gate transition must wake every deck: {a.media_player.wakes}, "
        f"{b.media_player.wakes}"
    )

    # Re-notifying the same state is not a transition and must not re-wake.
    monitor.on_lock_changed(True)
    assert a.media_player.wakes == 1, "a no-op re-evaluation must not wake"

    set_locked(monitor, False)
    assert monitor.is_quiescent() is False, "unlock must clear the gate"
    assert a.media_player.wakes == 2 and b.media_player.wakes == 2, (
        "the ungate transition must wake every deck too"
    )
    monitor.stop()
    print("PASS: lock gates, unlock clears, both directions wake every deck")


def check_idle_arithmetic() -> None:
    monitor = make_monitor(minutes=1)

    # Idle since well past the 1-minute residual -> gates on arrival.
    monitor.on_idle_hint_changed(True, idle_since=time.time() - 600)
    assert monitor.is_quiescent() is True, "an already-elapsed idle must gate at once"

    monitor.on_idle_hint_changed(False)
    assert monitor.is_quiescent() is False, "IdleHint clearing must ungate"

    # Idle that started 60s-0.4s ago: residual ~0.4s, so NOT yet quiescent,
    # and the deadline must fire on its own without any further input.
    monitor.on_idle_hint_changed(True, idle_since=time.time() - 60 + 0.4)
    assert monitor.is_quiescent() is False, (
        "an idle whose residual has not elapsed must not gate yet"
    )
    assert fixtures.wait_until(monitor.is_quiescent, timeout=3.0), (
        "the armed residual deadline never fired"
    )
    monitor.stop()
    print("PASS: idle arithmetic gates immediately or on the armed deadline")


def check_deck_activity_clears_and_rearms() -> None:
    (a,) = install_controllers(StubController())
    monitor = make_monitor(minutes=1)

    monitor.on_idle_hint_changed(True, idle_since=time.time() - 600)
    assert monitor.is_quiescent() is True
    assert a.media_player.wakes == 1

    # The compositor still says "idle" (deck presses are invisible to it) --
    # the press alone has to clear the gate and restart the clock.
    monitor.notify_activity()
    assert monitor.is_quiescent() is False, (
        "a deck press must clear the gate even while IdleHint is still true"
    )
    assert a.media_player.wakes == 2, "clearing the gate must wake the deck"

    # ... and it must not immediately re-gate: the deadline is now measured
    # from the press, not from the (much older) IdleSinceHint.
    time.sleep(0.3)
    assert monitor.is_quiescent() is False, "the deadline must re-arm from the press"
    monitor.stop()
    print("PASS: deck activity clears the gate and re-arms the deadline")


def check_set_mode_reevaluates() -> None:
    (a,) = install_controllers(StubController())
    monitor = make_monitor(mode=MODE_SCREENSAVER)
    set_locked(monitor, True)
    assert monitor.is_quiescent() is False

    monitor.set_mode(MODE_SYSTEM_IDLE, 5)
    assert monitor.is_quiescent() is True, "switching to system-idle while locked must gate"
    assert monitor.idle_minutes == 5
    assert a.media_player.wakes == 1

    monitor.set_mode(MODE_SCREENSAVER)
    assert monitor.is_quiescent() is False, "switching back must ungate immediately"
    assert a.media_player.wakes == 2

    # An unknown value degrades to the conservative default rather than
    # leaving gating on.
    monitor.set_mode("nonsense")
    assert monitor.mode == MODE_SCREENSAVER
    assert monitor.is_quiescent() is False

    # Minutes are clamped to >= 1 (the SpinRow's floor).
    monitor.set_mode(MODE_SYSTEM_IDLE, 0)
    assert monitor.idle_minutes == 1, f"minutes not clamped: {monitor.idle_minutes}"

    set_locked(monitor, False)
    monitor.stop()
    print("PASS: set_mode re-evaluates and sanitizes its arguments")


def check_constructor_seeds_from_settings() -> None:
    """A restart while the screen is already locked must come up gated -- the
    seed is the whole reason the setting survives a restart at all."""
    gl.settings_manager._app_settings.setdefault("performance", {}).update({
        "animation-pause-mode": MODE_SYSTEM_IDLE,
        "animation-idle-minutes": 9,
    })
    gl.screen_locked = True
    try:
        monitor = PresenceMonitor()
        assert monitor.mode == MODE_SYSTEM_IDLE, (
            f"mode not seeded from AppSettings: {monitor.mode!r}"
        )
        assert monitor.idle_minutes == 9, (
            f"minutes not seeded from AppSettings: {monitor.idle_minutes}"
        )
        assert monitor.is_quiescent() is True, (
            "the constructor must evaluate against the CURRENT gl.screen_locked"
        )
        monitor.stop()
    finally:
        gl.screen_locked = False
        gl.settings_manager._app_settings["performance"].clear()

    # And with the shipped default the same restart changes nothing.
    gl.screen_locked = True
    try:
        monitor = PresenceMonitor()
        assert monitor.mode == MODE_SCREENSAVER
        assert monitor.is_quiescent() is False, (
            "the DEFAULT mode must not gate on a locked-at-startup session"
        )
        monitor.stop()
    finally:
        gl.screen_locked = False
    print("PASS: constructor seeds mode/minutes and evaluates the current lock state")


def check_fan_out_is_snapshot_and_contained() -> None:
    """remove_controller() mutates gl.deck_manager.deck_controller from
    unplug/close threads, and a torn-down controller can raise out of wake()
    -- neither may strand the rest of the fan-out."""
    survivor = StubController()
    exploder = StubController(raises=True)

    class SelfRemovingController(StubController):
        def __init__(self):
            super().__init__()
            self.media_player = self

        def wake(self):
            # Mutates the very list the fan-out is iterating.
            gl.deck_manager.deck_controller.clear()
            self.wakes = getattr(self, "wakes", 0) + 1

    remover = SelfRemovingController()
    install_controllers(remover, exploder, survivor)

    monitor = make_monitor()
    set_locked(monitor, True)

    assert remover.wakes == 1, "the mutating controller itself was not woken"
    assert exploder.media_player.wakes == 1, "a raising wake() was not attempted"
    assert survivor.media_player.wakes == 1, (
        "the fan-out did not reach every controller -- it either iterated the "
        "live list or aborted on the first failure"
    )
    set_locked(monitor, False)
    monitor.stop()
    print("PASS: the wake fan-out iterates a snapshot and contains failures")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_presence_monitor")
    fixtures.install_stub_globals()

    check_default_mode_never_gates()
    check_lock_gates_and_unlock_clears()
    check_idle_arithmetic()
    check_deck_activity_clears_and_rearms()
    check_set_mode_reevaluates()
    check_constructor_seeds_from_settings()
    check_fan_out_is_snapshot_and_contained()

    print("\nALL PASS: scenario_presence_monitor")


if __name__ == "__main__":
    main()
