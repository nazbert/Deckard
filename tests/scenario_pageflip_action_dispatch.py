"""
Regression test for Change Page and Run Command on one button.

ControllerKey snapshots the state and the resolved action objects at key DOWN,
then dispatches every event of the gesture to that snapshot, whatever page
swaps happen in between.
"""

# Stub actions sit on two real Pages over a fake deck, with a recorder on page
# B's same key to catch bleed.
import os

import fixtures
import globals as gl

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

DOWN = Input.Key.Events.DOWN
SHORT_UP = Input.Key.Events.SHORT_UP
UP = Input.Key.Events.UP
HOLD_START = Input.Key.Events.HOLD_START


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
    """Mirrors ChangePage from com_core447_DeckPlugin. on_key_down loads the
    target page synchronously on the action pool."""

    def __init__(self, target_page, **kwargs):
        super().__init__(**kwargs)
        self.target_page = target_page

    def _raw_event_callback(self, event, data=None):
        super()._raw_event_callback(event, data)
        if event == DOWN:
            self.deck_controller.load_page(self.target_page)


class RaisingAction(RecordingAction):
    """Records, then raises on SHORT_UP and UP. One raiser must not starve
    its siblings in the dispatch loop."""

    def _raw_event_callback(self, event, data=None):
        super()._raw_event_callback(event, data)
        if event in (SHORT_UP, UP):
            raise RuntimeError("intentional test failure in action callback")


class RunCommandLikeAction(RecordingAction):
    """Mirrors the RunCommand latch from com_core447_OSPlugin. DOWN is
    swallowed while registered_down is set, and only UP clears it."""

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
    """Places stub action objects where get_all_actions_for_input reads them,
    at action_objects[input_type][json_identifier][state][index]."""
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

        # Press 1. DOWN flips the page mid-gesture.
        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: DOWN in run_action.received), \
            "DOWN never reached the old page's RunCommand-alike"
        assert fixtures.wait_until(lambda: controller.active_page is page_b), \
            "ChangePage-alike never flipped the page"
        assert run_action.run_count == 1

        deck.fire_key_event(0, False)
        assert fixtures.wait_until(lambda: UP in run_action.received), (
            "UP was not delivered to the DOWN-time actions: the page flip "
            "redirected the gesture tail to the new page -- "
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

        # Back to page A. On press 2 the command must run again.
        controller.load_page(page_a)
        assert fixtures.wait_until(lambda: controller.active_page is page_a)

        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: run_action.received.count(DOWN) == 2), \
            "second DOWN never reached the RunCommand-alike"
        assert run_action.run_count == 2, (
            "the command did not run on the second press -- the latch from "
            "press 1 was never cleared (the classic 'fires only once' latch)"
        )
        assert fixtures.wait_until(lambda: controller.active_page is page_b)
        deck.fire_key_event(0, False)
        assert fixtures.wait_until(lambda: run_action.received.count(UP) == 2), \
            "second UP lost"
        assert bleed_recorder.received == [], \
            f"gesture bleed onto page B on press 2: {bleed_recorder.received}"

        # Press 3. The origin page is evicted mid-gesture. The key handler
        # holds the pressed page only for the length of the callback, so the
        # origin page is evictable while the key is down. The dispatch loop
        # must skip the torn-down snapshot members and still serve a healthy
        # one. sentinel is detached from page A before the eviction, so
        # clear_action_objects never tears it down.
        sentinel = RecordingAction(
            tag="snapshot_sentinel",
            deck_controller=controller, page=page_a, input_ident=ident)
        inject(page_a, ident, [change_action, run_action, sentinel])

        controller.load_page(page_a)
        assert fixtures.wait_until(lambda: controller.active_page is page_a)

        deck.fire_key_event(0, True)
        assert fixtures.wait_until(lambda: run_action.received.count(DOWN) == 3)
        assert run_action.run_count == 3
        assert fixtures.wait_until(lambda: controller.active_page is page_b)

        # Evict page A through the real cache-budget path.
        old_max_pages = gl.page_manager.max_pages
        gl.page_manager.max_pages = 0
        # The sentinel leaves the page before the eviction.
        page_a.action_objects[ident.input_type][ident.json_identifier][0].pop(2)
        gl.page_manager.clear_old_cached_pages()
        gl.page_manager.max_pages = old_max_pages
        assert run_action._cleaned_up and change_action._cleaned_up, \
            "eviction should have torn the origin page's actions down"
        assert not sentinel._cleaned_up

        deck.fire_key_event(0, False)
        assert fixtures.wait_until(lambda: UP in sentinel.received), \
            "healthy snapshot member never got the UP after its siblings were torn down"
        assert SHORT_UP in sentinel.received
        assert run_action.received.count(UP) == 2, (
            "UP was dispatched into a torn-down action (clean_up already "
            f"ran): {run_action.received}"
        )
        assert change_action.received.count(UP) == 2, \
            f"UP was dispatched into a torn-down action: {change_action.received}"

        # Per-action isolation. A raiser must not starve its siblings.
        ident_iso = Input.Key("1x0")
        raiser = RaisingAction(
            tag="raiser",
            deck_controller=controller, page=page_b, input_ident=ident_iso)
        survivor = RecordingAction(
            tag="survivor",
            deck_controller=controller, page=page_b, input_ident=ident_iso)
        inject(page_b, ident_iso, [raiser, survivor])

        deck.fire_key_event(1, True)  # physical key 1 is "1x0" on the 2x4 layout
        assert fixtures.wait_until(lambda: DOWN in survivor.received)
        deck.fire_key_event(1, False)
        assert fixtures.wait_until(lambda: UP in survivor.received), (
            "a raising action starved its sibling of the UP -- per-action "
            f"isolation missing (survivor saw {survivor.received})"
        )
        assert SHORT_UP in survivor.received
        assert UP in raiser.received  # the raiser itself was still dispatched

        # The screensaver engages mid-hold and the gesture dies with the
        # stash. show() confiscates the whole input set, so the physical
        # release lands on the replacement key and is swallowed. The stashed
        # key's hold timer must not stay armed and fire HOLD_START into its
        # pinned snapshot after the finger left.
        controller.hold_time = 0.5
        ident_ss = Input.Key("2x0")
        ss_recorder = RecordingAction(
            tag="ss_recorder",
            deck_controller=controller, page=page_b, input_ident=ident_ss)
        inject(page_b, ident_ss, [ss_recorder])

        key_held = controller.get_input(ident_ss)
        deck.fire_key_event(2, True)  # physical key 2 is "2x0" on the 2x4 layout
        assert fixtures.wait_until(lambda: DOWN in ss_recorder.received)
        assert key_held.hold_start_timer is not None

        controller.screen_saver.set_media_path(
            fixtures.make_test_png(os.path.join(fixtures.DATA_DIR, "ss.png")))
        controller.screen_saver.show()

        assert key_held.hold_start_timer is None, \
            "show() must cancel the stashed key's armed hold timer"
        assert getattr(key_held, "_gesture", None) is None, \
            "show() must drop the stashed key's pinned gesture snapshot"
        assert key_held.down_start_time is None

        deck.fire_key_event(2, False)  # swallowed by the showing screensaver
        fired = fixtures.wait_until(
            lambda: HOLD_START in ss_recorder.received, timeout=controller.hold_time + 0.7)
        assert not fired, (
            "HOLD_START fired into the snapshot after the physical release, "
            f"mid-screensaver: {ss_recorder.received}"
        )
        assert UP not in ss_recorder.received  # the swallowed release dispatches nothing

        print("PASS: gesture events route to the DOWN-time action snapshot across page flips")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_pageflip_action_dispatch")


if __name__ == "__main__":
    main()
