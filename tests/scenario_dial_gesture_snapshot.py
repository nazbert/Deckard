"""
Regression test for issue #123: dial/touchscreen gestures still live-resolved
their target actions across page changes -- the dial/touchscreen variant of
issue #107 (fixed for keys by the DOWN-time snapshot this scenario's
mechanism mirrors).

Mechanisms exercised (all against the real code paths):

  * ControllerDial's PUSH branch used to resolve get_active_state() /
    get_own_actions() at dispatch time, so a ChangePage on the dial's DOWN
    swapped active_page mid-gesture and the tail (HOLD_STOP/SHORT_UP, UP)
    landed on the NEW page's dial actions. The old page's actions never saw
    their release -- EasyCommand (com_core447_OSPlugin) carries the same
    registered_down latch as RunCommand, so a ChangePage+EasyCommand combo on
    a dial jammed exactly like upstream #475.
  * ControllerDial.on_hold_timer_end live-resolved too: a hold crossing a
    page swap fired HOLD_START into the new page's actions.
  * TURN_CW/CCW and every touchscreen event (DRAG_*, and the SHORT/LONG
    touches routed to a dial's state) are single events, but they resolved
    when the pool worker ran -- a swap in the event->worker window redirected
    them to the new page. The fix resolves them at read time, in
    event_callback on the deck's input thread. The DeferredExecutor below
    makes that window deterministic: queue the dispatch, swap the page, then
    drain.
  * ScreenSaver.show()'s stash sweep used to cancel only KEY gestures: a
    stashed dial's hold timer stayed armed across the input swap and fired
    HOLD_START after the physical release (which lands on the replacement
    dial and is swallowed).
"""
import os
from concurrent.futures import Future

import fixtures
import globals as gl

from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionCore import ActionCore

DOWN = Input.Dial.Events.DOWN
SHORT_UP = Input.Dial.Events.SHORT_UP
UP = Input.Dial.Events.UP
HOLD_START = Input.Dial.Events.HOLD_START
HOLD_STOP = Input.Dial.Events.HOLD_STOP
TURN_CW = Input.Dial.Events.TURN_CW
SHORT_TOUCH = Input.Dial.Events.SHORT_TOUCH_PRESS
DRAG_RIGHT = Input.Touchscreen.Events.DRAG_RIGHT


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


class ChangePageDialAction(RecordingAction):
    """Mirrors com_core447_DeckPlugin's ChangePage on a dial: the DOWN event
    loads the target page synchronously on the action pool."""

    def __init__(self, target_page, **kwargs):
        super().__init__(**kwargs)
        self.target_page = target_page

    def _raw_event_callback(self, event, data=None):
        super()._raw_event_callback(event, data)
        if event == DOWN:
            self.deck_controller.load_page(self.target_page)


class EasyCommandLikeAction(RecordingAction):
    """Mirrors com_core447_OSPlugin's EasyCommand latch verbatim
    (EasyCommand.py:24, the twin of RunCommand's): DOWN is swallowed while
    registered_down is set; only UP clears it."""

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


class DeferredExecutor:
    """Stands in for the deck's action pool to make the event->pool-worker
    window deterministic: submit() queues the dispatch instead of running it,
    drain() runs everything queued (in order, on the caller's thread). Used
    to slide a page swap between an event's read and its dispatch."""

    def __init__(self):
        self.queue = []

    def submit(self, fn, *args):
        future = Future()
        self.queue.append((fn, args, future))
        return future

    def drain(self):
        queued, self.queue = self.queue, []
        for fn, args, future in queued:
            try:
                future.set_result(fn(*args))
            except Exception as exc:  # pragma: no cover - surfaced by asserts
                future.set_exception(exc)


def inject(page, ident, actions: list) -> None:
    """Places stub action objects where get_all_actions_for_input reads:
    action_objects[input_type][json_identifier][state][index]."""
    per_state = page.action_objects.setdefault(ident.input_type, {}).setdefault(ident.json_identifier, {})
    per_state[0] = {i: a for i, a in enumerate(actions)}


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_dial_gesture_snapshot")
    controller = fixtures.make_headless_controller(serial="dial-snap-1")
    try:
        # Generous hold threshold so pool latency can never reclassify the
        # taps below as holds.
        controller.hold_time = 10.0

        deck = fixtures.raw_deck(controller)
        ident = Input.Dial("0")
        ts_ident = Input.Touchscreen("sd-plus")

        page_a = controller.active_page  # "Main", loaded at construction
        seed_b = fixtures.seed_page("FlipTarget")
        page_b = gl.page_manager.get_page(seed_b, controller)
        assert page_a is not None and page_b is not page_a

        change_action = ChangePageDialAction(
            target_page=page_b, tag="change_page",
            deck_controller=controller, page=page_a, input_ident=ident)
        easy_action = EasyCommandLikeAction(
            tag="easy_command",
            deck_controller=controller, page=page_a, input_ident=ident)
        snapshot_recorder = RecordingAction(
            tag="page_a_recorder",
            deck_controller=controller, page=page_a, input_ident=ident)
        bleed_recorder = RecordingAction(
            tag="page_b_recorder",
            deck_controller=controller, page=page_b, input_ident=ident)

        inject(page_a, ident, [change_action, easy_action, snapshot_recorder])
        inject(page_b, ident, [bleed_recorder])

        # ---- Press 1: dial DOWN flips the page mid-gesture ---- #
        deck.fire_dial_event(0, DialEventType.PUSH, True)
        assert fixtures.wait_until(lambda: DOWN in easy_action.received), \
            "DOWN never reached the old page's EasyCommand-alike"
        assert fixtures.wait_until(lambda: controller.active_page is page_b), \
            "ChangePage-alike never flipped the page"
        assert easy_action.run_count == 1

        deck.fire_dial_event(0, DialEventType.PUSH, False)
        assert fixtures.wait_until(lambda: UP in easy_action.received), (
            "UP was not delivered to the DOWN-time actions: the page flip "
            "redirected the dial gesture tail to the new page (issue #123) "
            f"-- easy_action saw {easy_action.received}"
        )
        assert SHORT_UP in easy_action.received, \
            f"SHORT_UP missing from the DOWN-time actions: {easy_action.received}"
        assert easy_action.registered_down is False, \
            "the EasyCommand latch must be cleared by the UP"
        assert bleed_recorder.received == [], (
            "the new page's dial action received part of a gesture that "
            f"started on the old page: {bleed_recorder.received}"
        )

        # ---- Back to page A, press 2: the command must run again ---- #
        controller.load_page(page_a)
        assert fixtures.wait_until(lambda: controller.active_page is page_a)

        deck.fire_dial_event(0, DialEventType.PUSH, True)
        assert fixtures.wait_until(lambda: easy_action.received.count(DOWN) == 2), \
            "second DOWN never reached the EasyCommand-alike"
        assert easy_action.run_count == 2, (
            "the command did not run on the second press -- the latch from "
            "press 1 was never cleared (upstream #475's 'fires only once', "
            "dial edition)"
        )
        assert fixtures.wait_until(lambda: controller.active_page is page_b)
        deck.fire_dial_event(0, DialEventType.PUSH, False)
        assert fixtures.wait_until(lambda: easy_action.received.count(UP) == 2), \
            "second UP lost"
        assert bleed_recorder.received == [], \
            f"gesture bleed onto page B on press 2: {bleed_recorder.received}"

        # ---- Press 3: hold across the flip -- the timer's HOLD_START must
        # land on the snapshot, not live-resolve onto the new page ---- #
        controller.load_page(page_a)
        assert fixtures.wait_until(lambda: controller.active_page is page_a)
        controller.hold_time = 0.4

        deck.fire_dial_event(0, DialEventType.PUSH, True)
        assert fixtures.wait_until(lambda: controller.active_page is page_b)
        assert fixtures.wait_until(
            lambda: HOLD_START in snapshot_recorder.received,
            timeout=controller.hold_time + 2.0), (
            "HOLD_START never reached the DOWN-time actions: "
            "on_hold_timer_end live-resolved onto the new page -- "
            f"page B saw {bleed_recorder.received}"
        )
        assert HOLD_START not in bleed_recorder.received, \
            f"HOLD_START bled onto the new page: {bleed_recorder.received}"

        deck.fire_dial_event(0, DialEventType.PUSH, False)
        assert fixtures.wait_until(lambda: HOLD_STOP in snapshot_recorder.received), \
            f"HOLD_STOP missing from the snapshot: {snapshot_recorder.received}"
        assert fixtures.wait_until(lambda: snapshot_recorder.received.count(UP) == 3)
        assert bleed_recorder.received == [], \
            f"hold gesture bled onto page B: {bleed_recorder.received}"
        controller.hold_time = 10.0

        # ---- TURN: single event, resolved at READ time ---- #
        # A turn read on page A whose pool dispatch runs after a swap must
        # still land on page A's actions. DeferredExecutor holds the dispatch
        # while the test swaps the page -- the deterministic version of the
        # event->worker window.
        controller.load_page(page_a)
        assert fixtures.wait_until(lambda: controller.active_page is page_a)

        real_executor = controller.action_executor
        deferred = DeferredExecutor()
        controller.action_executor = deferred
        try:
            deck.fire_dial_event(0, DialEventType.TURN, 2)  # read on page A
            controller.load_page(page_b)                    # swap before dispatch
            assert controller.active_page is page_b
        finally:
            controller.action_executor = real_executor
        deferred.drain()

        assert TURN_CW in snapshot_recorder.received, (
            "a turn read on page A was dispatched against the page that was "
            f"active at pool time: page A saw {snapshot_recorder.received}, "
            f"page B saw {bleed_recorder.received}"
        )
        assert TURN_CW not in bleed_recorder.received, \
            f"turn bled onto the new page: {bleed_recorder.received}"

        # ---- Touchscreen: DRAG + dial-routed SHORT, resolved at READ time ---- #
        ts_recorder = RecordingAction(
            tag="ts_page_a_recorder",
            deck_controller=controller, page=page_a, input_ident=ts_ident)
        ts_bleed = RecordingAction(
            tag="ts_page_b_recorder",
            deck_controller=controller, page=page_b, input_ident=ts_ident)
        inject(page_a, ts_ident, [ts_recorder])
        inject(page_b, ts_ident, [ts_bleed])

        controller.load_page(page_a)
        assert fixtures.wait_until(lambda: controller.active_page is page_a)

        deferred = DeferredExecutor()
        controller.action_executor = deferred
        try:
            # x < x_out -> DRAG_RIGHT (own actions); SHORT at x=10 routes to
            # dial 0's state (SHORT_TOUCH_PRESS).
            deck.fire_touchscreen_event(
                TouchscreenEventType.DRAG,
                {"x": 10, "y": 50, "x_out": 700, "y_out": 50})
            deck.fire_touchscreen_event(
                TouchscreenEventType.SHORT, {"x": 10, "y": 20})
            controller.load_page(page_b)
            assert controller.active_page is page_b
        finally:
            controller.action_executor = real_executor
        deferred.drain()

        assert DRAG_RIGHT in ts_recorder.received, (
            "a drag read on page A was dispatched against the page that was "
            f"active at pool time: page A saw {ts_recorder.received}, "
            f"page B saw {ts_bleed.received}"
        )
        assert DRAG_RIGHT not in ts_bleed.received, \
            f"drag bled onto the new page: {ts_bleed.received}"
        assert SHORT_TOUCH in snapshot_recorder.received, (
            "a dial-routed touch read on page A was dispatched against the "
            f"new page: page B saw {bleed_recorder.received}"
        )
        assert SHORT_TOUCH not in bleed_recorder.received, \
            f"dial-routed touch bled onto the new page: {bleed_recorder.received}"

        # ---- Screensaver engages MID-HOLD: the dial gesture dies with the
        # stash, exactly like the key case ---- #
        controller.hold_time = 0.5
        ident_ss = Input.Dial("1")
        ss_recorder = RecordingAction(
            tag="ss_recorder",
            deck_controller=controller, page=page_b, input_ident=ident_ss)
        inject(page_b, ident_ss, [ss_recorder])

        dial_held = controller.get_input(ident_ss)
        deck.fire_dial_event(1, DialEventType.PUSH, True)
        assert fixtures.wait_until(lambda: DOWN in ss_recorder.received)
        assert dial_held.hold_start_timer is not None

        controller.screen_saver.set_media_path(
            fixtures.make_test_png(os.path.join(fixtures.DATA_DIR, "ss.png")))
        controller.screen_saver.show()

        assert dial_held.hold_start_timer is None, \
            "show() must cancel the stashed dial's armed hold timer"
        assert getattr(dial_held, "_gesture", None) is None, \
            "show() must drop the stashed dial's pinned gesture snapshot"
        assert dial_held.down_start_time is None

        deck.fire_dial_event(1, DialEventType.PUSH, False)  # swallowed
        fired = fixtures.wait_until(
            lambda: HOLD_START in ss_recorder.received,
            timeout=controller.hold_time + 0.7)
        assert not fired, (
            "HOLD_START fired into the snapshot after the physical release, "
            f"mid-screensaver: {ss_recorder.received}"
        )
        assert UP not in ss_recorder.received  # the swallowed release dispatches nothing

        print("PASS: dial/touchscreen events route to their read-time actions across page flips")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_dial_gesture_snapshot")


if __name__ == "__main__":
    main()
