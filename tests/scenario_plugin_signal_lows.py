"""
Unit-tier scenario for the grouped plugin/signal/GtkHelper LOWs (issue #56).

Covers the behavioral fixes:
  (a) SignalManager.trigger_signal forwards **kwargs to handlers and a
      truthy-returning handler runs exactly once (GLib must not re-schedule
      the idle source forever)
  (b) EventHolder.add_listener's dedupe-warning path survives a
      functools.partial listener (no `.__name__` AttributeError)
  (c) CallbackRegistry.add / SignalManager.connect_signal accept a bound
      method whose owner uses __slots__ (not weak-referenceable): stored as
      a strong ref instead of raising TypeError at connect time
  (d) ActionCore.launch_backend raises ValueError for a None or nonexistent
      backend_path instead of feeding None into os.path.exists
  (e) ActionCore.get_own_key resolves through get_input(self.input_ident)
      (the attributes the old body read never existed)

All checks drive the units directly -- no deck, no GTK widgets; (a) pumps
the default GLib main context by hand.
"""
import functools
import threading
import weakref

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from gi.repository import GLib

from src.Signals import Signals
from src.Signals.SignalManager import SignalManager
from src.Signals.weak_callbacks import CallbackRegistry


def pump_main_context(max_iterations: int = 25) -> int:
    """Dispatch pending sources on the default main context, bounded so a
    forever-rescheduling idle (the pre-fix bug) can't hang the scenario.
    Returns the number of iterations that dispatched something."""
    ctx = GLib.MainContext.default()
    dispatched = 0
    for _ in range(max_iterations):
        if not ctx.pending():
            break
        ctx.iteration(False)
        dispatched += 1
    return dispatched


def check_trigger_signal_kwargs_and_single_shot():
    sm = SignalManager()
    received = []

    def handler(*args, **kwargs):
        received.append((args, kwargs))
        return True  # truthy: GLib would re-schedule a raw idle handler

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
    # Second add of the same partial hits the dedupe-warning path, which
    # used to do `callback.__name__` -- partials don't have one.
    holder.add_listener(cb)
    assert len(holder.observers.snapshot()) == 1


class _SlottedOwner:
    """Bound methods of this class can't be WeakMethod'd: __slots__ without
    __weakref__ makes the instance non-weak-referenceable."""
    __slots__ = ("calls",)

    def __init__(self):
        self.calls = 0

    def method(self, *args, **kwargs):
        self.calls += 1


def check_slots_owner_connect_falls_back_strong():
    owner = _SlottedOwner()

    # Fixture sanity: this owner really is non-weak-referenceable.
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
    """Bare object exposing exactly what launch_backend touches before the
    path checks (start_server + server.port)."""
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
            pass  # expected: clean validation error before any Popen
        except TypeError as e:
            raise AssertionError(
                f"launch_backend({bad_path!r}) fed a bad value into os.path.exists: {e}"
            )
        else:
            raise AssertionError(
                f"launch_backend({bad_path!r}) did not raise -- would Popen a garbage command"
            )


def check_get_own_key_resolves_via_get_input():
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


def main() -> None:
    assert threading.current_thread() is threading.main_thread()
    check_trigger_signal_kwargs_and_single_shot()
    check_eventholder_partial_dedupe_no_crash()
    check_slots_owner_connect_falls_back_strong()
    check_launch_backend_path_validation()
    check_get_own_key_resolves_via_get_input()
    print("PASS: scenario_plugin_signal_lows")


if __name__ == "__main__":
    main()
