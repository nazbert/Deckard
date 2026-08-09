"""
Scenario: the default on_update no longer re-enters on_ready.

The lifecycle split landed on_ready_finished and gated tick/update
DISPATCH on it (ControllerInputState.own_actions_{tick,update}), but the
hole stayed open inside ActionCore itself: the default on_update ran
`self.on_ready()` unconditionally, so any caller reaching on_update while
the initial on_ready was still in flight ran a SECOND on_ready body
concurrently with it -- the duplicate-ready class the issue exists to kill
(real plugins allocate, subscribe and spawn backend processes in on_ready).
The dispatch-side gate only covers the app's own path; nothing covered a
plugin calling on_update() on itself, or any other caller.

The compat call is now skipped with a debug log, never deferred: the
in-flight ready sequence ends with its own on_update
(Page._run_ready_callbacks), so the redraw is not lost, and a queued
duplicate is exactly the re-entry being removed. After a completed ready the
path is unchanged.

Legs:
  a. re-entry: on_update() called by hand while on_ready blocks in the pool
     runs NO second on_ready body. The DeckController gate is bypassed on
     purpose -- this pins the ActionCore-level guard on its own.
  b. steady state: once on_ready_finished is set, on_update() runs the
     compat on_ready per call, exactly as it did before.
  c. an action that OVERRIDES on_update is untouched -- its body runs
     mid-ready and the guard never injects an on_ready into it.
  d. mutation-proof: putting the pre-fix body back on ActionCore flips (a)
     to the old, broken observation, so the leg is not vacuous.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from loguru import logger as log

from fixtures import make_headless_controller, start_watchdog, wait_until

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

COMPAT_SKIP_MARKER = "on_update compat on_ready skipped"


class _LogCapture:
    """Attaches a capturing loguru sink for the duration of a `with` block,
    so the "skipped" debug line can be asserted on: a silent skip is
    indistinguishable from a hung on_ready when a plugin author debugs it."""

    def __init__(self, level: str = "DEBUG"):
        self._level = level
        self.records: list[str] = []

    def __enter__(self):
        self._handle = log.add(lambda message: self.records.append(str(message)), level=self._level)
        return self

    def __exit__(self, *exc):
        log.remove(self._handle)
        return False

    def text(self) -> str:
        return "".join(self.records)


class CompatAction(ActionCore):
    """Deliberately does NOT override on_update -- the compat default IS the
    code under test. on_ready blocks on a gate and records entries plus peak
    concurrency."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_gate = threading.Event()
        self.entered_ready = threading.Event()
        self._counter_lock = threading.Lock()
        self.ready_entries = 0
        self._ready_active = 0
        self.ready_concurrent_max = 0

    def on_ready(self):
        with self._counter_lock:
            self.ready_entries += 1
            self._ready_active += 1
            self.ready_concurrent_max = max(self.ready_concurrent_max, self._ready_active)
        self.entered_ready.set()
        self.ready_gate.wait(timeout=5)
        with self._counter_lock:
            self._ready_active -= 1


class OverridingAction(CompatAction):
    """Overrides on_update without chaining to the compat default -- the
    shape the guard must never touch."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.update_calls = 0

    def on_update(self):
        self.update_calls += 1


def make_action(cls, controller, page, ident):
    action = cls(
        action_id="dev_test::OnReadyGating",
        action_name="OnReadyGating",
        deck_controller=controller,
        page=page,
        plugin_base=None,
        state=0,
        input_ident=ident,
    )
    page.action_objects.setdefault(ident.input_type, {})[ident.json_identifier] = {0: {0: action}}
    return action


def start_gated_action(controller, cls):
    """Schedules the ready callbacks and returns the action parked inside
    on_ready (on_ready_called set, on_ready_finished not) -- the window."""
    page = controller.active_page
    ident = Input.Key("0x0")
    action = make_action(cls, controller, page, ident)
    page.initialize_actions()
    if not action.entered_ready.wait(timeout=5):
        return None
    return action


def check_compat_call_is_gated() -> int:
    controller = make_headless_controller(serial="onready-gating-1")
    try:
        action = start_gated_action(controller, CompatAction)
        if action is None:
            print("FAIL: on_ready never started")
            return 1

        # --- (a) inside the in-flight window -----------------------------
        with _LogCapture(level="DEBUG") as capture:
            action.on_update()
            log_text = capture.text()

        failed = False
        if action.ready_entries != 1:
            print(f"FAIL(a): on_update re-entered on_ready mid-ready ({action.ready_entries} entries)")
            failed = True
        if action.ready_concurrent_max > 1:
            print(f"FAIL(a): {action.ready_concurrent_max} concurrent on_ready bodies")
            failed = True
        if COMPAT_SKIP_MARKER not in log_text:
            print("FAIL(a): the skipped compat on_ready logged nothing -- a silent skip is undebuggable")
            failed = True
        if failed:
            action.ready_gate.set()  # unblock the pool thread before teardown
            return 1

        # --- let the initial ready sequence complete ---------------------
        action.ready_gate.set()
        if not wait_until(lambda: action.on_ready_finished, timeout=5):
            print("FAIL: on_ready_finished never set")
            return 1
        # Page._run_ready_callbacks ends the sequence with its own
        # on_update, which for a non-overriding action IS the compat
        # on_ready -- the steady-state path, unchanged by the guard.
        if not wait_until(lambda: action.ready_entries >= 2, timeout=5):
            print(f"FAIL(b): the trailing on_update never ran its compat on_ready ({action.ready_entries})")
            return 1
        if action.ready_concurrent_max > 1:
            print(f"FAIL(a): {action.ready_concurrent_max} concurrent on_ready bodies across the whole sequence")
            return 1

        # --- (b) the compat path is unchanged once the ready completed ----
        # >= not ==: the controller's background dispatch can land its own
        # updates here, which is exactly the steady-state path being pinned.
        before = action.ready_entries
        action.on_update()
        action.on_update()
        if action.ready_entries < before + 2:
            print(f"FAIL(b): the compat on_ready stopped firing per on_update ({before} -> {action.ready_entries})")
            return 1

        print("PASS: the compat on_update call gates on on_ready_finished and is unchanged after it")
    finally:
        fixtures.teardown(controller)
    return 0


def check_overriding_action_untouched() -> int:
    controller = make_headless_controller(serial="onready-gating-2")
    try:
        action = start_gated_action(controller, OverridingAction)
        if action is None:
            print("FAIL(c): on_ready never started")
            return 1

        action.on_update()
        failed = False
        if action.update_calls != 1:
            print(f"FAIL(c): an overriding on_update was suppressed by the guard ({action.update_calls} calls)")
            failed = True
        if action.ready_entries != 1:
            print(f"FAIL(c): the guard injected an on_ready into an overriding action ({action.ready_entries})")
            failed = True
        if failed:
            action.ready_gate.set()
            return 1

        action.ready_gate.set()
        if not wait_until(lambda: action.on_ready_finished, timeout=5):
            print("FAIL(c): on_ready_finished never set")
            return 1
        if not wait_until(lambda: action.update_calls >= 2, timeout=5):
            print(f"FAIL(c): the trailing on_update never reached the override ({action.update_calls})")
            return 1

        before = action.update_calls
        action.on_update()
        if action.update_calls < before + 1:
            print(f"FAIL(c): an overriding on_update stopped running after ready ({before} -> {action.update_calls})")
            return 1
        if action.ready_entries != 1:
            print(f"FAIL(c): an overriding action ran on_ready {action.ready_entries}x -- the guard is not inert here")
            return 1

        print("PASS: an action overriding on_update is untouched by the guard")
    finally:
        fixtures.teardown(controller)
    return 0


def _prefix_on_update(self):
    """ActionCore.on_update as it stood before the fix."""
    self.on_ready()


def check_mutation_proof() -> int:
    """Puts the pre-fix body back on the REAL class and re-runs the same
    mid-ready probe: it must flip to the old, broken observation. A leg that
    survives its own mutation is pinning nothing."""
    saved_on_update = ActionCore.on_update
    ActionCore.on_update = _prefix_on_update

    controller = make_headless_controller(serial="onready-gating-3")
    try:
        action = start_gated_action(controller, CompatAction)
        if action is None:
            print("FAIL(d): on_ready never started")
            return 1

        # The pre-fix on_update blocks inside the re-entered on_ready until
        # the gate opens, so drive it off-thread and watch the counter.
        prober = threading.Thread(target=action.on_update, name="prefix-on_update", daemon=True)
        prober.start()
        reentered = wait_until(lambda: action.ready_entries >= 2, timeout=5)
        concurrent_max = action.ready_concurrent_max
        action.ready_gate.set()
        prober.join(timeout=5)

        if not reentered:
            print("FAIL(d): the pre-fix on_update did NOT re-enter on_ready -- leg (a) proves nothing")
            return 1
        if concurrent_max < 2:
            print("FAIL(d): the pre-fix on_update did NOT run on_ready concurrently -- leg (a) proves nothing")
            return 1

        print("PASS: restoring the pre-fix on_update flips leg (a) -- the guard is load-bearing")
    finally:
        ActionCore.on_update = saved_on_update
        fixtures.teardown(controller)
    return 0


def main() -> int:
    start_watchdog(90, "onready_gating")
    fixtures._install_integration_globals()
    rc = check_compat_call_is_gated()
    if rc:
        return rc
    rc = check_overriding_action_untouched()
    if rc:
        return rc
    return check_mutation_proof()


if __name__ == "__main__":
    raise SystemExit(main())
