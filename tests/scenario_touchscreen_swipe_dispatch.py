"""
Regression test for SD+ touchscreen swipe dispatch (#108, upstream #520).

Since the event-assigner rework (aadbaa59, in the 1.5.0 betas) touchscreen
DRAG events travel the whole pipeline -- reader callback ->
DeckController.touchscreen_event_callback -> ControllerTouchScreen
.event_callback -> own_actions_event_callback -> ActionCore
._raw_event_callback -> EventAssigner -- and then die in
ActionBase.event_callback: the backward-compat mapping there only handles
Key.DOWN/UP and Dial.DOWN/UP, so for every legacy ActionBase action (which
is what all real plugins are, incl. DeckPlugin's ChangePage) a swipe is a
silent no-op. The same rework commit also dropped the pre-existing
Dial.SHORT_TOUCH_PRESS -> on_key_down mapping, killing strip taps.

Asserts, over a REAL DeckController on a fake SD+ (FaultyFakeDeck fires the
callbacks exactly like the library reader thread does):
  1. a legacy action on the touchscreen gets on_key_down() for a left swipe,
  2. and for a right swipe,
  3. CONTROL: a legacy action that overrides event_callback receives the raw
     DRAG events (this always worked -- proves delivery, isolating the
     compat mapping as the break),
  4. a strip tap (TouchscreenEventType.SHORT over dial 0) reaches a legacy
     action on that dial as on_key_down (restores pre-rework behavior),
  5. key events still map (Key.DOWN -> on_key_down) -- compat table intact.
"""
import fixtures
from loguru import logger as log

from StreamDeck.Devices.StreamDeck import TouchscreenEventType

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionBase import ActionBase


class LegacyAction(ActionBase):
    """ChangePage-alike: plain deprecated ActionBase, only on_key_down/up
    overridden -- exactly the shape of every shipped plugin action."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.down_count = 0
        self.up_count = 0

    def on_key_down(self):
        self.down_count += 1

    def on_key_up(self):
        self.up_count += 1


class OverridingLegacyAction(ActionBase):
    """Legacy action that reroutes events itself (RunCommand-alike): its
    event_callback override must keep receiving the raw drag events."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_events = []

    def event_callback(self, event, data=None):
        self.seen_events.append(event)


def attach(page, action_cls, ident):
    """Builds a legacy action and registers it on `page` for `ident` the way
    load_action_objects would (action_objects[input_type][json_id][state])."""
    action = action_cls(
        action_id="test::legacy",
        action_name="LegacyStub",
        deck_controller=None,  # set right below, like ActionCore's setters do
        page=page,
        plugin_base=None,
        state=0,
        input_ident=ident,
    )
    page.action_objects.setdefault(ident.input_type, {}).setdefault(
        ident.json_identifier, {}
    ).setdefault(0, {})[len(page.action_objects[ident.input_type][ident.json_identifier][0])] = action
    return action


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_touchscreen_swipe_dispatch")

    controller = fixtures.make_headless_controller(serial="swipe-1")
    try:
        page = controller.active_page
        deck = fixtures.raw_deck(controller)
        touch_ident = Input.Touchscreen("sd-plus")

        assert controller.get_input(touch_ident) is not None, (
            "fake SD+ must expose a ControllerTouchScreen input"
        )

        legacy = attach(page, LegacyAction, touch_ident)
        legacy.deck_controller = controller
        observer = attach(page, OverridingLegacyAction, touch_ident)
        observer.deck_controller = controller

        # 1: left swipe (x > x_out) -> legacy on_key_down
        deck.fire_touchscreen_event(
            TouchscreenEventType.DRAG,
            {"x": 620, "y": 50, "x_out": 120, "y_out": 50},
        )
        assert fixtures.wait_until(lambda: legacy.down_count == 1, timeout=3.0), (
            "DRAG_LEFT never reached the legacy action's on_key_down -- "
            "ActionBase.event_callback compat mapping drops touchscreen drags (#108)"
        )

        # 2: right swipe (x < x_out) -> legacy on_key_down again
        deck.fire_touchscreen_event(
            TouchscreenEventType.DRAG,
            {"x": 120, "y": 50, "x_out": 620, "y_out": 50},
        )
        assert fixtures.wait_until(lambda: legacy.down_count == 2, timeout=3.0), (
            "DRAG_RIGHT never reached the legacy action's on_key_down (#108)"
        )

        # 3: control -- the overriding action saw both raw drag events, so
        # delivery up to the action layer works; the compat mapping is the
        # only place a default legacy action can lose them.
        assert fixtures.wait_until(
            lambda: observer.seen_events.count(Input.Touchscreen.Events.DRAG_LEFT) == 1
            and observer.seen_events.count(Input.Touchscreen.Events.DRAG_RIGHT) == 1,
            timeout=3.0,
        ), f"drag events must reach overriding legacy actions, saw {observer.seen_events}"

        # 4: strip tap over dial 0 -> that dial's legacy action fires
        # (SHORT_TOUCH_PRESS -> on_key_down, the mapping aadbaa59 removed).
        dial_legacy = attach(page, LegacyAction, Input.Dial("0"))
        dial_legacy.deck_controller = controller
        deck.fire_touchscreen_event(TouchscreenEventType.SHORT, {"x": 50, "y": 50})
        assert fixtures.wait_until(lambda: dial_legacy.down_count == 1, timeout=3.0), (
            "SHORT tap over dial 0 never reached the dial's legacy action "
            "(SHORT_TOUCH_PRESS compat mapping missing)"
        )

        # 5: key compat table intact -- Key.DOWN still lands.
        key_legacy = attach(page, LegacyAction, Input.Key("0x0"))
        key_legacy.deck_controller = controller
        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: key_legacy.down_count == 1, timeout=3.0), (
            "Key.DOWN no longer reaches legacy on_key_down -- compat regression"
        )
        deck.fire_key_event(0, False)
        assert fixtures.wait_until(lambda: key_legacy.up_count >= 1, timeout=3.0), (
            "Key.UP no longer reaches legacy on_key_up -- compat regression"
        )

        print("scenario_touchscreen_swipe_dispatch: PASS")
    finally:
        fixtures.teardown(controller)


if __name__ == "__main__":
    main()
