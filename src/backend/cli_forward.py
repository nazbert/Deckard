"""The CLI half of the control plane. This decides what an invocation that
carries --change-page or --change-state does with the requests it was given.

An invocation that names a deck has two different jobs, by whether Deckard
already runs. With nothing running, and with --close-running, which is about
to stop whatever runs, the invocation becomes the instance. It parks its
requests for the deck that has not enumerated yet, and goes on to boot; see
legs B and C in src/backend/startup_queue.py. With an instance running, the
requests go over the bus to that instance and this process ends.

The choice between the two comes before the process knows whether it becomes
the instance. Parking serves the boot that follows it, so it cannot wait for
the name to settle. A launch that parks and then loses that race holds
requests that belong to another process, and forward_parked_requests takes
them there rather than let them die with this one.

Why this is not in main.py

Nothing can import main.py, because its module body re-execs the process and
runs the rebrand migration against the real user directories. Everything that
lived there was untestable, and it showed. Forwarding returned after the first
request it sent, so a command that carried several page or state changes
applied one and dropped the rest. This module is the extraction that makes
that provable. main.py keeps the two things that cannot leave it, which are
the argv read and the process exit.

Syntax here, meaning there

This file answers only what needs no device. Do the coordinates read as two
non-negative integers, and does the state read as a non-negative integer. The
running instance answers whether (9,9) sits on the deck, whether state 19
exists on that input, and whether the page or the serial is real, and it
answers from the device itself. The caps this file once applied, of
coordinates at most 10 and state at most 20, matched no hardware and rejected
valid requests for a large deck before anything with a device saw them.

The caller must see a failure

Forwarding once travelled over a Gio action and expected no answer, so a
mistyped page name produced a line in the running instance's log and silence
in the terminal that typed it. The bus methods answer with a sentence, and
those sentences reach the verdict for main.py to print. A failed request does
not stop the ones behind it. Each one is independent, and a person who asked
for four changes is better served by three applied and one explained than by
a prefix.

Version skew, in both directions

Forwarding once travelled over Gio actions, which are gone. A CLI from an
older install therefore forwards a change_page or change_state action that a
current instance does not register, and org.gtk.Actions answers an unknown
action by doing nothing, so that direction fails in silence and this side
cannot change it. It needs both a current and an older install on one machine,
it ends as soon as the old install goes, and the parked boot path stays
correct, so this documents it rather than defends against it. The other
direction, a current CLI that meets an instance without the methods, can
speak, and it says SKEW_MESSAGE.

The transport is injectable

forward_cli_requests takes the object that talks to the running instance, so a
test drives the forwarding rules without a bus, a daemon or a second process.
The default is the real one, and it imports the toolkit when it is
constructed rather than at module import, which keeps this module's body
importable on the deployment floor with the standard library alone.
scenario_floor_import executes it there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import appinfo
from src.backend import startup_queue

if TYPE_CHECKING:
    from argparse import Namespace
    from typing import Protocol

    class Transport(Protocol):
        """What forwarding needs from the running instance."""

        def is_running(self) -> bool:
            """Is there an instance to forward to?"""

        def change_page(self, serial: str, page: str) -> str:
            """Empty on success, else the reason the instance gave."""

        def change_state(self, serial: str, page: str, coords: str,
                         state: int) -> str:
            """Empty on success, else the reason the instance gave."""


# Every CLI-side D-Bus call carries this rather than the 25s bus default.
# Without it a wedged instance that accepts and never replies blocks startup
# for that long. The probes in main.py import it from here, so one number
# serves both and no two numbers drift apart.
DBUS_CALL_TIMEOUT_MS = 5000

# The interface that the top-level object carries, which is the app id.
TOP_IFACE = appinfo.APP_ID

# What comes back when the methods are absent. GDBus uses these two spellings
# for a missing method, by its version, and for a missing object as well. It
# answers both from its own worker thread, so they arrive promptly whether or
# not the instance dispatches anything, which measurement confirms.
#
# A starting instance does not answer this way. The app publishes its objects
# before it takes the bus name (src/backend/instance_gate.py), so a name owned
# by such a build always has the methods behind it. Two cases still reach here. A build too old to carry
# them, and an instance on its way out, because teardown takes the interface
# off the bus first and keeps the name until the process ends. An instance
# whose publish failed looks the same, and says so in its own log. A wedged
# instance answers differently. Its objects are up, the call queues behind
# whatever blocks the loop, and the caller gets the timeout below.
_METHOD_MISSING = (
    "org.freedesktop.DBus.Error.UnknownMethod",
    "org.freedesktop.DBus.Error.UnknownInterface",
)

SKEW_MESSAGE = (
    "The running Deckard is not answering to the page and state methods this "
    "command needs. An older build does not have them at all, and restarting "
    "it is what picks up a build that does. An instance that is shutting down "
    "answers the same way until it is gone, so if one was just quitting, try "
    "again in a moment."
)

USAGE = """
Usage examples:
  --change-state CL123456789 Main 0,0 1
  --change-state CL123456789 Soundboard 2,1 0

Parameters:
  SERIAL_NUMBER: Device serial (e.g., CL123456789)
  PAGE_NAME: Page name (e.g., Main, Soundboard)
  COORDINATES: Position as x,y (e.g., 0,0 for top-left)
  STATE_NUMBER: State to change to (e.g., 0, 1, 2)"""


class OlderInstance(Exception):
    """The running instance carries no control methods on the bus. See
    _METHOD_MISSING for the cases that reach this."""


class TransportError(Exception):
    """The conversation with the running instance failed. It never replied,
    the connection went away, or it refused the call. This carries the bus's
    own text, so nothing outside this module needs to know a GLib.Error."""


@dataclass(frozen=True)
class Verdict:
    """What the invocation should do next.

    handled means that a running instance took the requests and this process
    has nothing left to do. False means that the requests are parked, or that
    none arrived, and this process boots on. failures holds the sentences for
    the person who typed the command. A non-empty list marks a failed
    invocation, whatever handled says.
    """

    handled: bool = False
    failures: list[str] = field(default_factory=list)


# Syntax

def _parse_state_requests(raw: list) -> tuple[list[tuple[str, str, str, int]],
                                              list[str]]:
    """Read the --change-state groups into (serial, page, coords, state).

    All or nothing. A command with one malformed group applies none of its
    requests, because a part-applied command costs more than either other
    outcome. This judges shapes alone; see the module docstring on why it
    judges no bounds.
    """
    parsed: list[tuple[str, str, str, int]] = []
    failures: list[str] = []
    for i, (serial_number, page_name, coords, state_number) in enumerate(raw):
        where = f"argument {i + 1}"
        if not serial_number:
            failures.append(f"Error: Invalid serial number in {where}: '{serial_number}'")
            continue
        if not page_name:
            failures.append(f"Error: Invalid page name in {where}: '{page_name}'")
            continue
        if not coords or "," not in coords:
            failures.append(
                f"Error: Invalid coordinate format in {where}: '{coords}'. "
                f"Expected format: 'x,y' (e.g., '0,0')")
            continue
        try:
            x, y = (int(part) for part in coords.split(","))
        except ValueError:
            failures.append(
                f"Error: Invalid coordinate format in {where}: '{coords}'. "
                f"Expected integers like '0,0'")
            continue
        if x < 0 or y < 0:
            failures.append(f"Error: Coordinates must be non-negative in {where}: '{coords}'")
            continue
        try:
            state = int(state_number)
        except ValueError:
            failures.append(
                f"Error: Invalid state number in {where}: '{state_number}'. "
                f"Must be an integer")
            continue
        if state < 0:
            failures.append(
                f"Error: State number must be non-negative in {where}: '{state_number}'")
            continue
        parsed.append((serial_number, page_name, coords, state))
    return parsed, failures


# The two things an invocation can do with its requests

def _park(page_requests: list, state_requests: list[tuple[str, str, str, int]]) -> None:
    """Hand every request to the startup queue, for the decks this process
    enumerates next. The serial keys each request and the last write wins,
    which is the parking contract rather than an effect of this loop."""
    queue = startup_queue.get()
    for serial_number, page_name in page_requests:
        queue.park_page_request(serial_number, page_name)
    for serial_number, page_name, coords, state in state_requests:
        queue.park_state_request(serial_number, {
            "page_name": page_name,
            "coords": coords,
            "state": state,
        })


def _forward(transport: Transport, page_requests: list,
             state_requests: list[tuple[str, str, str, int]]) -> list[str]:
    """Send every request to the running instance and collect what it said.

    This sends every request. A return after the first send moves deck A and
    leaves deck B alone for --change-page A X --change-page B Y, and it keeps
    a --change-state on a command that also carries a --change-page inside
    the process.

    The order is every page request as argv gave them, then every state
    request as argv gave them. argparse collects the two flags into two lists,
    so nothing here recovers how they interleaved, and this is the order the
    CLI applies.
    """
    failures: list[str] = []
    try:
        for serial_number, page_name in page_requests:
            message = transport.change_page(serial_number, page_name)
            if message:
                failures.append(message)
        for serial_number, page_name, coords, state in state_requests:
            message = transport.change_state(serial_number, page_name, coords, state)
            if message:
                failures.append(message)
    except OlderInstance:
        # Every request behind this one fails the same way, and one answer
        # covers all of them.
        failures.append(SKEW_MESSAGE)
    except TransportError as e:
        # The conversation broke. It does not heal inside one command, and a
        # retry of each remaining request spends the call timeout again per
        # request against an instance that answers nothing. Report the failure
        # once and drop the rest.
        failures.append(str(e))
    return failures


def forward_cli_requests(args: Namespace,
                         transport: Transport | None = None) -> Verdict:
    """Apply the invocation's --change-page and --change-state requests.

    It parks them for this process to pick up as it boots, or forwards them to
    the instance already running; see the module docstring. It never exits and
    never prints. The verdict says what happened, and main.py owns both.
    """
    page_requests = args.change_page or []
    raw_state_requests = args.change_state or []
    if not page_requests and not raw_state_requests:
        return Verdict()

    state_requests, failures = _parse_state_requests(raw_state_requests)
    if failures:
        # Nothing is parked or sent yet, and nothing gets parked or sent.
        return Verdict(handled=False, failures=failures + [USAGE])

    if transport is None:
        transport = _BusTransport()

    if not transport.is_running() or args.close_running:
        _park(page_requests, state_requests)
        return Verdict(handled=False)

    return Verdict(handled=True,
                   failures=_forward(transport, page_requests, state_requests))


def forward_parked_requests(transport: Transport | None = None) -> list[str]:
    """Hand this process's parked requests to the instance that owns the name.

    This serves the launch that parked and then lost. Parking happens before
    the race for the application name settles, and it must, because the
    requests exist for the boot that follows. So an invocation parks, gets as
    far as registering, and learns there that another launch took the name
    first. The requests then sit in a process that exits without a deck open,
    and without this call they leave with it, apply nothing, and report
    success.

    This probes nothing first. The caller just learned that another instance
    owns the name, which beats a second question. When that instance went away
    in between, the send fails and says so, like any other forward. The shapes
    come from this module's own parking a moment earlier, so nothing re-reads
    them, and the state is already the integer the bus method takes.

    Returns the failures to print. An empty list means that the instance took
    everything.
    """
    page_requests, parked_states = startup_queue.get().claim_parked_requests()
    if not page_requests and not parked_states:
        return []
    if transport is None:
        transport = _BusTransport()
    return _forward(transport, page_requests, [
        (serial, parked["page_name"], parked["coords"], parked["state"])
        for serial, parked in parked_states
    ])


# The real transport

class _BusTransport:
    """The running instance, over the session bus.

    The import of the toolkit sits here rather than at module scope, so this
    module's body stays standard-library-only. The deployment floor executes
    that body with every import stubbed out, and a CLI invocation that carries
    no request never builds one of these.
    """

    def __init__(self) -> None:
        from gi.repository import Gio, GLib

        self._gio = Gio
        self._glib = GLib
        self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def is_running(self) -> bool:
        """Does anything own the app's name on the bus right now?

        This asks the bus daemon and passes NO_AUTO_START. A call addressed to
        the well-known name activates it over D-Bus, which turns a probe into
        a launch. A probe that cannot complete reads as "nobody home", so a
        bus error parks the requests and boots rather than lose them.
        """
        try:
            reply = self._connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                self._glib.Variant("(s)", (appinfo.APP_ID,)),
                self._glib.VariantType("(b)"),
                self._gio.DBusCallFlags.NO_AUTO_START,
                DBUS_CALL_TIMEOUT_MS,
                None,
            )
        except self._glib.Error:
            return False
        return bool(reply.unpack()[0])

    def change_page(self, serial: str, page: str) -> str:
        return self._call("ChangePage", self._glib.Variant("(ss)", (serial, page)))

    def change_state(self, serial: str, page: str, coords: str, state: int) -> str:
        return self._call(
            "ChangeState",
            self._glib.Variant("(sssi)", (serial, page, coords, state)),
        )

    def _call(self, method: str, params) -> str:
        try:
            reply = self._connection.call_sync(
                appinfo.APP_ID,
                appinfo.DBUS_OBJECT_PATH,
                TOP_IFACE,
                method,
                params,
                self._glib.VariantType("(s)"),
                self._gio.DBusCallFlags.NO_AUTO_START,
                DBUS_CALL_TIMEOUT_MS,
                None,
            )
        except self._glib.Error as e:
            if self._gio.DBusError.get_remote_error(e) in _METHOD_MISSING:
                raise OlderInstance from e
            # Every other error, such as a timeout against a wedged
            # instance, a dropped connection or a refused call, becomes text
            # the caller prints. A GLib.Error left in place escapes the CLI
            # and logs as a crash, which puts a traceback on screen and still
            # exits zero, which reads as success.
            raise TransportError(str(e)) from e
        return str(reply.unpack()[0])
