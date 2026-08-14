"""Pins the CLI half of the control plane in src/backend/cli_forward.py.

Every page and state request is forwarded or parked. Validation is syntax
only and all-or-nothing, and a failure never stops the requests behind it.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import globals as gl  # noqa: E402

from cli_args import argparser  # noqa: E402

from src.backend import cli_forward  # noqa: E402

WATCHDOG_SECONDS = 60

# One command carrying three page requests and two state requests. Every one
# names a different deck, so a dropped request cannot hide behind
# last-write-wins parking.
ARGV = [
    "--change-page", "deck-a", "Alpha",
    "--change-page", "deck-b", "Beta",
    "--change-page", "deck-c", "Gamma",
    "--change-state", "deck-d", "Delta", "0,0", "1",
    "--change-state", "deck-e", "Epsilon", "1,2", "3",
]

EXPECTED_FORWARDS = [
    ("page", "deck-a", "Alpha"),
    ("page", "deck-b", "Beta"),
    ("page", "deck-c", "Gamma"),
    ("state", "deck-d", "Delta", "0,0", 1),
    ("state", "deck-e", "Epsilon", "1,2", 3),
]


def parse(argv: list[str]):
    """The app's own parser, so what is tested below is what argv produces."""
    return argparser.parse_args(argv)


def clear_parking() -> None:
    gl.api_page_requests.clear()
    gl.api_state_requests.clear()


class Recorder:
    """The running instance, without a bus.

    Records every call the forwarder makes, in order. It answers the way the
    real methods do, with an empty string on success and a sentence on failure.
    """

    def __init__(self, running: bool = True, answers: dict | None = None,
                 raises: Exception | None = None):
        self._running = running
        self._answers = answers or {}
        self._raises = raises
        self.calls: list[tuple] = []

    def is_running(self) -> bool:
        self.calls.append(("is_running",))
        return self._running

    def change_page(self, serial: str, page: str) -> str:
        self.calls.append(("page", serial, page))
        return self._answer(serial)

    def change_state(self, serial: str, page: str, coords: str, state: int) -> str:
        self.calls.append(("state", serial, page, coords, state))
        return self._answer(serial)

    def _answer(self, serial: str) -> str:
        if self._raises is not None:
            raise self._raises
        return self._answers.get(serial, "")

    def forwards(self) -> list[tuple]:
        return [call for call in self.calls if call[0] != "is_running"]


# 1. Every request is forwarded

def check_all_requests_are_forwarded() -> None:
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert recorder.forwards() == EXPECTED_FORWARDS, (
        f"the running instance was sent {recorder.forwards()} instead of "
        f"{EXPECTED_FORWARDS} -- a command's later requests are being dropped, "
        f"which is what returning after the first forwarded request did")
    assert verdict.handled, (
        "the instance took the requests, so this invocation is finished -- "
        "returning otherwise boots a second instance")
    assert verdict.failures == [], verdict.failures
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"forwarded requests must not also be parked: {gl.api_page_requests} / "
        f"{gl.api_state_requests}")

    # The state travelled as an integer. The bus method signature says so, and
    # the conversion is the job of the CLI edge.
    for call in recorder.forwards():
        if call[0] == "state":
            assert isinstance(call[4], int) and not isinstance(call[4], bool), (
                f"the state reached the transport as {call[4]!r}, not an int")

    print("PASS: every page and state request on the command line is forwarded")


# 2 and 3. Parking

def check_nothing_running_parks_everything() -> None:
    clear_parking()
    recorder = Recorder(running=False)

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert not verdict.handled, (
        "with nothing running this invocation becomes the instance, so it must "
        "carry on booting")
    assert verdict.failures == [], verdict.failures
    assert recorder.forwards() == [], (
        f"nothing may be forwarded when nothing is running: {recorder.forwards()}")

    assert gl.api_page_requests == {
        "deck-a": "Alpha", "deck-b": "Beta", "deck-c": "Gamma",
    }, gl.api_page_requests
    # The exact dict the boot path reads back, with state already an int.
    assert gl.api_state_requests == {
        "deck-d": {"page_name": "Delta", "coords": "0,0", "state": 1},
        "deck-e": {"page_name": "Epsilon", "coords": "1,2", "state": 3},
    }, gl.api_state_requests

    print("PASS: with no instance running every request is parked for the boot")


def check_close_running_parks_requests() -> None:
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(
        parse(ARGV + ["--close-running"]), recorder)

    assert not verdict.handled, (
        "--close-running boots over the instance it stops, so its requests are "
        "this process's to apply")
    assert recorder.forwards() == [], (
        f"--close-running must not hand requests to the instance it is about "
        f"to shut down: {recorder.forwards()}")
    assert len(gl.api_page_requests) == 3 and len(gl.api_state_requests) == 2, (
        f"{gl.api_page_requests} / {gl.api_state_requests}")

    print("PASS: --close-running parks its requests even with an instance running")


# 4 and 5. What the instance says comes back

def check_failures_surface_without_stopping() -> None:
    clear_parking()
    refusal = "Page 'Beta' not found. Available pages: Alpha, Gamma"
    unknown_deck = "StreamDeck with serial 'deck-d' not found. Available devices: deck-a"
    recorder = Recorder(running=True,
                        answers={"deck-b": refusal, "deck-d": unknown_deck})

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert verdict.failures == [refusal, unknown_deck], (
        f"the instance's own sentences are what the person who typed the "
        f"command has to see: {verdict.failures}")
    assert recorder.forwards() == EXPECTED_FORWARDS, (
        f"a request the instance refused must not stop the ones behind it: "
        f"{recorder.forwards()}")

    print("PASS: refusals come back in the verdict without stopping the command")


def check_older_instance_reports_once() -> None:
    clear_parking()
    recorder = Recorder(running=True, raises=cli_forward.OlderInstance())

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert verdict.failures == [cli_forward.SKEW_MESSAGE], verdict.failures
    assert "restart" in cli_forward.SKEW_MESSAGE, (
        "the message has to say what to do about it")
    assert "older build" in cli_forward.SKEW_MESSAGE, cli_forward.SKEW_MESSAGE
    assert "shutting down" in cli_forward.SKEW_MESSAGE, (
        "an instance takes its interface off the bus before it lets go of the "
        "name, so it answers identically to one too old to have the methods -- "
        "and that is now the only other way to reach this")
    assert "finished starting" not in cli_forward.SKEW_MESSAGE, (
        "a starting instance publishes its objects BEFORE it takes the name, "
        "so it cannot answer this way at all any more; telling people to wait "
        "for a startup to finish would send them to wait for nothing")
    assert len(recorder.forwards()) == 1, (
        f"every request would fail identically, so the rest are pointless: "
        f"{recorder.forwards()}")
    assert verdict.handled, (
        "an instance IS running -- booting a second one over it would be worse "
        "than the failure being reported")
    assert not gl.api_page_requests and not gl.api_state_requests, (
        "a version-skewed forward must not silently fall back to parking, "
        "which would apply the requests to a DIFFERENT process later")

    print("PASS: an instance without the methods is reported once, and clearly")


def check_broken_conversation_reported() -> None:
    """A wedged instance, a dropped connection or a refused call reads back.

    The bus text must reach the terminal. Left as the GLib error it starts as,
    it escapes the CLI, prints a traceback and still exits zero.
    """
    clear_parking()
    broke = cli_forward.TransportError(
        "GDBus.Error:org.freedesktop.DBus.Error.NoReply: Message did not "
        "receive a reply (timeout by message bus)")
    recorder = Recorder(running=True, raises=broke)

    verdict = cli_forward.forward_cli_requests(parse(ARGV), recorder)

    assert verdict.failures == [str(broke)], (
        f"a broken conversation has to come back as something printable: "
        f"{verdict.failures}")
    assert verdict.handled, (
        "an instance is running -- booting a second one over a bus hiccup "
        "would be worse than the failure being reported")
    assert len(recorder.forwards()) == 1, (
        f"the remaining requests would each spend the call timeout again "
        f"against something that is not answering: {recorder.forwards()}")
    assert not gl.api_page_requests and not gl.api_state_requests, (
        "a failed forward must not silently fall back to parking, which would "
        "apply the requests to a DIFFERENT process later")

    print("PASS: a failed conversation with the instance is reported, not raised")


# 6 and 7. Syntax only

def check_validation_is_syntax_only() -> None:
    bad = [
        ["--change-state", "deck-a", "Alpha", "nope", "1"],       # no comma
        ["--change-state", "deck-a", "Alpha", "1,2,3", "1"],      # three of them
        ["--change-state", "deck-a", "Alpha", "", "1"],           # nothing at all
        ["--change-state", "deck-a", "Alpha", "x,y", "1"],        # not numbers
        ["--change-state", "deck-a", "Alpha", "-1,0", "1"],       # before the first key
        ["--change-state", "deck-a", "Alpha", "0,0", "one"],      # not a number
        ["--change-state", "deck-a", "Alpha", "0,0", "-1"],       # before the first state
        ["--change-state", "", "Alpha", "0,0", "1"],              # no deck named
        ["--change-state", "deck-a", "", "0,0", "1"],             # no page named
    ]
    for argv in bad:
        clear_parking()
        recorder = Recorder(running=True)
        verdict = cli_forward.forward_cli_requests(parse(argv), recorder)
        assert verdict.failures, f"{argv} was accepted"
        assert verdict.failures[0].startswith("Error: "), verdict.failures
        assert cli_forward.USAGE in verdict.failures, (
            f"a malformed command has to be shown the shape of a good one: "
            f"{verdict.failures}")
        assert not verdict.handled, argv
        assert recorder.calls == [], (
            f"{argv} reached the bus before it was read: {recorder.calls}")
        assert not gl.api_page_requests and not gl.api_state_requests, (
            f"{argv} parked something despite being rejected")

    # All-or-nothing. One malformed group leaves the whole command unapplied,
    # because a partly applied command is the worst of the three outcomes.
    clear_parking()
    recorder = Recorder(running=True)
    verdict = cli_forward.forward_cli_requests(
        parse(["--change-page", "deck-a", "Alpha",
               "--change-state", "deck-b", "Beta", "0,0", "1",
               "--change-state", "deck-c", "Gamma", "nope", "1"]), recorder)
    assert verdict.failures and recorder.calls == [], recorder.calls
    assert not gl.api_page_requests and not gl.api_state_requests, (
        f"a command with one bad group applied part of itself: "
        f"{gl.api_page_requests} / {gl.api_state_requests}")

    print("PASS: syntax failures reject the whole command before anything happens")


def check_large_decks_not_pre_rejected() -> None:
    """Large coordinates and state numbers travel to the instance untouched.

    A cap of x,y <= 10 and state <= 20 matches no device and rejects valid
    requests for large decks before anything that knows a deck sees them.
    """
    argv = ["--change-state", "deck-a", "Alpha", "9,9", "19",
            "--change-state", "deck-b", "Beta", "14,7", "31"]

    clear_parking()
    recorder = Recorder(running=True)
    verdict = cli_forward.forward_cli_requests(parse(argv), recorder)
    assert verdict.failures == [], verdict.failures
    assert recorder.forwards() == [
        ("state", "deck-a", "Alpha", "9,9", 19),
        ("state", "deck-b", "Beta", "14,7", 31),
    ], recorder.forwards()

    # The same on the parking path, so no boot-time request is judged by
    # numbers the CLI made up.
    clear_parking()
    verdict = cli_forward.forward_cli_requests(parse(argv), Recorder(running=False))
    assert verdict.failures == [], verdict.failures
    assert gl.api_state_requests["deck-a"]["state"] == 19, gl.api_state_requests
    assert gl.api_state_requests["deck-b"]["coords"] == "14,7", gl.api_state_requests

    print("PASS: coordinates and states the device decides on are passed through")


# 8. Nothing asked for

def check_no_requests_touches_nothing() -> None:
    clear_parking()
    recorder = Recorder(running=True)

    verdict = cli_forward.forward_cli_requests(parse(["--devel"]), recorder)

    assert not verdict.handled and verdict.failures == [], verdict
    assert recorder.calls == [], (
        f"a launch that asks for no page or state change must not go near the "
        f"bus: {recorder.calls}")
    assert not gl.api_page_requests and not gl.api_state_requests

    print("PASS: an invocation with no requests probes nothing and parks nothing")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_cli_forward_all")
    try:
        check_all_requests_are_forwarded()
        check_nothing_running_parks_everything()
        check_close_running_parks_requests()
        check_failures_surface_without_stopping()
        check_older_instance_reports_once()
        check_broken_conversation_reported()
        check_validation_is_syntax_only()
        check_large_decks_not_pre_rejected()
        check_no_requests_touches_nothing()
    finally:
        clear_parking()

    print("ALL PASS: scenario_cli_forward_all")


if __name__ == "__main__":
    main()
