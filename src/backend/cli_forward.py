"""
The CLI half of the control plane: what an invocation carrying
``--change-page`` / ``--change-state`` does with the requests it was given.

An invocation that names a deck has two entirely different jobs depending on
whether Deckard is already running. With nothing running (or with
``--close-running``, which is about to stop whatever is), the invocation
BECOMES the instance: it parks its requests for the deck that has not
enumerated yet and goes on to boot (see src/backend/startup_queue.py legs
B/C). With an instance running, the requests are FORWARDED to it over the bus
and this process is done.

WHY THIS IS NOT IN main.py

main.py cannot be imported by anything: its module body re-execs the process
and runs the rebrand migration against the real user directories. Everything
that lived there was therefore untestable, and it showed -- forwarding
returned after the first request it sent, so a command carrying several page
or state changes silently applied one of them and dropped the rest. This
module is the extraction that makes that provable; main.py keeps the two
things that cannot leave it, reading argv and exiting the process.

SYNTAX HERE, MEANING THERE

The only questions asked here are ones answerable without a device: do the
coordinates read as two non-negative integers, does the state read as a
non-negative integer. Whether (9,9) is on the deck, whether state 19 exists on
that input, whether the page or the serial is real -- those are the running
instance's to answer, and it answers them from the device's own truth. The
caps this file used to apply (coordinates <= 10, state <= 20) were invented
numbers matching no hardware, and they rejected valid requests for large decks
before anything with a device ever saw them.

FAILURES ARE THE CALLER'S TO SEE

Forwarding used to be fire-and-forget over a Gio action, so a mistyped page
name produced a line in the RUNNING instance's log and total silence in the
terminal that typed it. The bus methods answer with a sentence instead, and
those sentences come back in the verdict for main.py to print. A request that
fails does not stop the ones behind it: each is independent, and a person who
asked for four changes is better served by three applied and one explained
than by an arbitrary prefix.

VERSION SKEW, BOTH DIRECTIONS

Forwarding used to travel over Gio actions, which are gone. A CLI from an
older install therefore forwards `change_page`/`change_state` actions that a
current instance does not register, and org.gtk.Actions answers an unknown
action by doing nothing at all -- so that direction fails silently and cannot
be made to do otherwise from this side. It needs both a current and an older
install on one machine to happen, it goes away as soon as the old one does,
and the parked/boot path is unaffected; that is why it is documented rather
than defended against. This direction -- a current CLI meeting an instance
without the methods -- is the one that can speak, and it says SKEW_MESSAGE.

THE TRANSPORT IS INJECTABLE

``forward_cli_requests`` takes the thing that talks to the running instance,
so the forwarding rules can be driven without a bus, a daemon or a second
process. The default is the real one; it imports the toolkit when it is
constructed rather than at module import, which is what keeps this module's
body importable on the deployment floor with nothing but the standard library
present (scenario_floor_import executes it there).
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


# Every CLI-side D-Bus call carries this instead of the 25s bus default:
# without it a wedged instance that accepts but never replies blocks startup
# for that long. main.py's own probes import it from here so there is one
# number rather than two that can drift apart.
DBUS_CALL_TIMEOUT_MS = 5000

# The interface the top-level object carries -- the app id itself.
TOP_IFACE = appinfo.APP_ID

# What comes back when the method is not there to call. GDBus uses these two
# spellings for a missing method depending on its version -- and, importantly,
# for a missing OBJECT as well: an instance that owns the bus name but has not
# published its objects yet answers exactly like an instance that is too old
# to have the methods. SKEW_MESSAGE says both, because this cannot tell them
# apart. (Telling them apart means asking the instance what it implements,
# which is worth doing when the boot window it exists for is closed.)
_METHOD_MISSING = (
    "org.freedesktop.DBus.Error.UnknownMethod",
    "org.freedesktop.DBus.Error.UnknownInterface",
)

SKEW_MESSAGE = (
    "The running Deckard has not finished starting, or is an older build "
    "without the page and state methods this command needs. Try again in a "
    "moment, and restart it if this persists."
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
    """The running instance did not answer to the control methods -- see
    SKEW_MESSAGE for the two things that produces."""


class TransportError(Exception):
    """The conversation with the running instance failed: it never replied,
    the connection went away, it refused the call. Carries the bus's own text,
    and exists so nothing outside this module has to know what a GLib.Error
    is."""


@dataclass(frozen=True)
class Verdict:
    """What the invocation should do next.

    ``handled`` means a running instance took the requests and this process
    has nothing left to do; False means the requests are parked (or there were
    none) and this process carries on booting. ``failures`` are the sentences
    to put in front of the person who typed the command -- a non-empty list is
    a failed invocation whatever ``handled`` says.
    """

    handled: bool = False
    failures: list[str] = field(default_factory=list)


# ===================================================================== #
# Syntax                                                                #
# ===================================================================== #

def _parse_state_requests(raw: list) -> tuple[list[tuple[str, str, str, int]],
                                              list[str]]:
    """Read the ``--change-state`` groups into (serial, page, coords, state).

    All-or-nothing, as it has always been: a command with one malformed group
    applies none of its requests, because a partly-applied command is the
    worst of the three possible outcomes. Only shapes are judged here -- see
    the module docstring on why the bounds are not.
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


# ===================================================================== #
# The two things an invocation can do with its requests                 #
# ===================================================================== #

def _park(page_requests: list, state_requests: list[tuple[str, str, str, int]]) -> None:
    """Hand every request to the startup queue, for the decks this process is
    about to enumerate. Keyed by serial and last-write-wins, which is the
    parking contract, not an accident of this loop."""
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

    EVERY request: the loops below used to return as soon as one had been
    sent, so `--change-page A X --change-page B Y` moved deck A and left deck
    B alone, and any `--change-state` on a command that also carried a
    `--change-page` never left the process at all.

    Order is every page request as argv gave them, then every state request as
    argv gave them. argparse collects the two flags into two separate lists,
    so the interleaving between them is not recoverable here -- and this is the
    order the CLI has always applied, so nothing about it changes today.
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
        # Every request behind this one would fail identically, and the
        # answer is the same for all of them.
        failures.append(SKEW_MESSAGE)
    except TransportError as e:
        # The conversation itself broke. It will not heal inside one command,
        # and retrying each remaining request would spend the call timeout
        # again per request against an instance that is not answering -- so
        # the failure is reported once and the rest are abandoned.
        failures.append(str(e))
    return failures


def forward_cli_requests(args: Namespace,
                         transport: Transport | None = None) -> Verdict:
    """Apply the invocation's ``--change-page`` / ``--change-state`` requests.

    Parked for this process to pick up as it boots, or forwarded to the
    instance already running -- see the module docstring. Never exits and
    never prints: the verdict says what happened and main.py owns both.
    """
    page_requests = args.change_page or []
    raw_state_requests = args.change_state or []
    if not page_requests and not raw_state_requests:
        return Verdict()

    state_requests, failures = _parse_state_requests(raw_state_requests)
    if failures:
        # Nothing has been parked or sent yet, and nothing will be.
        return Verdict(handled=False, failures=failures + [USAGE])

    if transport is None:
        transport = _BusTransport()

    if not transport.is_running() or args.close_running:
        _park(page_requests, state_requests)
        return Verdict(handled=False)

    return Verdict(handled=True,
                   failures=_forward(transport, page_requests, state_requests))


# ===================================================================== #
# The real transport                                                    #
# ===================================================================== #

class _BusTransport:
    """The running instance, over the session bus.

    The toolkit is imported here rather than at module scope so this module's
    body stays standard-library-only: it is executed on the deployment floor
    with every import stubbed out, and a CLI invocation that carries no
    requests never builds one of these at all.
    """

    def __init__(self) -> None:
        from gi.repository import Gio, GLib

        self._gio = Gio
        self._glib = GLib
        self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def is_running(self) -> bool:
        """Is the app's name owned on the bus right now?

        Asked of the bus daemon, and with NO_AUTO_START: addressing the
        well-known name directly would D-Bus-activate it, which turns a probe
        into a launch. A probe that cannot complete reads as "nobody home",
        which is what makes a bus hiccup park the requests and boot rather
        than lose them.
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
            # Anything else -- a timeout against a wedged instance, a dropped
            # connection, a refused call -- becomes something the caller can
            # print. Left as a GLib.Error it escaped the CLI entirely and was
            # logged as a crash, which put a traceback on screen and still
            # exited zero: the silent success this command set out to fix.
            raise TransportError(str(e)) from e
        return str(reply.unpack()[0])
