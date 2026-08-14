"""
Regression test for a page-flipping button that starts pressed on the new page.

load_page resets press_state on every ControllerKey, after the early-outs and
before the generation bump.
"""

# A render reads config_gen at the start of update() and press_state later, so
# a render stamped with the new generation always composes the key unpressed.
import fixtures
import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_pageflip_press_state")
    controller = fixtures.make_headless_controller(serial="pressstate-1")
    try:
        deck = fixtures.raw_deck(controller)
        key = controller.get_input(Input.Key("0x0"))
        assert key is not None, "expected a ControllerKey at 0x0 on the 2x4 fake deck"
        assert key.press_state is False, "key must start unpressed"

        # Physical DOWN on key 0, synchronous like the real reader thread's
        # callback. The page carries no actions, so the pool dispatch does
        # nothing. This check stands in for ChangePage and calls load_page
        # itself while the key is held.
        deck.fire_key_event(0, True)
        assert key.press_state is True, "DOWN must set press_state"
        assert key.down_start_time is not None, "DOWN must start the gesture clock"

        seed_path = fixtures.seed_page("PressStateTarget")
        page = gl.page_manager.get_page(seed_path, controller)
        controller.load_page(page)

        # The reset is synchronous and happens before the gen bump. Once
        # load_page returns, no render stamped with the new generation can
        # compose this key as pressed.
        assert key.press_state is False, (
            "press_state survived load_page -- the new page's key renders "
            "shrunk/'pressed'"
        )
        assert key.is_pressed() is False
        # The reset must not cancel the physical gesture. The release still
        # has to classify itself and dispatch to the DOWN-time actions.
        assert key.down_start_time is not None, (
            "load_page must reset only the visual press state, not the "
            "gesture bookkeeping"
        )

        # The release stays unpressed and the gesture completes.
        deck.fire_key_event(0, False)
        assert key.press_state is False
        assert key.down_start_time is None, "UP must close the gesture"

        print("PASS: press_state reset synchronously on page load while key held")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_pageflip_press_state")


if __name__ == "__main__":
    main()
