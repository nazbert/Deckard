"""
on_ready ordering guarantees, against the real dispatch code.

own_actions_tick and own_actions_update gate on on_ready_finished, so no tick
and no second on_ready runs beside the initial one. A per-plugin RLock
serializes get_settings against set_settings.
"""

# A raising on_ready still opens the gates, so the action does not stay dead
# for the page's lifetime.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import json
import threading
import time

import globals as gl
from fixtures import make_headless_controller, wait_until, start_watchdog

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore
import src.backend.settings_store as settings_store
from src.backend.PluginManager.PluginBase import PluginBase


class GatedAction(ActionCore):
    """on_ready blocks on an Event and records concurrency. on_tick and
    on_update count deliveries."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready_gate = threading.Event()
        self.entered_ready = threading.Event()
        self.ready_entries = 0
        self.ready_concurrent_max = 0
        self._ready_active = 0
        self._counter_lock = threading.Lock()
        self.tick_calls = 0
        self.update_calls = 0

    def on_ready(self):
        with self._counter_lock:
            self.ready_entries += 1
            self._ready_active += 1
            self.ready_concurrent_max = max(self.ready_concurrent_max, self._ready_active)
        self.entered_ready.set()
        self.ready_gate.wait(timeout=5)
        with self._counter_lock:
            self._ready_active -= 1

    def on_update(self):
        self.update_calls += 1
        super().on_update()  # default on_update calls on_ready (compat path)

    def on_tick(self):
        self.tick_calls += 1


class RaisingReadyAction(ActionCore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tick_calls = 0

    def on_ready(self):
        raise RuntimeError("boom in on_ready")

    def on_update(self):
        pass  # keep the compat path from re-raising

    def on_tick(self):
        self.tick_calls += 1


def make_action(cls, controller, page, ident):
    action = cls(
        action_id="dev_test::OnReadyOrdering",
        action_name="OnReadyOrdering",
        deck_controller=controller,
        page=page,
        plugin_base=None,
        state=0,
        input_ident=ident,
    )
    page.action_objects.setdefault(ident.input_type, {})[ident.json_identifier] = {0: {0: action}}
    return action


def check_gates() -> int:
    controller = make_headless_controller(serial="onready-1")
    try:
        page = controller.active_page
        ident = Input.Key("0x0")
        action = make_action(GatedAction, controller, page, ident)

        input_obj = controller.get_input(ident)
        if input_obj is None:
            print("FAIL: controller has no key 0x0 input")
            return 1
        state_obj = input_obj.states[0]

        # Schedule the ready callbacks on the pool. on_ready blocks on the
        # gate. The controller's background tick loop is live and calls
        # own_actions_tick too, which strengthens the gated-phase assertions.
        page.initialize_actions()
        if not action.entered_ready.wait(timeout=5):
            print("FAIL: on_ready never started")
            return 1

        # Ticks while on_ready is in flight must be skipped.
        for _ in range(3):
            state_obj.own_actions_tick()
        # An update while on_ready is in flight must be skipped. Otherwise
        # on_update runs the compat on_ready beside the pool's on_ready.
        state_obj.own_actions_update()
        time.sleep(0.1)  # give the background tick loop a few laps

        failed = False
        if action.tick_calls != 0:
            print(f"FAIL(a): on_tick ran {action.tick_calls}x before on_ready finished")
            failed = True
        if action.ready_concurrent_max > 1:
            print(f"FAIL(c): {action.ready_concurrent_max} concurrent on_ready invocations")
            failed = True
        if action.update_calls != 0:
            print(f"FAIL(c): on_update dispatched {action.update_calls}x mid-initialization")
            failed = True
        if failed:
            action.ready_gate.set()  # unblock the pool thread before teardown
            return 1

        action.ready_gate.set()
        if not wait_until(lambda: action.on_ready_finished, timeout=5):
            print("FAIL: on_ready_finished never set")
            return 1
        # The initial sequence ends with its own on_update.
        if not wait_until(lambda: action.update_calls >= 1, timeout=5):
            print("FAIL: trailing on_update never ran")
            return 1
        if action.ready_concurrent_max > 1:
            print(f"FAIL(c): {action.ready_concurrent_max} concurrent on_ready (trailing)")
            return 1

        # The gates are open now. The background tick loop counts too.
        updates_before = action.update_calls
        state_obj.own_actions_tick()
        state_obj.own_actions_update()
        if action.tick_calls < 1 or action.update_calls < updates_before + 1:
            print(f"FAIL: post-ready dispatch broken (ticks={action.tick_calls}, updates={action.update_calls})")
            return 1

        print("PASS: tick/update gate on on_ready_finished; no concurrent on_ready")
    finally:
        fixtures.teardown(controller)

    # Raising on_ready still opens the gates.
    controller2 = make_headless_controller(serial="onready-2")
    try:
        from loguru import logger as _log
        page2 = controller2.active_page
        ident = Input.Key("0x0")
        action2 = make_action(RaisingReadyAction, controller2, page2, ident)
        state_obj2 = controller2.get_input(ident).states[0]
        # on_ready raises here and _run_ready_callbacks logs the traceback.
        # Silence loguru until wait_until sees completion, so the expected
        # noise stays out of the run.
        _log.disable("")
        try:
            page2.initialize_actions()
            ready = wait_until(lambda: action2.on_ready_finished, timeout=5)
        finally:
            _log.enable("")
        if not ready:
            print("FAIL: raising on_ready left on_ready_finished unset (action dead forever)")
            return 1
        state_obj2.own_actions_tick()
        if action2.tick_calls < 1:
            print("FAIL: tick not delivered after raising on_ready")
            return 1
        print("PASS: raising on_ready still opens the gates")
    finally:
        fixtures.teardown(controller2)
    return 0


def make_plugin(settings_path: str) -> PluginBase:
    """__init__ is bypassed because it needs a real plugin dir, and only the
    settings accessors are under test."""
    plugin = PluginBase.__new__(PluginBase)
    plugin.settings_path = settings_path
    plugin.plugin_name = "OnReadyOrderingTest"
    plugin.PATH = settings_path
    # _settings_lock stays unset, to exercise the lazy _get_settings_lock
    # path that __new__-built instances take.
    return plugin


def check_settings_serialization() -> int:
    import os
    settings_path = os.path.join(gl.DATA_PATH, "settings", "plugins", "onready_test", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    # Seed the v1 format so get_settings takes the conversion-write path.
    with open(settings_path, "w") as f:
        json.dump({"old-key": 1}, f)

    plugin = make_plugin(settings_path)

    in_window = threading.Event()
    # The plugin accessors persist through the settings store, so the write
    # to hold open is the store's.
    real_write = settings_store.atomic_write_json
    first_call = [True]

    def slow_first_write(path, content):
        # The first write is the v1 to v2 conversion. Hold it open long
        # enough for the racing set_settings to slip between the stale read
        # and this write.
        if first_call[0]:
            first_call[0] = False
            in_window.set()
            time.sleep(0.4)
        real_write(path, content)

    settings_store.atomic_write_json = slow_first_write
    try:
        results = {}

        def reader():
            results["get"] = plugin.get_settings()

        def writer():
            in_window.wait(timeout=5)
            plugin.set_settings({"old-key": 1, "new-key": 2})

        t_r = threading.Thread(target=reader)
        t_w = threading.Thread(target=writer)
        t_r.start()
        t_w.start()
        t_r.join(timeout=10)
        t_w.join(timeout=10)
        if t_r.is_alive() or t_w.is_alive():
            print("FAIL(b): settings accessor deadlock")
            return 1
    finally:
        settings_store.atomic_write_json = real_write

    final = plugin.get_settings()
    if final.get("new-key") != 2:
        print(f"FAIL(b): stale v1->v2 conversion clobbered a concurrent save: {final}")
        return 1
    print("PASS: conversion write serialized against concurrent set_settings")

    # set_settings over a corrupt existing file must not raise. The guarded
    # read logs the caught JSONDecodeError, so silence loguru around the
    # corruption.
    from loguru import logger as _log
    with open(settings_path, "w") as f:
        f.write('{"trunc')
    _log.disable("")
    try:
        plugin.set_settings({"fresh": True})
    except Exception as e:
        print(f"FAIL(b): set_settings raised over a corrupt file: {type(e).__name__}: {e}")
        return 1
    finally:
        _log.enable("")
    if plugin.get_settings().get("fresh") is not True:
        print("FAIL(b): settings not recovered after corrupt-file save")
        return 1
    print("PASS: set_settings survives a corrupt existing file")
    return 0


def main() -> int:
    start_watchdog(40, "onready_ordering")
    fixtures._install_integration_globals()
    rc = check_gates()
    if rc:
        return rc
    return check_settings_serialization()


if __name__ == "__main__":
    raise SystemExit(main())
