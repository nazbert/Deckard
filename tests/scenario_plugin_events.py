"""
Unit-tier scenario for the plugin event/callback layer (issues #33, #37,
#36): the pieces between an EventHolder/InputEvent firing and a plugin
callback actually running are deck-independent, so this exercises them
directly -- no FakeDeck, no controller.

Covers:
  (a) #33 -- a raising observer (sync AND `async def`) dispatched through
      event_dispatch produces a logged ERROR that carries the exception
      type, message and a traceback, not just the bare one-liner.
  (b) #37 -- InputBases (KeyAction/DialAction/TouchScreenAction) default
      event assigners survive real delivery: _raw_event_callback forwards
      one positional data arg and the documented no-arg on_* handlers
      (including subclass overrides) run instead of TypeError-ing.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from loguru import logger as log

from fixtures import wait_until
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager import event_dispatch
from src.backend.PluginManager.InputBases import DialAction, KeyAction, TouchScreenAction


class _LogCapture:
    """Attaches a capturing loguru sink for the duration of a `with` block.
    Loguru hands text sinks the fully formatted message -- including the
    formatted traceback whenever the record carries exception info -- so
    asserting on the joined text is enough to prove a traceback was logged.
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


# ===================================================================== #
# (a) #33 -- dispatch logs the observer's traceback
# ===================================================================== #

def check_raising_sync_observer_logs_traceback():
    def exploding_observer(*args, **kwargs):
        raise RuntimeError("sync-boom-marker")

    with _LogCapture(level="ERROR") as capture:
        event_dispatch.dispatch([exploding_observer], ("evt",), {}, label="test::SyncEvent")
        assert wait_until(lambda: "could not be called" in capture.text(), timeout=5.0), (
            "dispatch never logged the failing sync observer"
        )
        # The batch runs on the shared dispatcher thread; the one-liner and
        # its exception block arrive as a single record, so once the marker
        # text is visible the assertion set below is race-free.
        text = capture.text()

    assert "exploding_observer" in text, text
    assert "test::SyncEvent" in text, text
    assert "RuntimeError" in text, f"exception type missing from log: {text!r}"
    assert "sync-boom-marker" in text, f"exception message missing from log: {text!r}"
    assert "Traceback" in text, f"no traceback in log -- #33 regressed: {text!r}"


def check_raising_async_observer_logs_traceback():
    # Every real EventHolder observer in the plugin ecosystem is an
    # `async def` -- this is the branch that mattered most for #33.
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
    assert "Traceback" in text, f"no traceback in log -- #33 regressed: {text!r}"


def check_raising_observer_does_not_stop_batch():
    # Exception isolation was already there -- make sure adding the
    # traceback didn't disturb it: the observer after the raising one still
    # runs.
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


# ===================================================================== #
# (b) #37 -- InputBases event delivery reaches the no-arg handlers
# ===================================================================== #

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
    """Subclass with the documented no-arg override signatures -- delivery
    must keep working for exactly this shape."""

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
    # Drive the exact production delivery path -- _raw_event_callback is
    # what ControllerInput invokes, and unlike the real call site there is
    # no @log.catch here, so a TypeError fails the scenario loudly.
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
    # DRAG_LEFT was doubly broken: wrong arity AND wired to on_trigger
    # instead of on_touchscreen_drag_left.
    action._raw_event_callback(Input.Touchscreen.Events.DRAG_LEFT, {"x": 10})
    action._raw_event_callback(Input.Touchscreen.Events.DRAG_RIGHT, {"x": 90})
    assert action.calls == ["drag_left", "drag_right"], action.calls


def check_base_handlers_are_callable_noops():
    # The unsubclassed bases must also survive delivery -- their handlers
    # are documented no-ops, not crash sites.
    for cls, event in (
        (KeyAction, Input.Key.Events.SHORT_UP),
        (DialAction, Input.Dial.Events.HOLD_STOP),
        (TouchScreenAction, Input.Touchscreen.Events.DRAG_LEFT),
    ):
        action = cls(**_ACTION_KWARGS)
        action._raw_event_callback(event, {"some": "data"})


def main() -> None:
    check_raising_sync_observer_logs_traceback()
    check_raising_async_observer_logs_traceback()
    check_raising_observer_does_not_stop_batch()
    check_key_action_event_delivery()
    check_dial_action_event_delivery()
    check_touchscreen_action_event_delivery()
    check_base_handlers_are_callable_noops()
    print("PASS: scenario_plugin_events")


if __name__ == "__main__":
    main()
