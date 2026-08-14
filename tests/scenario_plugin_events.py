"""
Unit-tier scenario for the plugin event and callback layer.

The pieces between an EventHolder firing and a plugin callback running are
deck-independent, so this drives them with no deck and no controller.
"""

# It covers observer error logging, InputBases delivery to the no-arg handlers,
# and the cross-plugin connect and disconnect APIs.
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from loguru import logger as log

import globals as gl
from fixtures import wait_until
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager import event_dispatch
from src.backend.PluginManager.EventHolder import EventHolder
from src.backend.PluginManager.InputBases import DialAction, KeyAction, TouchScreenAction
from src.backend.PluginManager.PluginBase import PluginBase


class _LogCapture:
    """Adds a capturing loguru sink for the with block.

    Loguru hands a text sink the fully formatted message, and that includes
    the formatted traceback when the record carries exception info. The
    joined text is therefore enough to prove a traceback was logged.
    """

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


# Dispatch logs the observer's traceback.

def check_raising_sync_observer_logs_traceback():
    def exploding_observer(*args, **kwargs):
        raise RuntimeError("sync-boom-marker")

    with _LogCapture(level="ERROR") as capture:
        event_dispatch.dispatch([exploding_observer], ("evt",), {}, label="test::SyncEvent")
        assert wait_until(lambda: "could not be called" in capture.text(), timeout=5.0), (
            "dispatch never logged the failing sync observer"
        )
        # The batch runs on the shared dispatcher thread. The one-liner and
        # its exception block arrive as one record, so the assertions below
        # are safe once the marker text appears.
        text = capture.text()

    assert "exploding_observer" in text, text
    assert "test::SyncEvent" in text, text
    assert "RuntimeError" in text, f"exception type missing from log: {text!r}"
    assert "sync-boom-marker" in text, f"exception message missing from log: {text!r}"
    assert "Traceback" in text, f"no traceback in log -- observer exceptions must log one: {text!r}"


def check_raising_async_observer_logs_traceback():
    # Every real EventHolder observer in the plugin ecosystem is an
    # async def, so this branch carries the ecosystem.
    async def exploding_coroutine_observer(*args, **kwargs):
        raise ValueError("async-boom-marker")

    with _LogCapture(level="ERROR") as capture:
        event_dispatch.dispatch([exploding_coroutine_observer], ("evt",), {}, label="test::AsyncEvent")
        assert wait_until(lambda: "could not be called" in capture.text(), timeout=5.0), (
            "dispatch never logged the failing async observer"
        )
        text = capture.text()

    assert "exploding_coroutine_observer" in text, text
    assert "ValueError" in text, f"exception type missing from log: {text!r}"
    assert "async-boom-marker" in text, f"exception message missing from log: {text!r}"
    assert "Traceback" in text, f"no traceback in log -- observer exceptions must log one: {text!r}"


def check_raising_observer_isolation():
    # The observer after a raising one still runs.
    ran = []

    def exploding(*args, **kwargs):
        raise RuntimeError("first observer dies")

    def survivor(*args, **kwargs):
        ran.append(args)

    with _LogCapture(level="ERROR"):
        event_dispatch.dispatch([exploding, survivor], ("evt",), {}, label="test::Isolation")
        assert wait_until(lambda: len(ran) == 1, timeout=5.0), (
            "observer after a raising one never ran -- batch isolation broke"
        )


# InputBases event delivery reaches the no-arg handlers.

_ACTION_KWARGS = dict(
    action_id="test::InputBases",
    action_name="InputBases test action",
    deck_controller=None,
    page=None,
    plugin_base=None,
    state=0,
    input_ident=None,
)


class _RecordingKeyAction(KeyAction):
    """Subclass with the documented no-arg override signatures. Delivery
    must keep working for this shape."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def on_key_down(self):
        self.calls.append("down")

    def on_key_up(self):
        self.calls.append("up")

    def on_key_hold_start(self):
        self.calls.append("hold_start")


class _RecordingDialAction(DialAction):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def on_dial_turn_cw(self):
        self.calls.append("turn_cw")

    def on_dial_turn_ccw(self):
        self.calls.append("turn_ccw")

    def on_dial_short_touch_press(self):
        self.calls.append("short_touch")


class _RecordingTouchScreenAction(TouchScreenAction):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def on_touchscreen_drag_left(self):
        self.calls.append("drag_left")

    def on_touchscreen_drag_right(self):
        self.calls.append("drag_right")


def check_key_action_event_delivery():
    action = _RecordingKeyAction(**_ACTION_KWARGS)
    # Drive the production delivery path. ControllerInput invokes
    # _raw_event_callback, and no @log.catch guards it here, so a TypeError
    # fails the scenario loudly.
    action._raw_event_callback(Input.Key.Events.DOWN, {"coords": (1, 2)})
    action._raw_event_callback(Input.Key.Events.UP, None)
    action._raw_event_callback(Input.Key.Events.HOLD_START, {"coords": (1, 2)})
    assert action.calls == ["down", "up", "hold_start"], action.calls


def check_dial_action_event_delivery():
    action = _RecordingDialAction(**_ACTION_KWARGS)
    action._raw_event_callback(Input.Dial.Events.TURN_CW, {"ticks": 1})
    action._raw_event_callback(Input.Dial.Events.TURN_CCW, {"ticks": -1})
    action._raw_event_callback(Input.Dial.Events.SHORT_TOUCH_PRESS, None)
    assert action.calls == ["turn_cw", "turn_ccw", "short_touch"], action.calls


def check_touchscreen_action_event_delivery():
    action = _RecordingTouchScreenAction(**_ACTION_KWARGS)
    # DRAG_LEFT must reach on_touchscreen_drag_left with the documented
    # arity.
    action._raw_event_callback(Input.Touchscreen.Events.DRAG_LEFT, {"x": 10})
    action._raw_event_callback(Input.Touchscreen.Events.DRAG_RIGHT, {"x": 90})
    assert action.calls == ["drag_left", "drag_right"], action.calls


def check_base_handlers_are_noops():
    # The unsubclassed bases must survive delivery. Their handlers are
    # documented no-ops.
    for cls, event in (
        (KeyAction, Input.Key.Events.SHORT_UP),
        (DialAction, Input.Dial.Events.HOLD_STOP),
        (TouchScreenAction, Input.Touchscreen.Events.DRAG_LEFT),
    ):
        action = cls(**_ACTION_KWARGS)
        action._raw_event_callback(event, {"some": "data"})


# Cross-plugin connect and disconnect target the right plugin.

PROVIDER_ID = "com_test_provider"
CONSUMER_ID = "com_test_consumer"
EVENT_SUFFIX = "VolumeChanged"
EVENT_ID = f"{PROVIDER_ID}::{EVENT_SUFFIX}"


class _StubPluginManager:
    """Only get_plugin_by_id is dereferenced on the paths under test."""

    def __init__(self, plugins: dict):
        self._plugins = plugins

    def get_plugin_by_id(self, plugin_id: str, include_disabled: bool = True):
        return self._plugins.get(plugin_id)


def make_plugin(plugin_id: str) -> PluginBase:
    """A PluginBase carrying only the state the event API reads.
    PluginBase.__init__ needs a manifest and assets on disk, which the
    connect and disconnect paths never touch."""
    plugin = PluginBase.__new__(PluginBase)
    plugin.event_holders = {}
    plugin.plugin_name = plugin_id
    plugin._plugin_id_cache = plugin_id  # short-circuits get_plugin_id()
    return plugin


def make_provider_and_consumer():
    provider = make_plugin(PROVIDER_ID)
    consumer = make_plugin(CONSUMER_ID)
    gl.plugin_manager = _StubPluginManager({PROVIDER_ID: provider, CONSUMER_ID: consumer})
    # Suffix construction must yield "<plugin_id>::<suffix>".
    holder = EventHolder(plugin_base=provider, event_id_suffix=EVENT_SUFFIX)
    assert holder.event_id == EVENT_ID, holder.event_id
    provider.add_event_holder(holder)
    return provider, consumer, holder


def check_connect_directly_reaches_target():
    provider, consumer, holder = make_provider_and_consumer()

    received = []
    consumer_side_callback = lambda *args, **kwargs: received.append(args)  # noqa: E731

    consumer.connect_to_event_directly(PROVIDER_ID, EVENT_ID, consumer_side_callback)

    assert consumer_side_callback in holder.observers.snapshot(), (
        "connect_to_event_directly did not attach to the target plugin's holder"
    )
    assert consumer.event_holders == {}, (
        "connect_to_event_directly grew state on the CALLING plugin"
    )

    # The connection must be live end to end, not only present in the
    # registry. trigger_event dispatches (event_id, *args) asynchronously.
    holder.trigger_event(42)
    assert wait_until(lambda: received == [(EVENT_ID, 42)], timeout=5.0), (
        f"connected callback never received the trigger: {received!r}"
    )


def check_connect_suffix_path():
    provider, consumer, holder = make_provider_and_consumer()

    # A suffix connect must resolve the full id before it indexes
    # event_holders, or the lookup runs with None.
    suffix_callback = lambda *args, **kwargs: None  # noqa: E731
    provider.connect_to_event(callback=suffix_callback, event_id_suffix=EVENT_SUFFIX)
    assert suffix_callback in holder.observers.snapshot(), (
        "suffix-based connect_to_event did not attach to the holder"
    )


def check_disconnect_suffix_symmetry():
    # disconnect_from_event accepts the same event_id_suffix as
    # connect_to_event, so a plugin that suffix-connected can also
    # suffix-disconnect instead of leaking the subscription.
    provider, consumer, holder = make_provider_and_consumer()

    suffix_callback = lambda *args, **kwargs: None  # noqa: E731
    provider.connect_to_event(callback=suffix_callback, event_id_suffix=EVENT_SUFFIX)
    assert suffix_callback in holder.observers.snapshot()

    provider.disconnect_from_event(callback=suffix_callback, event_id_suffix=EVENT_SUFFIX)
    assert suffix_callback not in holder.observers.snapshot(), (
        "suffix-based disconnect_from_event did not remove the subscription "
        "-- suffix connect/disconnect are asymmetric (subscription leaks)"
    )

    # The full-id positional call must keep working unchanged.
    provider.connect_to_event(callback=suffix_callback, event_id=EVENT_ID)
    assert suffix_callback in holder.observers.snapshot()
    provider.disconnect_from_event(EVENT_ID, suffix_callback)
    assert suffix_callback not in holder.observers.snapshot(), (
        "positional full-id disconnect_from_event regressed"
    )


def check_disconnect_directly_hits_target():
    provider, consumer, holder = make_provider_and_consumer()

    # Give the consumer its own holder under the same event id, with its own
    # listener. A detach from the calling plugin would touch this decoy.
    consumer_holder = EventHolder(plugin_base=consumer, event_id=EVENT_ID)
    consumer.add_event_holder(consumer_holder)
    decoy_callback = lambda *args, **kwargs: None  # noqa: E731
    consumer_holder.add_listener(decoy_callback)

    shared_callback = lambda *args, **kwargs: None  # noqa: E731
    consumer.connect_to_event_directly(PROVIDER_ID, EVENT_ID, shared_callback)
    assert shared_callback in holder.observers.snapshot()

    consumer.disconnect_from_event_directly(PROVIDER_ID, EVENT_ID, shared_callback)

    assert shared_callback not in holder.observers.snapshot(), (
        "disconnect_from_event_directly left the callback on the target plugin"
    )
    assert decoy_callback in consumer_holder.observers.snapshot(), (
        "disconnect_from_event_directly detached from the CALLING plugin's holder"
    )


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_plugin_events")
    check_raising_sync_observer_logs_traceback()
    check_raising_async_observer_logs_traceback()
    check_raising_observer_isolation()
    check_key_action_event_delivery()
    check_dial_action_event_delivery()
    check_touchscreen_action_event_delivery()
    check_base_handlers_are_noops()
    check_connect_directly_reaches_target()
    check_connect_suffix_path()
    check_disconnect_suffix_symmetry()
    check_disconnect_directly_hits_target()
    print("PASS: scenario_plugin_events")


if __name__ == "__main__":
    main()
