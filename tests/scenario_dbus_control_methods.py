"""Pins the top-level DBus control methods ChangePage and ChangeState.

They run over a real bus against real controllers. The answer matters as much
as the effect. An empty string means done; anything else is a sentence to read.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402

from gi.repository import GLib  # noqa: E402

import appinfo  # noqa: E402
import globals as gl  # noqa: E402

import src.api as api  # noqa: E402

# The isolated-daemon harness, shared rather than copied. Importing it only
# defines helpers, because its own legs run under its __main__ guard.
import scenario_api_lifecycle_publish as harness  # noqa: E402

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

WATCHDOG_SECONDS = 80

SERIAL = "dbus-ctl-1"
OTHER_SERIAL = "dbus-ctl-2"
STATE_KEY = "0x0"
STATE_COUNT = 4


def seed_multistate_page(page_name: str, key_ident: str, n_states: int) -> str:
    """Seed a page whose key_ident carries n_states states.

    The methods are then checked against the input's real state count.
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


def active_name(controller) -> str | None:
    page = controller.active_page
    return None if page is None else page.get_name()


class Client:
    """The two methods, as a client calls them."""

    def __init__(self, observer):
        self._observer = observer

    def change_page(self, serial: str, page: str) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "ChangePage",
            GLib.Variant("(ss)", (serial, page)))
        return reply.unpack()[0]

    def change_state(self, serial: str, page: str, coords: str, state: int) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, api.TOP_IFACE, "ChangeState",
            GLib.Variant("(sssi)", (serial, page, coords, state)))
        return reply.unpack()[0]

    def introspect(self) -> str:
        reply = self._observer.call(
            api.DBUS_OBJECT_PATH, "org.freedesktop.DBus.Introspectable",
            "Introspect", None)
        return reply.unpack()[0]


# Legs

def leg_change_page(client, controller) -> None:
    assert client.change_page(SERIAL, "Alpha") == "", (
        "a switch that worked must answer with nothing at all -- the CLI reads "
        "any text as a failure to print")
    assert active_name(controller) == "Alpha", (
        f"ChangePage answered success without switching the deck: "
        f"{active_name(controller)}")

    assert client.change_page(SERIAL, "Beta") == ""
    assert active_name(controller) == "Beta"
    print("  PASS: ChangePage switches the deck it names")


def leg_already_active_is_success(client, controller) -> None:
    """Asking for the page already showing is a fulfilled request.

    A script that sets a page on every event asks for it constantly, and the
    method must not reload.
    """
    assert active_name(controller) == "Beta", "this leg starts from a known page"

    original_load_page = controller.load_page
    loads: list = []

    def counting_load_page(page, *args, **kwargs):
        loads.append(page)
        return original_load_page(page, *args, **kwargs)

    controller.load_page = counting_load_page
    try:
        assert client.change_page(SERIAL, "Beta") == "", (
            "the deck already showing the page is a fulfilled request, not an "
            "error to report")
        assert loads == [], f"the already-active page was reloaded: {loads}"
    finally:
        del controller.load_page
    print("  PASS: asking for the active page answers success and loads nothing")


def leg_errors_name_what_exists(client, controller) -> None:
    unknown_deck = client.change_page("not-a-deck", "Alpha")
    assert unknown_deck, "an unknown serial must not answer success"
    assert SERIAL in unknown_deck and OTHER_SERIAL in unknown_deck, (
        f"the failure must name the decks that ARE connected: {unknown_deck!r}")

    unknown_page = client.change_page(SERIAL, "no-such-page")
    assert unknown_page, "an unknown page must not answer success"
    assert "Alpha" in unknown_page and "Beta" in unknown_page, (
        f"the failure must list the pages that exist: {unknown_page!r}")
    assert active_name(controller) == "Beta", (
        f"a rejected request must leave the deck alone: {active_name(controller)}")
    print("  PASS: failures come back as sentences that name what does exist")


def leg_change_state(client, controller) -> None:
    assert client.change_state(SERIAL, "States", STATE_KEY.replace("x", ","), 2) == "", (
        "a state change that worked must answer with nothing")
    assert active_name(controller) == "States", (
        "ChangeState loads the page whose input it is addressing")

    c_input = controller.get_input(Input.Key(STATE_KEY))
    assert c_input is not None
    assert c_input.state == 2, f"the input is on state {c_input.state}, not 2"
    print("  PASS: ChangeState loads the page and sets the input's state")


def leg_state_errors(client, controller) -> None:
    c_input = controller.get_input(Input.Key(STATE_KEY))

    too_high = client.change_state(SERIAL, "States", "0,0", STATE_COUNT)
    assert too_high, "a state past the end of the input must not answer success"
    assert f"{STATE_COUNT} states" in too_high, too_high
    assert c_input.state == 2, "a rejected state must not have been applied"

    off_device = client.change_state(SERIAL, "States", "99,0", 0)
    assert off_device and "out of bounds" in off_device, off_device

    unparsable = client.change_state(SERIAL, "States", "nope", 0)
    assert unparsable and "x,y" in unparsable, unparsable

    unknown_deck = client.change_state("not-a-deck", "States", "0,0", 0)
    assert unknown_deck and SERIAL in unknown_deck, unknown_deck

    assert active_name(controller) == "States", (
        "a rejected state change must not move the page either")
    print("  PASS: every rejected state change answers with its reason")


def leg_signatures_match_cli(client) -> None:
    """The published signatures are the ones the CLI composes calls from.

    The CLI builds the (ss) and (sssi) variants by hand, and a wire signature
    has no other guard, so a change here fails at the bus.
    """
    xml = client.introspect()
    assert '<method name="ChangePage">' in xml, xml
    assert '<method name="ChangeState">' in xml, xml

    def signature(method: str) -> tuple[str, str]:
        block = xml.split(f'<method name="{method}">')[1].split("</method>")[0]
        args = [line for line in block.splitlines() if "<arg" in line]
        in_args = "".join(a.split('type="')[1].split('"')[0] for a in args
                          if 'direction="in"' in a)
        out_args = "".join(a.split('type="')[1].split('"')[0] for a in args
                           if 'direction="out"' in a)
        return in_args, out_args

    assert signature("ChangePage") == ("ss", "s"), signature("ChangePage")
    assert signature("ChangeState") == ("sssi", "s"), signature("ChangeState")
    print("  PASS: the published signatures are the ones the CLI calls with")


def drive_on_worker(work, name: str, timeout: float = 30.0) -> dict:
    """Run work(answers) on a thread while this one pumps, and return records.

    The CLI transport calls call_sync, which blocks until the reply lands, and
    the reply comes from this process's main context. A worker matches the real
    shape, where the CLI is a different process.
    """
    answers: dict = {}

    def run() -> None:
        try:
            work(answers)
        except BaseException as e:  # surfaced by the caller's assertions
            answers["error"] = e
        answers["done"] = True

    worker = threading.Thread(target=run, name=name)
    worker.start()
    harness.pump_until(lambda: answers.get("done"), timeout,
                       f"{name} never finished its calls")
    worker.join(timeout=10)
    assert not worker.is_alive(), f"{name} did not return"
    assert "error" not in answers, f"{name} raised: {answers['error']!r}"
    return answers


def leg_cli_transport_reaches_service(controller) -> None:
    """The CLI's own transport, against the real service.

    This object composes its own variants, addresses the app's well-known name
    and carries the CLI timeout. It runs on a worker because call_sync blocks
    and this thread must pump. Every request names a serial local to this run.
    """
    from src.backend import cli_forward

    assert os.environ.get("DBUS_SESSION_BUS_ADDRESS"), \
        "the isolated bus address is not in the environment this transport reads"

    transport = cli_forward._BusTransport()

    def drive(answers: dict) -> None:
        answers["running"] = transport.is_running()
        answers["page"] = transport.change_page(SERIAL, "Alpha")
        # Read where the deck ended up before the next call moves it. The page
        # was loaded before the reply was sent, so this is settled.
        answers["after_page"] = active_name(controller)
        answers["bad_page"] = transport.change_page(SERIAL, "no-such-page")
        answers["state"] = transport.change_state(SERIAL, "States", "0,0", 1)

    answers = drive_on_worker(drive, "cli-transport")

    assert answers["running"] is True, (
        "the app's name is owned on this bus, so the CLI must see an instance "
        "to forward to")
    assert answers["page"] == "", answers["page"]
    assert answers["after_page"] == "Alpha", (
        f"the CLI transport's request never reached the deck: "
        f"{answers['after_page']}")
    assert "no-such-page" in answers["bad_page"], answers["bad_page"]
    assert answers["state"] == "", answers["state"]
    assert active_name(controller) == "States", (
        f"the state request must have loaded its own page: "
        f"{active_name(controller)}")
    assert controller.get_input(Input.Key(STATE_KEY)).state == 1, (
        "the CLI transport's state change never reached the input")
    print("  PASS: the CLI's own transport drives this service end to end")


def leg_instance_never_answers(controller) -> None:
    """What the CLI says when the methods are not on the bus.

    GDBus reports a missing object the same way it reports a missing method, so
    a build without the methods and an instance shutting down look alike. The
    name stays owned here and the top-level object goes off the bus.
    """
    from src.backend import cli_forward

    transport = cli_forward._BusTransport()
    page_before = active_name(controller)

    # Phase 1. The object is gone and the name is not, which is what a client
    # meets while an instance tears down. The registrations are mutated here on
    # the main context, as everything else that touches them does.
    api._bus.unpublish_object(api.DBUS_OBJECT_PATH)

    def drive_missing_object(answers: dict) -> None:
        answers["running"] = transport.is_running()
        # Drive the whole forwarding path, so the assertion covers the sentence
        # a person reads rather than a constant this file names.
        answers["failures"] = cli_forward._forward(
            transport, [(SERIAL, "Alpha")], [(SERIAL, "States", "0,0", 1)])

    no_objects = drive_on_worker(drive_missing_object, "cli-transport-no-objects")

    api._bus.publish_object(api.DBUS_OBJECT_PATH, api._api_instance)

    # Phase 2. The genuinely older build has the object and not the method. The
    # third case is a call the service refuses for another reason, which must
    # not be mistaken for either.
    def drive_missing_method(answers: dict) -> None:
        try:
            transport._call("NoSuchMethod",
                            GLib.Variant("(ss)", (SERIAL, "Alpha")))
            answers["missing_method"] = None
        except cli_forward.OlderInstance as e:
            answers["missing_method"] = e
        try:
            transport._call("ChangePage", GLib.Variant("(s)", (SERIAL,)))
            answers["refused"] = None
        except cli_forward.TransportError as e:
            answers["refused"] = e

    older = drive_on_worker(drive_missing_method, "cli-transport-missing-method")

    assert no_objects["running"] is True, (
        "the name is owned -- an instance with nothing published looks exactly "
        "as running as any other, which is why this state is reachable")
    assert no_objects["failures"] == [cli_forward.SKEW_MESSAGE], no_objects["failures"]
    assert "older build" in cli_forward.SKEW_MESSAGE, cli_forward.SKEW_MESSAGE
    assert "shutting down" in cli_forward.SKEW_MESSAGE, (
        f"the state this leg just produced is also what tearing down looks "
        f"like from outside, and it is now the only other way to reach it: "
        f"{cli_forward.SKEW_MESSAGE!r}")
    assert "finished starting" not in cli_forward.SKEW_MESSAGE, (
        f"an instance that is starting cannot answer this way any more -- it "
        f"publishes before it takes the name -- so offering that reading sends "
        f"people to wait for something that already happened: "
        f"{cli_forward.SKEW_MESSAGE!r}")
    assert isinstance(older["missing_method"], cli_forward.OlderInstance), (
        f"a method this build does not have must reach the same answer: "
        f"{older['missing_method']!r}")
    assert isinstance(older["refused"], cli_forward.TransportError), (
        f"a call refused for any other reason must arrive as something the CLI "
        f"can print, not as a toolkit error nobody catches: "
        f"{older['refused']!r}")
    assert str(older["refused"]), "the refusal came back with nothing to say"
    assert active_name(controller) == page_before, (
        f"nothing above was applied, so the deck must not have moved: "
        f"{active_name(controller)}")
    print("  PASS: an instance with no methods on the bus is reported for the "
          "two things that now means")


def run_legs(bus_address: str, controller, other) -> None:
    api.start_dbus_service()
    assert api._bus is not None, "the DBus service did not start"
    # GApplication owns the app name in the running app. This stands in for it,
    # so the CLI transport leg can address the name it addresses in the field
    # rather than a unique connection name.
    api._bus.register_service(appinfo.APP_ID)
    observer = harness.Observer(bus_address, api._bus.connection.get_unique_name())
    client = Client(observer)
    # Whatever the second deck booted onto, it must still be showing it. Every
    # request below names one deck.
    other_page = other.active_page
    assert other_page is not None, "the second deck never loaded a page"
    try:
        leg_change_page(client, controller)
        leg_already_active_is_success(client, controller)
        leg_errors_name_what_exists(client, controller)
        leg_change_state(client, controller)
        leg_state_errors(client, controller)
        leg_signatures_match_cli(client)
        leg_cli_transport_reaches_service(controller)
        leg_instance_never_answers(controller)

        assert other.active_page is other_page, (
            f"every request above named one deck; the other one moved anyway: "
            f"{active_name(other)}")
    finally:
        api.stop_dbus_service()
        observer.connection.close_sync(None)


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_dbus_control_methods")
    fixtures._install_integration_globals()
    fixtures.seed_page("Main")
    fixtures.seed_page("Alpha")
    fixtures.seed_page("Beta")
    seed_multistate_page("States", STATE_KEY, STATE_COUNT)

    bus_proc, bus_address = harness.start_private_bus()
    controller = fixtures.make_headless_controller(serial=SERIAL)
    other = fixtures.make_headless_controller(serial=OTHER_SERIAL)
    try:
        run_legs(bus_address, controller, other)
    finally:
        fixtures.teardown(other)
        fixtures.teardown(controller)
        harness.stop_private_bus(bus_proc)

    print("PASS: scenario_dbus_control_methods")


if __name__ == "__main__":
    main()
