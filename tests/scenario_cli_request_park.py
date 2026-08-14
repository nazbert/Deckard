"""Pins the parked page and state requests in src/backend/startup_queue.py.

A page request is claimed once and gone. A state request is peeked, then
resolved, so an exception during apply leaves it for the next load to retry.
"""
import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import os  # noqa: E402

import globals as gl  # noqa: E402

WATCHDOG_SECONDS = 60

# The deck this scenario stands up.
SERIAL = "park-1"
# A serial no deck in this process reports.
ABSENT_SERIAL = "never-connects"


def _state_request(page_name: str, coords: str = "0,0", state: int = 0) -> dict:
    """The dict shape main.py parks, after its own int() conversion."""
    return {"page_name": page_name, "coords": coords, "state": state}


def check_parking_is_last_write_wins(queue) -> None:
    gl.api_page_requests.clear()
    gl.api_state_requests.clear()

    queue.park_page_request("lww", "First")
    queue.park_page_request("lww", "Second")
    assert gl.api_page_requests == {"lww": "Second"}, (
        f"a second page request for one serial replaces the first: "
        f"{gl.api_page_requests}"
    )

    queue.park_state_request("lww", _state_request("First", state=0))
    queue.park_state_request("lww", _state_request("Second", state=3))
    assert list(gl.api_state_requests) == ["lww"], (
        f"state requests are keyed by serial alone: {gl.api_state_requests}"
    )
    assert gl.api_state_requests["lww"]["state"] == 3, (
        f"the later state request must win: {gl.api_state_requests['lww']}"
    )

    # Parking two serials keeps two requests. The collapse above is per-serial,
    # not global.
    queue.park_page_request("other", "Third")
    assert gl.api_page_requests == {"lww": "Second", "other": "Third"}, (
        f"different serials must not collide: {gl.api_page_requests}"
    )

    gl.api_page_requests.clear()
    gl.api_state_requests.clear()
    print("PASS: parking is per-serial last-write-wins")


def check_page_request_is_claimed_once(queue) -> None:
    gl.api_page_requests.clear()

    assert queue.claim_page_request("nothing-parked") is None, (
        "claiming a serial with nothing parked must answer None, not raise"
    )

    queue.park_page_request(SERIAL, "Parked")
    assert queue.claim_page_request(SERIAL) == "Parked", (
        "the first claim must hand the parked page name over"
    )
    assert SERIAL not in gl.api_page_requests, (
        f"the claim must remove the request: {gl.api_page_requests}"
    )
    assert queue.claim_page_request(SERIAL) is None, (
        "a page request is one-shot: the second claim must answer None"
    )

    print("PASS: a parked page request is claimed exactly once")


def check_state_peek_does_not_consume(queue) -> None:
    gl.api_state_requests.clear()

    assert queue.peek_state_request("nothing-parked") is None

    parked = _state_request("StateTarget", state=2)
    queue.park_state_request(SERIAL, parked)

    first = queue.peek_state_request(SERIAL)
    second = queue.peek_state_request(SERIAL)
    assert first is parked and second is parked, (
        "peek must hand back the parked request itself, unchanged and still "
        f"parked: {first!r} / {second!r}"
    )
    assert SERIAL in gl.api_state_requests, (
        "peek must NOT consume -- the request has to outlive the work it "
        "describes, or a failure mid-apply loses it"
    )

    queue.resolve_state_request(SERIAL)
    assert SERIAL not in gl.api_state_requests, (
        f"resolve must consume the request: {gl.api_state_requests}"
    )
    assert queue.peek_state_request(SERIAL) is None
    # Two controllers can race the same serial up, so a resolve that finds
    # nothing is not an error.
    queue.resolve_state_request(SERIAL)

    print("PASS: peek leaves the state request parked, resolve consumes it")


def check_slots_are_read_per_call(queue) -> None:
    original_pages = gl.api_page_requests
    original_states = gl.api_state_requests
    swapped_pages: dict = {}
    swapped_states: dict = {}
    try:
        gl.api_page_requests = swapped_pages
        gl.api_state_requests = swapped_states

        queue.park_page_request(SERIAL, "Parked")
        queue.park_state_request(SERIAL, _state_request("StateTarget"))
        assert swapped_pages and swapped_states, (
            "parking must land on the dicts gl points at now, not on the ones "
            f"the queue first saw: {swapped_pages} / {swapped_states}"
        )
        assert not original_pages and not original_states, (
            "nothing may land on the dicts gl no longer points at: "
            f"{original_pages} / {original_states}"
        )
        assert queue.claim_page_request(SERIAL) == "Parked"
        assert queue.peek_state_request(SERIAL) is not None

        # The other direction. A request written straight into the slot must be
        # visible to the queue. The CLI-facing surface is the dict, and
        # scenario_active_page_guards drives the state branch by writing one by
        # hand. Both stay honest only while nothing here is cached.
        gl.api_page_requests["hand-written"] = "Parked"
        gl.api_state_requests["hand-written"] = _state_request("StateTarget")
        assert queue.claim_page_request("hand-written") == "Parked", (
            "a hand-written page request must be claimable"
        )
        assert queue.peek_state_request("hand-written") is not None, (
            "a hand-written state request must be visible to peek"
        )
        queue.resolve_state_request("hand-written")
        assert list(swapped_states) == [SERIAL], (
            f"resolve must remove only the serial it was given: {swapped_states}"
        )
    finally:
        gl.api_page_requests = original_pages
        gl.api_state_requests = original_states

    print("PASS: the gl request slots are read per call, never cached")


def check_first_load_claims_parked_page(controller) -> None:
    """The first load of a controller claims a page request parked before it.

    DeckController.__init__ ends in load_default_page(), so that first load is
    the claim.
    """
    parked_path = fixtures.seed_page("Parked")

    assert controller.active_page is not None, (
        "the headless controller should have loaded a page during __init__"
    )
    assert os.path.abspath(controller.active_page.json_path) == os.path.abspath(parked_path), (
        f"the first load_default_page must apply the parked page request, not "
        f"the default page: {controller.active_page.json_path}"
    )
    assert SERIAL not in gl.api_page_requests, (
        f"applying the request must consume it: {gl.api_page_requests}"
    )

    # A request left parked re-applies itself on every later load_default_page
    # for this serial, on every replug and every fallback, so the deck can never
    # be moved off the page the CLI named. The second load must fall back to the
    # normal default.
    fallback_path = gl.page_manager.get_pages()[0]
    controller.load_default_page()
    assert os.path.abspath(controller.active_page.json_path) != os.path.abspath(parked_path), (
        "the second load_default_page re-applied the already-claimed page "
        "request -- the claim is not one-shot"
    )
    assert os.path.abspath(controller.active_page.json_path) == os.path.abspath(fallback_path), (
        f"the second load must take the normal default-page path, got "
        f"{controller.active_page.json_path}"
    )

    print("PASS: a parked page request is applied once, by the first load only")


def check_absent_serial_stays_parked(queue, controller) -> None:
    queue.park_page_request(ABSENT_SERIAL, "Parked")
    queue.park_state_request(ABSENT_SERIAL, _state_request("StateTarget"))

    for _ in range(4):
        controller.load_default_page()

    assert gl.api_page_requests.get(ABSENT_SERIAL) == "Parked", (
        f"another deck's loads must not consume this serial's page request: "
        f"{gl.api_page_requests}"
    )
    assert gl.api_state_requests.get(ABSENT_SERIAL) is not None, (
        f"another deck's loads must not consume this serial's state request: "
        f"{gl.api_state_requests}"
    )

    # Nothing in the app sweeps these. The scenario does, so the guards below
    # start from a known set.
    gl.api_page_requests.pop(ABSENT_SERIAL, None)
    gl.api_state_requests.pop(ABSENT_SERIAL, None)

    print("PASS: a request for a serial that never connects stays parked")


def check_state_request_survives_failed_apply(queue, controller) -> None:
    target_path = fixtures.seed_page("StateTarget")
    queue.park_state_request(SERIAL, _state_request("StateTarget"))

    original_load_page = controller.load_page
    loads: list = []

    def exploding_load_page(page, *args, **kwargs):
        loads.append(page)
        if len(loads) >= 2:
            # Within one load_default_page() call the first load is the default
            # page and the second is the page of the state request, so this
            # raises exactly between the peek and the resolve, which is the
            # window the split exists for.
            raise RuntimeError("simulated failure while applying a state request")
        return original_load_page(page, *args, **kwargs)

    controller.load_page = exploding_load_page
    try:
        raised = None
        try:
            controller.load_default_page()
        except RuntimeError as e:
            raised = e
        assert raised is not None, (
            f"the failure seam never fired -- the state request's page was not "
            f"loaded, so this guard would pass vacuously (loads: {len(loads)})"
        )
        assert len(loads) == 2, (
            f"expected the default load then the state request's load: {len(loads)}"
        )
        assert gl.api_state_requests.get(SERIAL) is not None, (
            "a state request must stay parked when applying it raises -- "
            "peek/resolve are two calls precisely so the next load retries it"
        )
    finally:
        del controller.load_page

    # The retry runs the same request with nothing in the way this time.
    controller.load_default_page()
    assert os.path.abspath(controller.active_page.json_path) == os.path.abspath(target_path), (
        f"the retry must load the page the state request names: "
        f"{controller.active_page.json_path}"
    )
    assert SERIAL not in gl.api_state_requests, (
        f"a processed state request must be resolved: {gl.api_state_requests}"
    )

    # Having been resolved, it is not applied a third time.
    fallback_path = gl.page_manager.get_pages()[0]
    controller.load_default_page()
    assert os.path.abspath(controller.active_page.json_path) == os.path.abspath(fallback_path), (
        f"a resolved state request must not re-apply itself: "
        f"{controller.active_page.json_path}"
    )

    print("PASS: a state request survives a failed apply and is consumed by the retry")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_cli_request_park")

    from src.backend import startup_queue

    queue = startup_queue.get()

    try:
        check_parking_is_last_write_wins(queue)
        check_page_request_is_claimed_once(queue)
        check_state_peek_does_not_consume(queue)
        check_slots_are_read_per_call(queue)

        gl.api_page_requests.clear()
        gl.api_state_requests.clear()

        # Park before the deck exists, which is the point of the leg. The
        # load_default_page of DeckController.__init__ is the first claim.
        fixtures.seed_page("Parked")
        queue.park_page_request(SERIAL, "Parked")

        controller = fixtures.make_headless_controller(serial=SERIAL)
        try:
            check_first_load_claims_parked_page(controller)
            check_absent_serial_stays_parked(queue, controller)
            check_state_request_survives_failed_apply(queue, controller)
        finally:
            fixtures.teardown(controller)
    finally:
        gl.api_page_requests.clear()
        gl.api_state_requests.clear()

    print("ALL PASS: scenario_cli_request_park")


if __name__ == "__main__":
    main()
