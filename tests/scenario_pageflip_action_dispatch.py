"""
Regression test for issue #107 (upstream #475): with "Change Page" +
"Run Command" on one button, the command fired only once.

Mechanism (all verified against the real code paths): ControllerKey used to
resolve its target actions at *dispatch* time -- get_active_state() taken
fresh at the UP event, and ControllerInputState.get_own_actions() reading
deck_controller.active_page when the action-pool worker actually runs.
ChangePage's on_key_down calls load_page() synchronously on that pool, which
swaps active_page immediately -- so the gesture's SHORT_UP/UP always resolved
against the NEW page's action objects. Two consequences:

  * the old page's actions never received the release for a DOWN they DID
    receive (RunCommand's `registered_down` latch -- the plugin's own
    workaround, set on DOWN and cleared only on UP -- jammed shut, so every
    later DOWN returned early: "runs only once");
  * the new page's same-position actions received a spurious SHORT_UP/UP for
    a press that was never theirs (the very defect that workaround was
    written against).

The fix snapshots the state + resolved action objects at key DOWN and
dispatches every event of the gesture (DOWN, HOLD_START, HOLD_STOP/SHORT_UP,
UP) to that snapshot, regardless of page swaps in between.

This scenario reproduces the exact upstream setup with stub ActionCore
objects injected into two real Pages on a fake deck: a ChangePage-alike
(loads page B on DOWN, from the action pool, like the real plugin) and a
RunCommand-alike (the plugin's literal latch semantics) side by side on one
key of page A, plus a recorder on page B's same key to catch bleed. Without
the fix the gesture tail lands on page B's recorder, the latch never clears,
and the second press runs nothing.
"""
import fixtures
import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

DOWN = Input.Key.Events.DOWN
SHORT_UP = Input.Key.Events.SHORT_UP
UP = Input.Key.Events.UP


class RecordingAction(ActionCore):
    """Minimal ActionCore that records every raw event it is dispatched."""

    def __init__(self, tag: str, deck_controller, page, input_ident):
        super().__init__(
            action_id=f"test::{tag}", action_name=tag,
            deck_controller=deck_controller, page=page, plugin_base=None,
            state=0, input_ident=input_ident,
        )
        self.tag = tag
        self.received: list = []

    def _raw_event_callback(self, event, data=None):
        self.received.append(event)


class ChangePageAction(RecordingAction):
    """Mirrors com_core447_DeckPlugin's ChangePage: on_key_down loads the
    target page synchronously on the action pool."""

    def __init__(self, target_page, **kwargs):
        super().__init__(**kwargs)
        self.target_page = target_page

    def _raw_event_callback(self, event, data=None):
        super()._raw_event_callback(event, data)
        if event == DOWN:
            self.deck_controller.load_page(self.target_page)


class RunCommandLikeAction(RecordingAction):
    """Mirrors com_core447_OSPlugin's RunCommand latch verbatim: DOWN is
    swallowed while registered_down is set; only UP clears it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.registered_down = False
        self.run_count = 0

    def _raw_event_callback(self, event, data=None):
        super()._raw_event_callback(event, data)
        if event == DOWN:
            if self.registered_down:
                return
            self.registered_down = True
            self.run_count += 1  # the "command"
        elif event == UP:
            self.registered_down = False


def inject(page, ident: Input.Key, actions: list) -> None:
    """Places stub action objects where get_all_actions_for_input reads:
    action_objects[input_type][json_identifier][state][index]."""
    per_state = page.action_objects.setdefault(ident.input_type, {}).setdefault(ident.json_identifier, {})
    per_state[0] = {i: a for i, a in enumerate(actions)}


def main() -> None:
    fixtures.start_watchdog(45, label="scenario_pageflip_action_dispatch")
    controller = fixtures.make_headless_controller(serial="dispatch-1")
    try:
        # Generous hold threshold so pool latency can never reclassify the
        # taps below as holds.
        controller.hold_time = 10.0

        deck = fixtures.raw_deck(controller)
        ident = Input.Key("0x0")

        page_a = controller.active_page  # "Main", loaded at construction
        seed_b = fixtures.seed_page("FlipTarget")
        page_b = gl.page_manager.get_page(seed_b, controller)
        assert page_a is not None and page_b is not page_a

        change_action = ChangePageAction(
            target_page=page_b, tag="change_page",
            deck_controller=controller, page=page_a, input_ident=ident)
        run_action = RunCommandLikeAction(
            tag="run_command",
            deck_controller=controller, page=page_a, input_ident=ident)
        bleed_recorder = RecordingAction(
            tag="page_b_recorder",
            deck_controller=controller, page=page_b, input_ident=ident)

        inject(page_a, ident, [change_action, run_action])
        inject(page_b, ident, [bleed_recorder])

        # ---- Press 1: DOWN flips the page mid-gesture ---- #
        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: DOWN in run_action.received), \
            "DOWN never reached the old page's RunCommand-alike"
        assert fixtures.wait_until(lambda: controller.active_page is page_b), \
            "ChangePage-alike never flipped the page"
        assert run_action.run_count == 1

        deck.fire_key_event(0, False)
        assert fixtures.wait_until(lambda: UP in run_action.received), (
            "UP was not delivered to the DOWN-time actions: the page flip "
            "redirected the gesture tail to the new page (issue #107) -- "
            f"run_action saw {run_action.received}"
        )
        assert SHORT_UP in run_action.received, \
            f"SHORT_UP missing from the DOWN-time actions: {run_action.received}"
        assert UP in change_action.received, \
            f"UP missing on the ChangePage-alike: {change_action.received}"
        assert run_action.registered_down is False, \
            "the RunCommand latch must be cleared by the UP"
        assert bleed_recorder.received == [], (
            "the new page's action received part of a gesture that started "
            f"on the old page: {bleed_recorder.received}"
        )

        # ---- Back to page A, press 2: the command must run again ---- #
        controller.load_page(page_a)
        assert fixtures.wait_until(lambda: controller.active_page is page_a)

        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: run_action.received.count(DOWN) == 2), \
            "second DOWN never reached the RunCommand-alike"
        assert run_action.run_count == 2, (
            "the command did not run on the second press -- the latch from "
            "press 1 was never cleared (upstream #475's 'fires only once')"
        )
        assert fixtures.wait_until(lambda: controller.active_page is page_b)
        deck.fire_key_event(0, False)
        assert fixtures.wait_until(lambda: run_action.received.count(UP) == 2), \
            "second UP lost"
        assert bleed_recorder.received == [], \
            f"gesture bleed onto page B on press 2: {bleed_recorder.received}"

        print("PASS: gesture events route to the DOWN-time action snapshot across page flips")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_pageflip_action_dispatch")


if __name__ == "__main__":
    main()
