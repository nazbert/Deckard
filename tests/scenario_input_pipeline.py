"""
Scenario: the input-event pipeline (press/dial/touch -> action delivery),
issue #58.

The injection API (FaultyFakeDeck.fire_key_event / fire_dial_event /
fire_touchscreen_event) was dead code at audit time: zero scenarios fired any
input, so the app's PRIMARY input path was entirely untested despite producing
multiple field bugs (dial starvation, HA dial reversal, rotated-deck remap).
This drives real input events through the whole chain --

  fire_*_event
    -> BetterDeck remapper_callback (physical -> logical via get_logical_index)
    -> DeckController.key/dial/touchscreen_event_callback (coords remap + ident)
    -> DeckController.event_callback -> get_input -> ControllerInput.event_callback
    -> ControllerInputState.own_actions_event_callback_threaded (action pool)
    -> ActionCore._raw_event_callback -> EventAssigner.call

-- against the REAL DeckController/Page/ControllerKey/ActionCore machinery,
with a minimal recording stub action injected via a stub plugin_manager (the
diag_wipe_contract.py stub pattern; kept local to this scenario).

Legs:
  1. key events (rotation 0): a physical press then release delivers DOWN,
     then SHORT_UP + UP, to the action on the pressed key -- and NOT to a
     different key's action.
  2. rotation 90 input remap: firing a PHYSICAL key delivers to the correct
     LOGICAL coordinates (the fire_key_event -> get_logical_index -> coords
     path). Plus a press-state-seeding check that is a regression net for
     f9533578 (reorder_physical_for_rotation direction): a physical key held
     at init seeds the CORRECT logical ControllerKey's press_state.
  3. dial events: turn CW/CCW deliver TURN_CW/TURN_CCW carrying the detent
     count; push+release deliver DOWN then SHORT_UP + UP.
  4. touchscreen events: a drag left->right delivers DRAG_RIGHT; right->left
     delivers DRAG_LEFT.
  5. hold timer: a key held past hold_time delivers HOLD_START (from the
     timer-wheel fire), and the release after that delivers HOLD_STOP, not
     SHORT_UP.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import json
import os
import threading
import types

import globals as gl
from fixtures import (
    make_headless_controller,
    raw_deck,
    start_watchdog,
    wait_until,
)

from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType
from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.PluginManager.EventAssigner import EventAssigner

FAKE_PLUGIN_BASE = types.SimpleNamespace(PATH="/tmp", backend=None)

# Process-wide record of delivered (input_ident, event, data) tuples, keyed so
# each leg can filter to its own input. Appended from the action pool thread;
# guarded by a lock. Cleared between legs.
_DELIVERED: list = []
_DELIVERED_LOCK = threading.Lock()


def _record(ident_str, event, data):
    with _DELIVERED_LOCK:
        _DELIVERED.append((ident_str, event, data))


def _delivered_events(ident_str=None):
    with _DELIVERED_LOCK:
        if ident_str is None:
            return [(e, d) for (i, e, d) in _DELIVERED]
        return [(e, d) for (i, e, d) in _DELIVERED if i == ident_str]


def _reset_delivered():
    with _DELIVERED_LOCK:
        _DELIVERED.clear()


class RecordingAction(ActionCore):
    """Registers one EventAssigner per input event of interest, each recording
    its (ident, event, data) into the shared log. Everything else is stubbed to
    the ActionCore no-op contract (see diag_wipe_contract.py)."""

    # Every event this action listens for, by input type. Each becomes its own
    # EventAssigner (default_events=[event]) so _raw_event_callback finds it.
    _KEY_EVENTS = [
        Input.Key.Events.DOWN, Input.Key.Events.UP, Input.Key.Events.SHORT_UP,
        Input.Key.Events.HOLD_START, Input.Key.Events.HOLD_STOP,
    ]
    _DIAL_EVENTS = [
        Input.Dial.Events.DOWN, Input.Dial.Events.UP, Input.Dial.Events.SHORT_UP,
        Input.Dial.Events.HOLD_START, Input.Dial.Events.HOLD_STOP,
        Input.Dial.Events.TURN_CW, Input.Dial.Events.TURN_CCW,
    ]
    _TOUCH_EVENTS = [
        Input.Touchscreen.Events.DRAG_LEFT, Input.Touchscreen.Events.DRAG_RIGHT,
    ]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        ident_str = self.input_ident.json_identifier
        input_type = self.input_ident.input_type
        if input_type == Input.Key.input_type:
            events = self._KEY_EVENTS
        elif input_type == Input.Dial.input_type:
            events = self._DIAL_EVENTS
        else:
            events = self._TOUCH_EVENTS

        for ev in events:
            # A fresh closure per event so the recorded event is the right one.
            def make_cb(event=ev, ident=ident_str):
                def cb(data=None):
                    _record(ident, event, data)
                return cb
            self.add_event_assigner(EventAssigner(
                id=f"rec_{ident_str}_{ev.string_name}",
                ui_label=ev.string_name,
                callback=make_cb(),
                default_events=[ev],
            ))

    def load_event_overrides(self):
        pass

    def load_initial_generative_ui(self):
        pass

    def on_ready(self):
        pass


class _Holder:
    def get_is_compatible(self):
        return True

    def init_and_get_action(self, deck_controller, page, state, input_ident):
        return RecordingAction(
            action_id="dev_test_RecordingAction", action_name="RecordingAction",
            deck_controller=deck_controller, page=page,
            plugin_base=FAKE_PLUGIN_BASE, state=state, input_ident=input_ident,
        )


class _PluginManager:
    def get_action_holder_from_id(self, action_id):
        return _Holder() if action_id == "dev_test_RecordingAction" else None

    def get_plugin_id_from_action_id(self, action_id):
        return "dev_test"

    def get_is_plugin_out_of_date(self, plugin_id):
        return False


def _seed_page(name, action_map):
    """action_map: {input_type: {json_ident: True}} -- each gets a
    RecordingAction on state 0. input_type is one of 'keys'/'dials'/
    'touchscreens'."""
    page = {"keys": {}, "dials": {}, "touchscreens": {}}
    for input_type, idents in action_map.items():
        for ident in idents:
            page[input_type][ident] = {"states": {"0": {
                "actions": [{"id": "dev_test_RecordingAction", "settings": {}}],
            }}}
    path = os.path.join(gl.DATA_PATH, "pages", f"{name}.json")
    with open(path, "w") as f:
        json.dump(page, f)
    return path


def _load_page_and_wait(controller, path):
    page = gl.page_manager.get_page(path, controller)
    controller.load_page(page, allow_reload=True)
    # The actions' event assigners register in RecordingAction.__init__, which
    # runs during load (add_action_object_from_holder). Wait until the page's
    # actions are actually present so a fired event has somewhere to land.
    wait_until(lambda: page.action_objects, timeout=3)
    return page


def test_key_events_rotation_0() -> int:
    _reset_delivered()
    controller = make_headless_controller(serial="input-key0")
    try:
        deck = raw_deck(controller)
        # Two keys with actions so we can prove delivery goes to the RIGHT key.
        _load_page_and_wait(controller, _seed_page(
            "KeyPage0", {"keys": {"0x0": True, "1x0": True}}))

        # Physical key 0 == logical/coords 0x0 at rotation 0.
        deck.fire_key_event(0, True)   # press
        if not wait_until(lambda: (Input.Key.Events.DOWN, None) in _delivered_events("0x0"), timeout=3):
            print("FAIL(1): key DOWN was never delivered to the pressed key 0x0")
            return 1
        deck.fire_key_event(0, False)  # release (short)
        if not wait_until(lambda: (Input.Key.Events.SHORT_UP, None) in _delivered_events("0x0"), timeout=3):
            print("FAIL(1): key SHORT_UP was never delivered on release")
            return 1
        if not wait_until(lambda: (Input.Key.Events.UP, None) in _delivered_events("0x0"), timeout=3):
            print("FAIL(1): key UP was never delivered on release")
            return 1

        # Isolation: the OTHER key's action must have received nothing.
        if _delivered_events("1x0"):
            print(f"FAIL(1): events leaked to the wrong key 1x0: "
                  f"{_delivered_events('1x0')}")
            return 1
        print("PASS: key press/release delivers DOWN + SHORT_UP + UP to the "
              "pressed key only")
        return 0
    finally:
        fixtures.teardown(controller)


def test_rotation_90_remap() -> int:
    _reset_delivered()
    controller = make_headless_controller(serial="input-key90")
    try:
        deck = raw_deck(controller)
        better = controller.deck  # the BetterDeck wrapper

        # --- press-state seeding (regression net for f9533578) ---
        # A physical key held at init must seed the CORRECT logical
        # ControllerKey.press_state under rotation 90. reorder_physical_for_
        # rotation's contract: key_states()[get_logical_index(p)] == raw[p].
        raw_states = [False] * deck.key_count()
        raw_states[3] = True  # physical key 3 held
        deck.key_states = lambda: list(raw_states)
        better.set_rotation(90)
        logical_of_3 = better.get_logical_index(3)
        ks = better.key_states()
        pressed_logical = [i for i, v in enumerate(ks) if v]
        if pressed_logical != [logical_of_3]:
            print(f"FAIL(2): rotation-90 press-state seeding is scrambled -- "
                  f"physical key 3 held should light logical {logical_of_3}, "
                  f"but key_states() reports {pressed_logical} (f9533578 "
                  f"regression: reorder_physical_for_rotation direction)")
            return 1

        # --- live input remap through the pipeline ---
        # Reset the held state, then REBUILD the inputs so their identifiers
        # match the rotation. In production the rotation is fixed at __init__
        # (BetterDeck(deck, rotation) from deck settings) and init_inputs()
        # runs after; here we set rotation post-construction, so we re-run
        # init_inputs() to get the same consistent state -- otherwise the
        # inputs keep their rotation-0 identifiers while key_event_callback
        # routes with the rotation-90 remap (a harness-only mismatch, not a
        # product bug).
        raw_states[3] = False
        controller.init_inputs()
        all_idents = [k.identifier.json_identifier for k in controller.inputs[Input.Key]]
        _load_page_and_wait(controller, _seed_page(
            "KeyPage90", {"keys": {ident: True for ident in all_idents}}))

        # Expected delivery coords for each physical key at rotation 90,
        # computed from the SAME primitives the pipeline uses, but assembled
        # independently (so a bug in the pipeline can't make this agree with
        # it): logical = get_logical_index(p); coords via the RAW deck layout
        # Index_To_Coords (cols from deck.key_layout(), NOT the BetterDeck's
        # rotated layout -- key_event_callback passes the raw deck); then x/y
        # swapped because rotation % 180 != 0.
        rows, cols = deck.key_layout()  # raw [2,4] -> cols=4
        for physical in range(deck.key_count()):
            logical = better.get_logical_index(physical)
            cx, cy = logical % cols, logical // cols
            expected_ident = f"{cy}x{cx}"  # swapped for rotation 90

            _reset_delivered()
            deck.fire_key_event(physical, True)
            if not wait_until(lambda ei=expected_ident: (Input.Key.Events.DOWN, None) in _delivered_events(ei), timeout=3):
                print(f"FAIL(2): physical key {physical} at rotation 90 did "
                      f"not deliver DOWN to the expected logical coords "
                      f"{expected_ident}; delivered to "
                      f"{[i for (i, e, d) in _DELIVERED]}")
                deck.fire_key_event(physical, False)
                return 1
            # Nothing must land on any OTHER ident.
            wrong = [i for (i, e, d) in _DELIVERED if i != expected_ident]
            if wrong:
                print(f"FAIL(2): physical key {physical} leaked to wrong "
                      f"coords {set(wrong)} (expected only {expected_ident})")
                deck.fire_key_event(physical, False)
                return 1
            # Release AND drain: the release delivers SHORT_UP + UP on the
            # action pool asynchronously; wait for it to land before the next
            # iteration's _reset_delivered(), or it would pollute that window.
            deck.fire_key_event(physical, False)
            wait_until(lambda ei=expected_ident: (Input.Key.Events.UP, None) in _delivered_events(ei), timeout=3)

        print("PASS: rotation-90 input remap delivers each physical key to its "
              "correct logical coords; press-state seeding is not scrambled")
        return 0
    finally:
        fixtures.teardown(controller)


def test_dial_events() -> int:
    _reset_delivered()
    controller = make_headless_controller(serial="input-dial")
    try:
        deck = raw_deck(controller)
        _load_page_and_wait(controller, _seed_page(
            "DialPage", {"dials": {"0": True}}))

        # Turn CW by 3 detents: TURN_CW with ticks=3.
        deck.fire_dial_event(0, DialEventType.TURN, 3)
        if not wait_until(lambda: any(e == Input.Dial.Events.TURN_CW and (d or {}).get("ticks") == 3
                                      for (e, d) in _delivered_events("0")), timeout=3):
            print("FAIL(3): dial TURN_CW (ticks=3) not delivered")
            return 1
        # Turn CCW by 2 detents (negative value): TURN_CCW with ticks=2.
        deck.fire_dial_event(0, DialEventType.TURN, -2)
        if not wait_until(lambda: any(e == Input.Dial.Events.TURN_CCW and (d or {}).get("ticks") == 2
                                      for (e, d) in _delivered_events("0")), timeout=3):
            print("FAIL(3): dial TURN_CCW (ticks=2) not delivered")
            return 1

        # Push + release (short): DOWN then SHORT_UP + UP.
        deck.fire_dial_event(0, DialEventType.PUSH, True)
        if not wait_until(lambda: (Input.Dial.Events.DOWN, None) in _delivered_events("0"), timeout=3):
            print("FAIL(3): dial DOWN not delivered on push")
            return 1
        deck.fire_dial_event(0, DialEventType.PUSH, False)
        if not wait_until(lambda: (Input.Dial.Events.SHORT_UP, None) in _delivered_events("0"), timeout=3):
            print("FAIL(3): dial SHORT_UP not delivered on release")
            return 1
        if not wait_until(lambda: (Input.Dial.Events.UP, None) in _delivered_events("0"), timeout=3):
            print("FAIL(3): dial UP not delivered on release")
            return 1
        print("PASS: dial turn (CW/CCW with detents) and push/release deliver "
              "the right events")
        return 0
    finally:
        fixtures.teardown(controller)


def test_touchscreen_events() -> int:
    _reset_delivered()
    controller = make_headless_controller(serial="input-touch")
    try:
        deck = raw_deck(controller)
        _load_page_and_wait(controller, _seed_page(
            "TouchPage", {"touchscreens": {"sd-plus": True}}))

        # Drag left -> right: x < x_out  => DRAG_RIGHT.
        deck.fire_touchscreen_event(
            TouchscreenEventType.DRAG, {"x": 10, "y": 5, "x_out": 700, "y_out": 5})
        if not wait_until(lambda: (Input.Touchscreen.Events.DRAG_RIGHT, None) in _delivered_events("sd-plus"), timeout=3):
            print("FAIL(4): touchscreen DRAG_RIGHT not delivered for a "
                  "left-to-right drag")
            return 1

        # Drag right -> left: x > x_out => DRAG_LEFT.
        deck.fire_touchscreen_event(
            TouchscreenEventType.DRAG, {"x": 700, "y": 5, "x_out": 10, "y_out": 5})
        if not wait_until(lambda: (Input.Touchscreen.Events.DRAG_LEFT, None) in _delivered_events("sd-plus"), timeout=3):
            print("FAIL(4): touchscreen DRAG_LEFT not delivered for a "
                  "right-to-left drag")
            return 1
        print("PASS: touchscreen drag delivers DRAG_RIGHT / DRAG_LEFT by "
              "direction")
        return 0
    finally:
        fixtures.teardown(controller)


def test_hold_timer() -> int:
    _reset_delivered()
    controller = make_headless_controller(serial="input-hold")
    try:
        deck = raw_deck(controller)
        # Shrink the hold time so the timer-wheel fire is quick + deterministic
        # (still real-time, but far under the watchdog).
        controller.hold_time = 0.15
        _load_page_and_wait(controller, _seed_page(
            "HoldPage", {"keys": {"0x0": True}}))

        # Press and hold past hold_time: the timer wheel fires on_hold_timer_end
        # -> HOLD_START.
        deck.fire_key_event(0, True)
        if not wait_until(lambda: (Input.Key.Events.HOLD_START, None) in _delivered_events("0x0"), timeout=3):
            print("FAIL(5): HOLD_START was never delivered after holding past "
                  "hold_time (the hold timer never fired into the pipeline)")
            return 1

        # Release AFTER the hold elapsed: HOLD_STOP, not SHORT_UP.
        deck.fire_key_event(0, False)
        if not wait_until(lambda: (Input.Key.Events.HOLD_STOP, None) in _delivered_events("0x0"), timeout=3):
            print("FAIL(5): HOLD_STOP not delivered on release after a hold")
            return 1
        if (Input.Key.Events.SHORT_UP, None) in _delivered_events("0x0"):
            print("FAIL(5): a release after the hold elapsed wrongly delivered "
                  "SHORT_UP as well as HOLD_STOP")
            return 1
        print("PASS: holding past hold_time delivers HOLD_START; the later "
              "release delivers HOLD_STOP not SHORT_UP")
        return 0
    finally:
        fixtures.teardown(controller)


def main() -> int:
    start_watchdog(75, "input_pipeline")
    gl.plugin_manager = _PluginManager()
    rc = test_key_events_rotation_0()
    rc |= test_rotation_90_remap()
    rc |= test_dial_events()
    rc |= test_touchscreen_events()
    rc |= test_hold_timer()
    if rc == 0:
        print("PASS: scenario_input_pipeline")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
