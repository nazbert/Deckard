"""One place decides whether a page or state switch is valid.

"Switch deck S to page P" and "set input (x,y) on page P of deck S to state N"
arrive from several transports. Those are the D-Bus methods that an external
tool or a second CLI invocation calls on the running instance, and the argv
requests that a booting process parks for a deck which has not enumerated yet.
A copy of the rules per transport drifts. The same-page no-op reached two of
the three, an unknown page produced a warning, a list of suggestions or
silence by where the request landed, and the coordinate bounds came from the
device here and from invented constants in the CLI's own pre-check. This
module is the one rule set they all ask.

What is a rule and what is a transport

The decision lives here, and the rendering stays at the surface. Every method
answers with a ControlResult that carries a machine-readable code and the
human sentence for it, and the caller decides what a sentence becomes, which
is a log line, a D-Bus return string, or terminal output. Nothing here logs,
and nothing here touches the toolkit. For the same reason a message names the
deck only where the failure is about the deck, which is an unknown serial or a
state changed, and each surface adds its own context.

Two layers, because one caller has no serial to resolve

load_default_page runs inside DeckController.__init__, before anything appends
the controller to gl.deck_manager.deck_controller. It holds the controller and
cannot look itself up by serial. So the rules take a controller, in
change_page_on and change_state_on, and the serial-resolving wrappers,
change_page and change_state, add a thin lookup for the transports that speak
serials.

No blanket except

An invalid request is a result. An unexpected exception, such as a load_page
that raises or a device gone mid-call, propagates to the caller untouched, and
that decides a behaviour rather than tidy the code. The boot path peeks a
parked state request, applies it through here, and resolves it once the apply
returns, so an exception on the way through leaves the request parked for the
next load to retry (see src/backend/startup_queue.py). A catch here turns that
retry into a silent drop.

Thread contract

The caller's thread runs this, whichever it is, which covers the boot thread,
a USB hotplug thread, the GTK main thread and the D-Bus dispatch. Every
surface this replaces did the same. load_page serializes itself under the
controller's page lock, the media thread stays the one device writer, and
nothing here calls the UI. This snapshots the manager's controller list before
it walks it, because a hotplug thread appends to it and removes from it.

Imports

globals and the input identifiers at runtime, everything else under
TYPE_CHECKING, and no gi at all. The deck controller imports this module, so
it sits inside the render engine's import closure and takes both standing
guards of that closure, which are the widget-free rule
(scenario_headless_engine_no_gtk) and the named gl surface
(scenario_engine_gl_surface, and this module reads deck_manager and
page_manager and nothing else). The deployment floor runs Python 3.13, which
evaluates an annotation at definition time, and the future import prevents
that. scenario_floor_import executes this module body on that interpreter to
keep it true.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import globals as gl
from src.backend.DeckManagement.InputIdentifier import Input

if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend


@dataclass(frozen=True)
class ControlResult:
    """The outcome of one control request.

    code is the vocabulary a caller branches on. message says the same thing
    to a person, and every current surface renders that part alone.

      ""                      the request applied
      "already-active"        the deck already shows that page, so nothing
                              happens. This is an ok result, because a no-op
                              fulfils the request.
      "no-such-deck"          no controller reports that serial
      "no-such-page"          no page file matches that name or path
      "page-build-failed"     the page exists and could not be built
      "no-page-manager"       asked before the page store exists, on boot only
      "bad-coords"            the coordinates do not read as x,y
      "coords-out-of-bounds"  outside this device's key layout
      "no-such-input"         in bounds, and the deck has no input there
      "bad-state"             the state number is no integer
      "state-out-of-range"    that input has no such state
    """

    ok: bool
    code: str = ""
    message: str = ""


def _controllers() -> list[DeckController]:
    """Snapshot of the live controllers. A snapshot because USB hotplug and
    teardown mutate this list from their own threads while a request walks
    it."""
    deck_manager = gl.deck_manager
    if deck_manager is None:
        return []
    return list(deck_manager.deck_controller)


def _no_such_deck(serial_number: str) -> ControlResult:
    """The unknown-serial result. It lists what is connected, because the
    answer to "did I mistype the serial?" belongs in the failure itself.

    With nothing connected there is no list to give, and an empty answer reads
    two ways. The app answers a request from the moment it takes the bus name,
    which comes before it enumerates a deck. So the empty case names that
    possibility, rather than let a person conclude that a deck is unplugged
    while it is only not open yet.
    """
    available = [controller.serial_number() for controller in _controllers()]
    if available:
        message = (f"StreamDeck with serial '{serial_number}' not found. "
                   f"Available devices: {', '.join(available)}")
    else:
        message = ("No StreamDeck devices connected yet "
                   "(the app may still be starting)")
    return ControlResult(False, "no-such-deck", message)


def _no_such_page(page_ref: str, page_manager: PageManagerBackend) -> ControlResult:
    """The unknown-page result, listing the page names that do exist."""
    available = [os.path.splitext(os.path.basename(p))[0]
                 for p in page_manager.get_pages()]
    return ControlResult(False, "no-such-page",
                         f"Page '{page_ref}' not found. "
                         f"Available pages: {', '.join(available)}")


class ControlPlane:
    """The rules. This holds no state, and every call reads the gl slots it
    needs, so a controller list or a page store rebound underneath it, which
    the test harness does, still applies."""

    # The cores, which take a controller.

    def change_page_on(self, controller: DeckController, page_ref: str) -> ControlResult:
        """Show page_ref, which is a page name or a page path, on controller.

        The load is skipped while that page is already the active one. That
        no-op is why this check has one home. A repeated switch request
        otherwise reloads the deck on one transport and does nothing on the
        others, and on a real deck a reload flickers and re-renders every key.
        """
        page_manager = gl.page_manager
        if page_manager is None:
            return ControlResult(False, "no-page-manager",
                                 f"Cannot change to page '{page_ref}': no page manager")

        page_path = page_manager.find_matching_page_path(page_ref)
        if page_path is None:
            return _no_such_page(page_ref, page_manager)

        # Snapshot the page and check it for None. active_page can be None,
        # after a racing close or clear, or a load deferred by a showing
        # screensaver, and another thread can swap it between the read and the
        # compare. No current page means the requested one differs, so the
        # load proceeds.
        active_page = controller.active_page
        if active_page is not None and os.path.abspath(page_path) == os.path.abspath(active_page.json_path):
            return ControlResult(True, "already-active", f"Page '{page_ref}' is already active")

        page = page_manager.get_page(page_path, controller)
        if page is None:
            # The file matched a name and did not build into a page. A None
            # handed to load_page clears the deck, so a page that failed to
            # build blanks the device and reports success. A deck that keeps
            # what it shows is the better failure, and this tells the caller
            # the reason.
            return ControlResult(False, "page-build-failed",
                                 f"Page '{page_ref}' could not be loaded")

        controller.load_page(page)
        return ControlResult(True)

    def change_state_on(self, controller: DeckController, page_ref: str,
                        coords: str, state: int | str) -> ControlResult:
        """Set the input at coords on page_ref to state, and load the page
        first when it is not the active one.

        The page comes first, because the requested page defines the input
        this addresses, and that input's state count bounds the state number.
        The bounds come from this device's own key layout and this input's own
        state list, and never from a constant, because no constant holds for
        every deck.
        """
        page_result = self.change_page_on(controller, page_ref)
        if not page_result.ok:
            return page_result

        try:
            x, y = map(int, coords.split(","))
        except (ValueError, AttributeError):
            return ControlResult(False, "bad-coords",
                                 f"Invalid coordinate format '{coords}'. "
                                 f"Expected format: 'x,y' (e.g., '0,0')")

        rows, cols = controller.deck.key_layout()
        if x < 0 or x >= cols or y < 0 or y >= rows:
            return ControlResult(False, "coords-out-of-bounds",
                                 f"Coordinates ({x},{y}) are out of bounds for this device. "
                                 f"Valid range: x=0-{cols - 1}, y=0-{rows - 1}")

        c_input = controller.get_input(Input.Key(f"{x}x{y}"))
        if c_input is None:
            return ControlResult(False, "no-such-input",
                                 f"Could not find input at coordinates ({x},{y})")

        # The state number is an int on the D-Bus method's signature and on
        # the CLI's parked requests, which convert at their own edge. The boot
        # path's own tests write into the parked dict directly, and so does
        # anything else that holds gl.api_state_requests, so a string still
        # reaches here. A conversion costs less than a rule that every writer
        # of that dict must get the type right.
        try:
            state_number = int(state)
        except (TypeError, ValueError):
            return ControlResult(False, "bad-state",
                                 f"Invalid state number '{state}'. Must be an integer")

        state_count = len(c_input.states)
        if state_number < 0 or state_number >= state_count:
            if state_count == 1:
                has = "only has 1 state (state 0)"
            else:
                has = f"has {state_count} states (0-{state_count - 1})"
            return ControlResult(False, "state-out-of-range",
                                 f"Position ({x},{y}) {has}. "
                                 f"Requested state {state_number} does not exist")

        c_input.set_state(state_number)
        return ControlResult(True, "",
                             f"Successfully changed state of ({x},{y}) to state "
                             f"{state_number} on device {controller.serial_number()}")

    # The wrappers, which resolve a serial.

    def change_page(self, serial_number: str, page_ref: str) -> ControlResult:
        """change_page_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.change_page_on(controller, page_ref)

    def change_state(self, serial_number: str, page_ref: str,
                     coords: str, state: int | str) -> ControlResult:
        """change_state_on for the deck that reports serial_number."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.change_state_on(controller, page_ref, coords, state)

    def _find(self, serial_number: str) -> DeckController | None:
        """The first controller that reports serial_number, or None. A serial
        is unique per device. A duplicate means a deck that reports another
        deck's identity, and the first controller is as good an answer as
        exists."""
        for controller in _controllers():
            if controller.serial_number() == serial_number:
                return controller
        return None


# The process-wide control plane. A module singleton rather than a gl slot,
# for the reason the startup queue is one. A named protocol should shrink the
# shared namespace.
_control_plane = ControlPlane()


def get() -> ControlPlane:
    """The process-wide control plane. Never None."""
    return _control_plane
