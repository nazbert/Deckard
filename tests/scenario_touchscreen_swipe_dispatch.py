"""
Regression test for SD+ touchscreen swipe dispatch.

A touchscreen DRAG event travels the whole pipeline into
ActionBase.event_callback, whose compatibility mapping must reach a legacy
action's on_key_down. The same mapping carries a strip tap and the key events.
"""

# A real DeckController over a fake SD+ fires the callbacks.
import fixtures

from StreamDeck.Devices.StreamDeck import TouchscreenEventType

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionBase import ActionBase


class LegacyAction(ActionBase):
    """A plain deprecated ActionBase with only on_key_down and on_key_up
    overridden, which is the shape of every shipped plugin action."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.down_count = 0
        self.up_count = 0

    def on_key_down(self):
        self.down_count += 1

    def on_key_up(self):
        self.up_count += 1


class OverridingLegacyAction(ActionBase):
    """A legacy action that reroutes events itself. Its event_callback
    override must keep receiving the raw drag events."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_events = []

    def event_callback(self, event, data=None):
        self.seen_events.append(event)


def attach(page, action_cls, ident):
    """Builds a legacy action and registers it on page for ident the way
    load_action_objects does."""
    action = action_cls(
        action_id="test::legacy",
        action_name="LegacyStub",
        deck_controller=None,  # set below, as ActionCore's setters do
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

        # 1. A left swipe reaches the legacy on_key_down.
        deck.fire_touchscreen_event(
            TouchscreenEventType.DRAG,
            {"x": 620, "y": 50, "x_out": 120, "y_out": 50},
        )
        assert fixtures.wait_until(lambda: legacy.down_count == 1, timeout=3.0), (
            "DRAG_LEFT never reached the legacy action's on_key_down -- "
            "ActionBase.event_callback compat mapping drops touchscreen drags"
        )

        # 2. A right swipe reaches the legacy on_key_down again.
        deck.fire_touchscreen_event(
            TouchscreenEventType.DRAG,
            {"x": 120, "y": 50, "x_out": 620, "y_out": 50},
        )
        assert fixtures.wait_until(lambda: legacy.down_count == 2, timeout=3.0), (
            "DRAG_RIGHT never reached the legacy action's on_key_down"
        )

        # 3. The control. The overriding action saw both raw drag events, so
        # delivery reaches the action layer and only the compatibility
        # mapping can lose them.
        assert fixtures.wait_until(
            lambda: observer.seen_events.count(Input.Touchscreen.Events.DRAG_LEFT) == 1
            and observer.seen_events.count(Input.Touchscreen.Events.DRAG_RIGHT) == 1,
            timeout=3.0,
        ), f"drag events must reach overriding legacy actions, saw {observer.seen_events}"

        # 4. A strip tap over dial 0 fires that dial's legacy action,
        # through the SHORT_TOUCH_PRESS mapping.
        dial_legacy = attach(page, LegacyAction, Input.Dial("0"))
        dial_legacy.deck_controller = controller
        deck.fire_touchscreen_event(TouchscreenEventType.SHORT, {"x": 50, "y": 50})
        assert fixtures.wait_until(lambda: dial_legacy.down_count == 1, timeout=3.0), (
            "SHORT tap over dial 0 never reached the dial's legacy action "
            "(SHORT_TOUCH_PRESS compat mapping missing)"
        )

        # 5. The key compatibility table is intact, so Key.DOWN lands.
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
