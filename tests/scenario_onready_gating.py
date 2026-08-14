"""
The default on_update does not re-enter on_ready.

ActionCore's default on_update calls on_ready for compatibility. It skips
that call, and logs a debug line, while the initial on_ready is still in
flight. After on_ready_finished is set, the compat call runs on every
on_update again. An action that overrides on_update keeps its own body.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from loguru import logger as log

from fixtures import make_headless_controller, start_watchdog, wait_until

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

COMPAT_SKIP_MARKER = "on_update compat on_ready skipped"


class _LogCapture:
    """Adds a capturing loguru sink for the with block, so the assertions can
    read the skip line. A silent skip looks like a hung on_ready to a plugin
    author."""

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
    """Does not override on_update, so the compat default is the code under
    test. on_ready blocks on a gate and records entries and peak
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
    """Overrides on_update without a chain to the compat default. The guard
    must never touch this shape."""

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
    on_ready, with on_ready_called set and on_ready_finished clear."""
    page = controller.active_page
    ident = Input.Key("0x0")
    action = make_action(cls, controller, page, ident)
    page.initialize_actions()
    if not action.entered_ready.wait(timeout=5):
        return None
    return action


def check_compat_call_gated() -> int:
    controller = make_headless_controller(serial="onready-gating-1")
    try:
        action = start_gated_action(controller, CompatAction)
        if action is None:
            print("FAIL: on_ready never started")
            return 1

        # Inside the in-flight window.
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

        # Let the initial ready sequence complete.
        action.ready_gate.set()
        if not wait_until(lambda: action.on_ready_finished, timeout=5):
            print("FAIL: on_ready_finished never set")
            return 1
        # Page._run_ready_callbacks ends the sequence with its own on_update,
        # which for a non-overriding action is the compat on_ready.
        if not wait_until(lambda: action.ready_entries >= 2, timeout=5):
            print(f"FAIL(b): the trailing on_update never ran its compat on_ready ({action.ready_entries})")
            return 1
        if action.ready_concurrent_max > 1:
            print(f"FAIL(a): {action.ready_concurrent_max} concurrent on_ready bodies across the whole sequence")
            return 1

        # The compat path runs per call once the ready completed. The
        # comparison allows extra calls, because the controller's background
        # dispatch lands its own updates here.
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
    """The ungated on_update body, which calls on_ready with no gate."""
    self.on_ready()


def check_mutation_proof() -> int:
    """Puts the ungated body back on the real class and repeats the mid-ready
    probe. The observation must flip; a leg that survives its own mutation
    pins nothing."""
    saved_on_update = ActionCore.on_update
    ActionCore.on_update = _prefix_on_update

    controller = make_headless_controller(serial="onready-gating-3")
    try:
        action = start_gated_action(controller, CompatAction)
        if action is None:
            print("FAIL(d): on_ready never started")
            return 1

        # The ungated on_update blocks inside the re-entered on_ready until
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
    rc = check_compat_call_gated()
    if rc:
        return rc
    rc = check_overriding_action_untouched()
    if rc:
        return rc
    return check_mutation_proof()


if __name__ == "__main__":
    raise SystemExit(main())
