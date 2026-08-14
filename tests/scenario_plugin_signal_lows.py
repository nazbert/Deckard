"""
Unit-tier scenario for grouped plugin, signal and GtkHelper fixes.

SignalManager.trigger_signal forwards kwargs and runs a truthy handler once. No
deck, no widgets.
"""

# EventHolder dedupes a functools.partial listener, CallbackRegistry accepts a
# __slots__ owner, launch_backend validates its path, get_own_key resolves
# through get_input, and a null action id survives removal.
import functools
import threading
import weakref

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from gi.repository import GLib

from src.Signals import Signals
from src.Signals.SignalManager import SignalManager
from src.Signals.weak_callbacks import CallbackRegistry


def pump_main_context(max_iterations: int = 25) -> int:
    """Dispatch pending sources on the default main context. The bound stops
    a forever-rescheduling idle from hanging the scenario. Returns the number
    of iterations that dispatched something."""
    ctx = GLib.MainContext.default()
    dispatched = 0
    for _ in range(max_iterations):
        if not ctx.pending():
            break
        ctx.iteration(False)
        dispatched += 1
    return dispatched


def check_trigger_signal_kwargs_single_shot():
    sm = SignalManager()
    received = []

    def handler(*args, **kwargs):
        received.append((args, kwargs))
        return True  # truthy, so GLib would re-schedule a raw idle handler

    sm.connect_signal(Signals.PageRename, handler)
    sm.trigger_signal(Signals.PageRename, "old.json", new_path="new.json")

    pump_main_context()
    assert received, "handler never ran -- idle source was not dispatched"
    assert received[0] == (("old.json",), {"new_path": "new.json"}), (
        f"args/kwargs not forwarded intact: {received[0]!r}"
    )

    # A truthy return from the handler must not keep the source alive.
    pump_main_context()
    pump_main_context()
    assert len(received) == 1, (
        f"truthy-returning handler was re-scheduled: ran {len(received)} times"
    )
    assert not GLib.MainContext.default().pending(), (
        "idle source still pending after dispatch -- would re-run forever"
    )


def check_eventholder_partial_dedupe_no_crash():
    from src.backend.PluginManager.EventHolder import EventHolder

    hits = []

    def target(tag, event_id, *args, **kwargs):
        hits.append(tag)

    holder = EventHolder(plugin_base=None, event_id="test_plugin::TestEvent")
    cb = functools.partial(target, "a")
    holder.add_listener(cb)
    # A second add of the same partial hits the dedupe-warning path. A
    # partial carries no __name__.
    holder.add_listener(cb)
    assert len(holder.observers.snapshot()) == 1


class _SlottedOwner:
    """A bound method of this class cannot be WeakMethod'd. __slots__ without
    __weakref__ makes the instance non-weak-referenceable."""
    __slots__ = ("calls",)

    def __init__(self):
        self.calls = 0

    def method(self, *args, **kwargs):
        self.calls += 1


def check_slots_owner_falls_back_strong():
    owner = _SlottedOwner()

    # The owner really is non-weak-referenceable.
    try:
        weakref.WeakMethod(owner.method)
    except TypeError:
        pass
    else:
        raise AssertionError("fixture sanity: expected WeakMethod to refuse a __slots__ owner")

    registry = CallbackRegistry()
    assert registry.add(owner.method) is True, "add() must not raise or refuse a __slots__ owner"
    snap = registry.snapshot()
    assert len(snap) == 1, snap
    snap[0]()
    assert owner.calls == 1

    # End-to-end through the SignalManager connect path too.
    sm = SignalManager()
    sm.connect_signal(Signals.PageAdd, owner.method)  # must not raise
    sm.trigger_signal(Signals.PageAdd, "page.json")
    pump_main_context()
    assert owner.calls == 2, f"signal did not reach the strong-stored bound method: {owner.calls}"


class _LaunchStubServer:
    port = 4242


class _LaunchStub:
    """Bare object exposing what launch_backend touches before the path
    checks, start_server and server.port."""
    server = _LaunchStubServer()

    def start_server(self):
        pass


def check_launch_backend_path_validation():
    from src.backend.PluginManager.ActionCore import ActionCore

    stub = _LaunchStub()

    for bad_path in (None, "/nonexistent/definitely/not/here/backend.py"):
        try:
            ActionCore.launch_backend(stub, backend_path=bad_path)
        except ValueError:
            pass  # expected, a clean validation error before any Popen
        except TypeError as e:
            raise AssertionError(
                f"launch_backend({bad_path!r}) fed a bad value into os.path.exists: {e}"
            )
        else:
            raise AssertionError(
                f"launch_backend({bad_path!r}) did not raise -- would Popen a garbage command"
            )


def check_get_own_key_via_get_input():
    from src.backend.DeckManagement.InputIdentifier import Input
    from src.backend.PluginManager.ActionCore import ActionCore

    sentinel = object()

    class _ControllerStub:
        def __init__(self):
            self.asked = None

        def get_input(self, ident):
            self.asked = ident
            return sentinel

    class _ActionStub:
        pass

    action = _ActionStub()
    action.input_ident = Input.Key("0x0")
    action.deck_controller = _ControllerStub()

    result = ActionCore.get_own_key(action)
    assert result is sentinel, f"get_own_key must resolve through get_input, got {result!r}"
    assert action.deck_controller.asked is action.input_ident

    # Non-key identifiers have no "own key".
    action.input_ident = Input.Dial("0")
    action.deck_controller = _ControllerStub()
    assert ActionCore.get_own_key(action) is None
    assert action.deck_controller.asked is None


def check_remove_actions_survives_null_id():
    from src.backend.PageManagement.Page import Page

    # An action with an explicit null id, one with no id at all, and a normal
    # one belonging to the plugin being removed. A None id that reaches
    # .split("::") raises AttributeError and aborts the whole removal.
    page_dict = {
        "keys": {
            "0x0": {
                "states": {
                    "0": {
                        "actions": [
                            {"id": None},
                            {"name": "no-id-key"},
                            {"id": "victim_plugin::SomeAction"},
                            {"id": "other_plugin::KeepMe"},
                        ]
                    }
                }
            }
        }
    }

    class _PageStub:
        def __init__(self, d):
            self.dict = d

        def save(self):
            pass  # no disk I/O in the unit tier

    stub = _PageStub(page_dict)
    # Must not raise on the None id.
    Page.remove_plugin_actions_from_json(stub, "victim_plugin")

    remaining = stub.dict["keys"]["0x0"]["states"]["0"]["actions"]
    ids = [a.get("id") for a in remaining]
    assert "victim_plugin::SomeAction" not in ids, f"victim action not removed: {ids!r}"
    assert "other_plugin::KeepMe" in ids, f"unrelated action wrongly removed: {ids!r}"
    # The null-id and no-id actions belong to no plugin and stay untouched.
    assert len(remaining) == 3, f"expected 3 survivors, got {remaining!r}"


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_plugin_signal_lows")
    assert threading.current_thread() is threading.main_thread()
    check_trigger_signal_kwargs_single_shot()
    check_eventholder_partial_dedupe_no_crash()
    check_slots_owner_falls_back_strong()
    check_launch_backend_path_validation()
    check_get_own_key_via_get_input()
    check_remove_actions_survives_null_id()
    print("PASS: scenario_plugin_signal_lows")


if __name__ == "__main__":
    main()
