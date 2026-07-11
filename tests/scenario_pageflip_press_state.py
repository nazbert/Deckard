"""
Regression test for issue #103: a button that flips pages must not start
"pressed" on the new page.

press_state lives on the ControllerKey, which init_inputs() creates once and
every page load reuses -- load_page() rebuilds the key's *states* but never
touched the pressed flag. A key that triggers a page change is still
physically down while the new page renders, so every new-page composite went
through is_pressed() -> shrink_image(), and the release's repaint can lose
the enqueue race against a loader render that read press_state=True just
before the UP landed -- leaving the new page's key stuck "pressed" until an
unrelated event repainted it.

The fix resets press_state on every ControllerKey inside load_page, after the
early-outs and BEFORE the generation bump: renders read config_gen at the
start of update() and press_state later (at composite time), so any render
stamped with the new generation is guaranteed to compose unpressed.

Deterministic seam: fire a fake-deck key DOWN (synchronous -- the callback
runs on the calling thread), load a different page while the key is still
"held", and assert the pressed flag is already cleared the instant load_page
returns. Then release and make sure the gesture bookkeeping still completed
(down_start_time cleared, press_state still False). With the fix reverted the
first assertion fails: press_state stays True across the switch.
"""
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

        # Physical DOWN on key 0 ("0x0"), synchronous like the real reader
        # thread's callback. The page is action-free, so the pool dispatch is
        # a no-op -- the scenario stands in for ChangePage by calling
        # load_page itself while the key is held.
        deck.fire_key_event(0, True)
        assert key.press_state is True, "DOWN must set press_state"
        assert key.down_start_time is not None, "DOWN must start the gesture clock"

        seed_path = fixtures.seed_page("PressStateTarget")
        page = gl.page_manager.get_page(seed_path, controller)
        controller.load_page(page)

        # The reset is synchronous, before the gen bump: by the time
        # load_page returns, no render stamped with the new generation can
        # compose this key as pressed.
        assert key.press_state is False, (
            "press_state survived load_page -- the new page's key renders "
            "shrunk/'pressed' (issue #103)"
        )
        assert key.is_pressed() is False
        # The physical gesture itself must NOT be cancelled by the reset:
        # the release still needs to classify (SHORT_UP vs HOLD_STOP) and
        # dispatch to the DOWN-time actions (issue #107).
        assert key.down_start_time is not None, (
            "load_page must reset only the visual press state, not the "
            "gesture bookkeeping"
        )

        # Release: stays unpressed, gesture completes cleanly.
        deck.fire_key_event(0, False)
        assert key.press_state is False
        assert key.down_start_time is None, "UP must close the gesture"

        print("PASS: press_state reset synchronously on page load while key held")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_pageflip_press_state")


if __name__ == "__main__":
    main()
