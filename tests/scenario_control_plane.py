"""Pins the control plane in src/backend/control_plane.py.

One rule set decides whether a page or state switch is valid, for every
transport. A validation failure comes back as a result; an exception escapes.
"""
import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

import globals as gl  # noqa: E402

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

WATCHDOG_SECONDS = 120

# Controller A is the stock 2x4 fake deck, used for everything about pages
# rather than device geometry.
SERIAL_A = "cp-a"
# Controller B is a 10x10 deck, the only way to prove that in-bounds means what
# this device says rather than what some constant says.
SERIAL_B = "cp-b"
WIDE_KEY = "9x9"
WIDE_STATES = 20


def seed_multistate_page(page_name: str, key_ident: str, n_states: int) -> str:
    """Seed a page whose key_ident carries n_states states.

    The state bound under test is then the input's real state count.
    """
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {key_ident: {"states": {str(i): {} for i in range(n_states)}}},
            "dials": {}, "touchscreens": {},
        }, f)
    return path


def settle(controller, quiet_for: float = 0.4, timeout: float = 10.0) -> None:
    """Wait until the deck has gone quiet_for seconds without a write.

    The media thread feeds the journal, so a claim that nothing happened means
    something only once whatever was happening has finished.
    """
    raw = fixtures.raw_deck(controller)
    deadline = time.monotonic() + timeout
    last = raw.current_seq()
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = raw.current_seq()
        if now != last:
            last = now
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_for:
            return
    raise AssertionError(
        f"deck {controller.serial_number()} never went quiet within {timeout}s "
        f"-- the journal-delta guards below cannot be trusted")


def load_named_page(controller, page_name: str) -> str:
    """Put page_name on controller the plain way, with no service involved.

    Waits for it to become the active page, the fixed starting point the
    equivalence and no-op guards compare from.
    """
    path = gl.page_manager.find_matching_page_path(page_name)
    assert path is not None, f"the scenario must seed {page_name!r} before loading it"
    if controller.active_page is None or os.path.abspath(controller.active_page.json_path) != os.path.abspath(path):
        controller.load_page(gl.page_manager.get_page(path, controller))
    assert fixtures.wait_until(
        lambda: controller.active_page is not None
        and os.path.abspath(controller.active_page.json_path) == os.path.abspath(path)
    ), f"{page_name!r} never became the active page"
    return path


def active_name(controller) -> str | None:
    page = controller.active_page
    return None if page is None else page.get_name()


# 1. Unknown serial

def check_unknown_serial(plane) -> None:
    result = plane.change_page("not-a-deck", "Main")
    assert not result.ok and result.code == "no-such-deck", result
    assert SERIAL_A in result.message and SERIAL_B in result.message, (
        f"the failure must name the serials that ARE connected, so a typo is "
        f"self-diagnosing: {result.message!r}")

    state_result = plane.change_state("not-a-deck", "Main", "0,0", 0)
    assert not state_result.ok and state_result.code == "no-such-deck", state_result
    assert state_result.message == result.message, (
        "both entry points must answer an unknown serial identically")

    # With nothing connected there is no list to offer. The wording has to leave
    # room for a deck on its way, because requests are answered from the moment
    # the app takes the bus name, which is before it opens a single deck. A flat
    # none-connected would call a deck unplugged while it is merely unenumerated.
    manager = gl.deck_manager
    saved = manager.deck_controller
    manager.deck_controller = []
    try:
        empty = plane.change_page("not-a-deck", "Main")
    finally:
        manager.deck_controller = saved
    assert not empty.ok and empty.code == "no-such-deck", empty
    assert empty.message == ("No StreamDeck devices connected yet "
                             "(the app may still be starting)"), empty.message

    print("PASS: an unknown serial answers no-such-deck and lists what is connected")


# 2. Unknown page

def check_unknown_page(plane, controller) -> None:
    before = load_named_page(controller, "Alpha")
    # Compare by identity, not by path equality. A rejected request that
    # reloaded the same page would leave an equal path and a different object,
    # and only identity tells those apart.
    active_before = controller.active_page

    result = plane.change_page_on(controller, "no-such-page")
    assert not result.ok and result.code == "no-such-page", result
    assert "Alpha" in result.message and "Beta" in result.message, (
        f"the failure must list the pages that exist: {result.message!r}")
    assert controller.active_page is active_before, (
        "an unknown page name must leave the active page untouched")
    assert os.path.abspath(controller.active_page.json_path) == os.path.abspath(before)

    # The same request through the serial-resolving wrapper and through the
    # state entry point. One rule, three doors.
    assert plane.change_page(SERIAL_A, "no-such-page").code == "no-such-page"
    assert plane.change_state(SERIAL_A, "no-such-page", "0,0", 0).code == "no-such-page", (
        "a state change must fail on the page BEFORE it looks at coordinates")

    # The same guards, through the transport a --change-page reaches today. The
    # handler they were written against crashes on an unknown name. It compares
    # os.path.abspath(None) against the active page path and calls
    # get_page(page_path=None) before its None check.
    from src.api import DeckardAPI

    top = DeckardAPI()

    assert top.ChangePage(SERIAL_A, "no-such-page"), (
        "an unknown page must answer with the reason, not the empty string "
        "that means success")
    assert controller.active_page is active_before, (
        "unknown page name must leave the active page untouched")

    # An unknown page with no active page is the second half of the same crash.
    controller.active_page = None
    top.ChangePage(SERIAL_A, "no-such-page")
    assert controller.active_page is None, "skip path must not load anything"
    controller.active_page = active_before

    # A successful switch, the same page again as a no-op, and a serial nobody
    # reports. None of them may raise.
    target = fixtures.seed_page("ChangeTarget")
    assert top.ChangePage(SERIAL_A, "ChangeTarget") == ""
    assert fixtures.wait_until(
        lambda: controller.active_page is not None
        and os.path.abspath(controller.active_page.json_path) == os.path.abspath(target)
    ), "known page name must still load the page"
    assert top.ChangePage(SERIAL_A, "ChangeTarget") == "", (
        "the deck already showing the page is a fulfilled request")
    assert top.ChangePage("other-serial", "ChangeTarget"), (
        "a serial nobody reports must answer with the reason")

    print("PASS: an unknown page answers no-such-page and never disturbs the deck")


def check_unbuildable_page_leaves_deck_alone(plane, controller) -> None:
    """A page name that resolves to a file the store cannot turn into a page.

    get_page answers None, and load_page(None) clears the deck, so the deck goes
    blank while the request reports success on every surface at once.
    """
    load_named_page(controller, "Alpha")
    active_before = controller.active_page
    page_manager = gl.page_manager
    real_get_page = page_manager.get_page

    def failing_get_page(path, deck_controller, *args, **kwargs):
        if os.path.basename(path) == "Beta.json":
            return None
        return real_get_page(path, deck_controller, *args, **kwargs)

    page_manager.get_page = failing_get_page
    try:
        result = plane.change_page_on(controller, "Beta")
        assert not result.ok and result.code == "page-build-failed", (
            f"a page that cannot be built must be a failure, not a success "
            f"that happens to clear the deck: {result}")
        assert "Beta" in result.message, result.message
        assert controller.active_page is active_before, (
            f"the deck was cleared by a page it could not build: "
            f"{active_name(controller)}")

        # And through the transports, because both report success while the
        # deck goes dark.
        from src.api import ControllerInstanceAPI, DeckardAPI

        assert DeckardAPI().ChangePage(SERIAL_A, "Beta") == result.message
        ControllerInstanceAPI(controller).SetActivePage("Beta")
        assert controller.active_page is active_before, (
            "SetActivePage cleared the deck with a page it could not build")

        # The state entry point fails on the page before it looks further.
        state_result = plane.change_state_on(controller, "Beta", "0,0", 0)
        assert state_result.code == "page-build-failed", state_result
    finally:
        del page_manager.get_page

    print("PASS: a page that cannot be built is a failure, and the deck keeps its page")


# 3. The same-page no-op, through the DBus method

def check_same_page_noop_over_dbus(controller) -> None:
    """SetActivePage must not reload the page a deck already shows.

    A reload reaches the device, which the write journal records. Identical
    repaints are deduped at the write boundary, so the probe is something a
    load restores, and an off-page brightness makes the next load visible.
    """
    from src.api import ControllerInstanceAPI

    probe_brightness = 37

    load_named_page(controller, "Beta")
    api = ControllerInstanceAPI(controller)
    raw = fixtures.raw_deck(controller)

    # The armed check comes first. A real switch must move the journal, or the
    # no-writes assertion below would pass for the wrong reason.
    controller.set_brightness(probe_brightness)
    settle(controller)
    seq = raw.current_seq()
    api.SetActivePage("Alpha")
    assert fixtures.wait_until(lambda: raw.current_seq() > seq, timeout=5.0), (
        "switching to a different page wrote nothing to the deck -- the journal "
        "instrument is not armed, so the no-op assertion below proves nothing")
    assert fixtures.wait_until(lambda: active_name(controller) == "Alpha"), (
        f"SetActivePage did not switch the page: {active_name(controller)}")
    assert api.ActivePageName == "Alpha", api.ActivePageName

    # The guard itself runs the same request again, with the probe re-armed.
    controller.set_brightness(probe_brightness)
    settle(controller)
    seq = raw.current_seq()
    api.SetActivePage("Alpha")
    time.sleep(0.5)
    assert raw.current_seq() == seq, (
        f"SetActivePage for the page already showing reached the device: "
        f"{[(e[2], e[3]) for e in raw.ops_after(seq)]} -- the same-page no-op "
        f"is missing, so every repeat call reloads the deck")
    assert controller.brightness == probe_brightness, (
        f"the reload restored the page's brightness over the probe: "
        f"{controller.brightness}")
    assert api.ActivePageName == "Alpha", (
        "a no-op is a fulfilled request: the reported active page must stand")

    print("PASS: SetActivePage for the active page is a no-op (nothing reaches the deck)")


# 4 and 5. Device-truth bounds, and loading only when different

def check_load_only_if_different(plane, controller) -> None:
    load_named_page(controller, "Alpha")

    original_load_page = controller.load_page
    loads: list = []

    def counting_load_page(page, *args, **kwargs):
        loads.append(page)
        return original_load_page(page, *args, **kwargs)

    controller.load_page = counting_load_page
    try:
        same = plane.change_page_on(controller, "Alpha")
        assert same.ok and same.code == "already-active", same
        assert loads == [], f"the active page must not be reloaded: {loads}"

        moved = plane.change_page_on(controller, "Beta")
        assert moved.ok and moved.code == "", moved
        assert len(loads) == 1, f"a real switch must load exactly once: {loads}"
        assert fixtures.wait_until(lambda: active_name(controller) == "Beta")

        # A page named by its full path is the same page as one named by name.
        by_path = plane.change_page_on(controller, controller.active_page.json_path)
        assert by_path.code == "already-active", by_path
        assert len(loads) == 1, f"resolving by path must reach the same no-op: {loads}"
    finally:
        del controller.load_page

    print("PASS: a page load happens only when the requested page is different")


def check_device_truth_bounds(plane, controller_b) -> None:
    """The 10x10 deck. Every number here comes from the device or the page."""
    result = plane.change_page_on(controller_b, "Wide")
    assert result.ok, result
    c_input = controller_b.get_input(Input.Key(WIDE_KEY))
    assert c_input is not None, f"the 10x10 deck must have an input at {WIDE_KEY}"
    assert fixtures.wait_until(lambda: len(c_input.states) == WIDE_STATES, timeout=10.0), (
        f"the page's {WIDE_STATES} states never reached the input: {len(c_input.states)}")

    # In bounds for this device, and beyond the invented CLI caps of x,y <= 10
    # and state <= 20, which rejected requests before they reached a deck.
    ok = plane.change_state_on(controller_b, "Wide", "9,9", 19)
    assert ok.ok and ok.code == "", ok
    assert c_input.state == 19, f"the input must actually be on state 19: {c_input.state}"
    assert SERIAL_B in ok.message, ok.message

    # A state that arrives as text still applies. Nothing on the DBus signature
    # can carry a string, but the parked-request dict is written into by hand,
    # so the success arm of that conversion holds only while this asserts it.
    assert plane.change_state_on(controller_b, "Wide", "9,9", "18").ok
    assert c_input.state == 18, f"a state given as text must apply: {c_input.state}"
    assert plane.change_state_on(controller_b, "Wide", "9,9", 19).ok

    # Out of bounds for this device.
    oob = plane.change_state_on(controller_b, "Wide", "10,10", 0)
    assert not oob.ok and oob.code == "coords-out-of-bounds", oob
    assert "x=0-9" in oob.message and "y=0-9" in oob.message, oob.message
    assert c_input.state == 19, "a rejected request must not have touched the deck"

    negative = plane.change_state_on(controller_b, "Wide", "-1,0", 0)
    assert negative.code == "coords-out-of-bounds", negative

    # Past the end of the states of this input.
    too_high = plane.change_state_on(controller_b, "Wide", "9,9", WIDE_STATES)
    assert not too_high.ok and too_high.code == "state-out-of-range", too_high
    assert f"{WIDE_STATES} states (0-{WIDE_STATES - 1})" in too_high.message, too_high.message
    assert c_input.state == 19, "a rejected state must not have been applied"

    # And the single-state phrasing, from a key the page says nothing about.
    single = controller_b.get_input(Input.Key("0x0"))
    assert len(single.states) == 1, f"an unconfigured key has one state: {len(single.states)}"
    one_state = plane.change_state_on(controller_b, "Wide", "0,0", 1)
    assert one_state.code == "state-out-of-range", one_state
    assert "only has 1 state (state 0)" in one_state.message, one_state.message

    print("PASS: coordinate and state bounds are the device's and the page's truth")


# 6 and 7. Failures are results, exceptions are not

def check_validation_failures_are_results(plane, controller) -> None:
    load_named_page(controller, "Alpha")
    raw = fixtures.raw_deck(controller)
    settle(controller)
    seq = raw.current_seq()

    cases = [
        (("Alpha", "not-coords", 0), "bad-coords"),
        (("Alpha", "1,2,3", 0), "bad-coords"),
        (("Alpha", "", 0), "bad-coords"),
        (("Alpha", None, 0), "bad-coords"),
        (("Alpha", "99,0", 0), "coords-out-of-bounds"),
        (("Alpha", "0,0", "not-a-number"), "bad-state"),
        (("Alpha", "0,0", None), "bad-state"),
        (("Alpha", "0,0", 7), "state-out-of-range"),
        (("Alpha", "0,0", -1), "state-out-of-range"),
        (("no-such-page", "0,0", 0), "no-such-page"),
    ]
    for args, expected in cases:
        try:
            result = plane.change_state_on(controller, *args)
        except Exception as e:
            raise AssertionError(
                f"change_state_on{args} raised {e!r} -- a bad request is a "
                f"result, not an exception") from e
        assert not result.ok and result.code == expected, f"{args} -> {result}"
        assert result.message, f"{args} produced a failure with nothing to say"

    assert raw.current_seq() == seq, (
        f"a rejected request must not reach the device: {raw.ops_after(seq)}")
    assert active_name(controller) == "Alpha", (
        f"a rejected request must not change the page: {active_name(controller)}")

    print("PASS: every validation failure is a ControlResult, and none reach the deck")


def check_unexpected_exception_propagates(plane, controller) -> None:
    """Only a genuine exception may escape, and it must escape.

    The peek-and-resolve split of the boot path is built on this.
    """
    load_named_page(controller, "Alpha")

    def exploding_load_page(page, *args, **kwargs):
        raise RuntimeError("simulated failure while loading a page")

    controller.load_page = exploding_load_page
    try:
        raised = None
        try:
            plane.change_state_on(controller, "Beta", "0,0", 0)
        except RuntimeError as e:
            raised = e
        assert raised is not None, (
            "a raising load_page must propagate out of the service -- swallowing "
            "it here turns the parked-request retry into a silent drop")

        raised = None
        try:
            plane.change_page_on(controller, "Beta")
        except RuntimeError as e:
            raised = e
        assert raised is not None, "the page entry point must propagate it too"
    finally:
        del controller.load_page

    print("PASS: an unexpected exception propagates out of the service")


# 8. The delegate and the core agree

def check_dbus_delegate_matches_service(plane, controller) -> None:
    """Drive the DBus method and the service core over the same requests.

    Both start from the same state, and the deck must end up doing the same
    thing. The transport may render the answer, and may not decide anything.
    """
    from src.api import DeckardAPI

    top = DeckardAPI()

    def outcome(drive) -> tuple:
        """Active page and load count after drive(), from a fixed start.

        load_page sets active_page itself, so it is already the answer when
        the drive returns.
        """
        load_named_page(controller, "Alpha")
        original_load_page = controller.load_page
        loads: list = []

        def counting_load_page(page, *args, **kwargs):
            loads.append(page)
            return original_load_page(page, *args, **kwargs)

        controller.load_page = counting_load_page
        try:
            drive()
        finally:
            del controller.load_page
        return (active_name(controller), len(loads))

    requests = [
        (SERIAL_A, "Beta"),            # a real switch
        (SERIAL_A, "Alpha"),           # the same-page no-op
        (SERIAL_A, "no-such-page"),    # an unknown page
        ("not-a-deck", "Beta"),        # an unknown serial
    ]
    for serial, page_name in requests:
        through_method = outcome(
            lambda s=serial, p=page_name: top.ChangePage(s, p))
        through_service = outcome(
            lambda s=serial, p=page_name: plane.change_page(s, p))
        assert through_method == through_service, (
            f"request ({serial!r}, {page_name!r}) came out differently through the "
            f"DBus method ({through_method}) than through the service "
            f"({through_service}) -- the transport has grown rules of its own")
        # The method says what the service decided. It answers empty on
        # success, the no-op included, and the service sentence otherwise.
        result = plane.change_page(serial, page_name)
        assert top.ChangePage(serial, page_name) == ("" if result.ok else result.message), (
            f"the method's answer for ({serial!r}, {page_name!r}) is not the "
            f"service's: {result}")

    print("PASS: the DBus method and the service core reach the same outcome")


def check_state_delegate_matches_service(plane, controller) -> None:
    """The state half of the same comparison.

    Drive the DBus method and the service core over the same requests from the
    same starting state, and compare what the deck ended up doing.
    """
    from src.api import DeckardAPI

    top = DeckardAPI()
    c_input = controller.get_input(Input.Key(WIDE_KEY))

    def outcome(drive) -> tuple:
        assert plane.change_page_on(controller, "Wide").ok
        assert fixtures.wait_until(lambda: len(c_input.states) == WIDE_STATES, timeout=10.0)
        c_input.set_state(0)
        original_load_page = controller.load_page
        loads: list = []

        def counting_load_page(page, *args, **kwargs):
            loads.append(page)
            return original_load_page(page, *args, **kwargs)

        controller.load_page = counting_load_page
        try:
            drive()
        finally:
            del controller.load_page
        return (active_name(controller), c_input.state, len(loads))

    # The state is an integer on this transport, as the method signature says,
    # and the coordinates are still the text the caller typed. Parsing them is
    # the job of the service, on every path.
    requests = [
        (SERIAL_B, "Wide", "9,9", 5),          # applied
        (SERIAL_B, "Wide", "9,9", 99),         # state out of range
        (SERIAL_B, "Wide", "9,9", -1),         # below the first state
        (SERIAL_B, "Wide", "nope", 5),         # not coordinates
        (SERIAL_B, "Wide", "0,0", 1),          # in bounds, single-state input
        (SERIAL_B, "no-such-page", "9,9", 5),  # unknown page
        ("not-a-deck", "Wide", "9,9", 5),      # unknown serial
    ]
    for request in requests:
        through_method = outcome(lambda r=request: top.ChangeState(*r))
        through_service = outcome(lambda r=request: plane.change_state(*r))
        assert through_method == through_service, (
            f"request {request} came out differently through the DBus method "
            f"({through_method}) than through the service ({through_service}) "
            f"-- the transport has grown rules of its own")
        result = plane.change_state(*request)
        assert top.ChangeState(*request) == ("" if result.ok else result.message), (
            f"the method's answer for {request} is not the service's: {result}")

    print("PASS: the state method and the service core reach the same outcome")


def check_other_decks_are_untouched(plane, controller_a, controller_b) -> None:
    """A request names one deck.

    The second controller is here anyway, so pinning that it stays out of the
    request costs nothing.
    """
    load_named_page(controller_a, "Alpha")
    settle(controller_b)
    raw_b = fixtures.raw_deck(controller_b)
    page_b = controller_b.active_page
    seq_b = raw_b.current_seq()

    assert plane.change_page(SERIAL_A, "Beta").ok
    assert fixtures.wait_until(lambda: active_name(controller_a) == "Beta")
    assert plane.change_state(SERIAL_A, "Alpha", "0,0", 0).ok
    time.sleep(0.3)

    assert controller_b.active_page is page_b, (
        f"a request naming one deck moved another one's page: "
        f"{active_name(controller_b)}")
    assert raw_b.current_seq() == seq_b, (
        f"a request naming one deck wrote to another: "
        f"{[(e[2], e[3]) for e in raw_b.ops_after(seq_b)]}")

    print("PASS: a request reaches only the deck it names")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_control_plane")

    from src.backend import control_plane

    plane = control_plane.get()

    fixtures.seed_page("Alpha")
    fixtures.seed_page("Beta")
    seed_multistate_page("Wide", WIDE_KEY, WIDE_STATES)

    controller_a = fixtures.make_headless_controller(serial=SERIAL_A)
    controller_b = fixtures.make_headless_controller(serial=SERIAL_B, key_layout=[10, 10])
    try:
        check_unknown_serial(plane)
        check_unknown_page(plane, controller_a)
        check_unbuildable_page_leaves_deck_alone(plane, controller_a)
        check_same_page_noop_over_dbus(controller_a)
        check_load_only_if_different(plane, controller_a)
        check_device_truth_bounds(plane, controller_b)
        check_other_decks_are_untouched(plane, controller_a, controller_b)
        check_validation_failures_are_results(plane, controller_a)
        check_unexpected_exception_propagates(plane, controller_a)
        check_dbus_delegate_matches_service(plane, controller_a)
        check_state_delegate_matches_service(plane, controller_b)
    finally:
        fixtures.teardown(controller_b)
        fixtures.teardown(controller_a)

    print("ALL PASS: scenario_control_plane")


if __name__ == "__main__":
    main()
