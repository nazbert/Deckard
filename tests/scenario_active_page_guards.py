"""Regression tests for unguarded active_page derefs and pending-page retention.

active_page goes None on close() or load_page(None), and a racing switch swaps
it. Each part builds a deterministic seam for a race that is otherwise rare.
"""
from types import SimpleNamespace

import fixtures
import globals as gl


class FlippingController:
    """Stands in for a DeckController whose active_page a racing thread nulls.

    The property serves a real page for the first live_reads reads, then None.
    """

    def __init__(self, page, live_reads: int):
        self._page = page
        self._reads = 0
        self._live_reads = live_reads

    def get_alive(self) -> bool:
        return True

    @property
    def active_page(self):
        self._reads += 1
        return self._page if self._reads <= self._live_reads else None


def part_a_get_own_actions() -> None:
    from src.backend.DeckManagement.DeckController import ControllerInputState

    sentinel = ["sentinel-action"]
    page = SimpleNamespace(
        action_objects={},
        get_all_actions_for_input=lambda identifier, state: list(sentinel),
    )
    # Two live reads let the None checks pass, so the final
    # get_all_actions_for_input call is what sees the flip to None.
    ctrl = FlippingController(page, live_reads=2)

    state = object.__new__(ControllerInputState)
    state.deck_controller = ctrl
    state.controller_input = SimpleNamespace(deck_controller=ctrl, identifier="key-0x0")
    state.state = 0

    # Without the snapshot, this raises AttributeError on NoneType.
    actions = state.get_own_actions()
    assert actions == sentinel, (
        f"expected the snapshot page's actions, got {actions!r} -- the live "
        f"attribute was re-read after the None check"
    )
    print("PASS: get_own_actions survives a post-check active_page flip")


def part_b_load_page_tail(controller) -> None:
    from src.Signals import Signals

    seed_path = fixtures.seed_page("GuardTailPage")
    page = gl.page_manager.get_page(seed_path, controller)

    # initialize_actions runs in the tail right before the ChangePage signal.
    # Have it stand in for the racing close() or load_page(None) that nulls
    # active_page, which makes the seam deterministic.
    page.initialize_actions = lambda *a, **k: setattr(controller, "active_page", None)

    # Observe at the trigger call site, because a connected callback needs a
    # GLib main loop iteration and this harness runs none. The deref under test
    # happens at argument evaluation, so an AttributeError lands in the
    # @log.catch of load_page and records no ChangePage trigger.
    received = []
    original_trigger = gl.signal_manager.trigger_signal

    def recording_trigger(signal, *args, **kwargs):
        if signal is Signals.ChangePage:
            received.append(args)
        return original_trigger(signal, *args, **kwargs)

    gl.signal_manager.trigger_signal = recording_trigger
    try:
        controller.load_page(page)
    finally:
        gl.signal_manager.trigger_signal = original_trigger

    assert received, "ChangePage was never triggered -- the tail deref crashed into @log.catch"
    assert page.json_path in received[-1], (
        f"ChangePage should carry this switch's page path {page.json_path!r}, "
        f"got {received[-1]!r}"
    )
    print("PASS: load_page tail signals ChangePage despite a racing active_page null")


def part_c_load_default_page(controller) -> None:
    fixtures.seed_page("StateReqPage")

    loaded = []
    # Keep load_page inert, so active_page stays what the scenario sets. This is
    # the seam for a close() or clear that races the state-request branch.
    controller.load_page = lambda page, *a, **k: loaded.append(page)
    controller.active_page = None
    gl.api_state_requests[controller.serial_number()] = {
        "page_name": "StateReqPage",
        "coords": "0,0",
        "state": 0,
    }

    # Without the guard this raises AttributeError. The deref sits before the
    # branch's own try/except and the method has no other guard.
    controller.load_default_page()

    assert len(loaded) >= 2, (
        f"with no current page the requested page must be treated as different "
        f"and loaded; load_page calls: {len(loaded)}"
    )
    assert controller.serial_number() not in gl.api_state_requests, (
        "the state request must be consumed after processing"
    )
    print("PASS: load_default_page state-request branch survives active_page=None")


def part_d_close_clears_pending(controller) -> None:
    sentinel = object()
    controller._screensaver_pending_page = sentinel
    controller.close(remove_media=True)
    assert controller._screensaver_pending_page is None, (
        "_screensaver_pending_page must be released on close() -- it pins the "
        "deferred page's object graph on a dead controller"
    )
    print("PASS: close() releases _screensaver_pending_page")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_active_page_guards")

    part_a_get_own_actions()

    controller_b = fixtures.make_headless_controller(serial="guards-b")
    try:
        part_b_load_page_tail(controller_b)
    finally:
        fixtures.teardown(controller_b)

    controller_cd = fixtures.make_headless_controller(serial="guards-cd")
    try:
        part_c_load_default_page(controller_cd)
        part_d_close_clears_pending(controller_cd)
    finally:
        fixtures.teardown(controller_cd)

    print("PASS: scenario_active_page_guards")


if __name__ == "__main__":
    main()
