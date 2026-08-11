"""
One place decides whether a page or state switch is valid.

"Switch deck S to page P" and "set input (x,y) on page P of deck S to state N"
arrive from several transports -- the DBus API, the Gio actions a second CLI
invocation forwards to the running instance, and the argv requests a booting
process parks for a deck that has not enumerated yet. Each of those used to
carry its own copy of the rules, and the copies had drifted: the same-page
no-op was in two of the three, an unknown page was warned about, listed with
suggestions, or silently swallowed depending on where the request landed, and
coordinate bounds were the device's truth here but invented constants in the
CLI's own pre-check. This module is the single rule set they all ask.

WHAT IS A RULE AND WHAT IS A TRANSPORT

The DECISION lives here; the RENDERING stays at the surface. Every method
answers with a ``ControlResult`` carrying a machine-readable ``code`` and the
human sentence that goes with it, and the caller decides what a sentence is:
a log line, a DBus return string, something printed to a terminal. Nothing in
here logs, and nothing in here touches the toolkit -- which is also why the
messages name the deck only where the failure is genuinely about the deck (an
unknown serial, or a state successfully changed), leaving each surface free to
add its own context.

TWO LAYERS, BECAUSE ONE CALLER HAS NO SERIAL TO RESOLVE

``load_default_page`` runs inside ``DeckController.__init__``, before the
controller is appended to ``gl.deck_manager.deck_controller``: it holds the
controller but could not look itself up by serial. So the rules are written
against a controller (``change_page_on`` / ``change_state_on``) and the
serial-resolving wrappers (``change_page`` / ``change_state``) are a thin
lookup on top for the transports that speak serials.

NO BLANKET EXCEPT, DELIBERATELY

An invalid REQUEST is a result. An unexpected EXCEPTION -- a ``load_page``
that raises, a device gone mid-call -- propagates to the caller untouched, and
that is load-bearing rather than tidy: the boot path peeks a parked state
request, applies it through here, and only resolves it once the apply
returned, so an exception on the way through is exactly what leaves the
request parked for the next load to retry (see src/backend/startup_queue.py).
Catching everything here would turn that retry into a silent drop.

THREAD CONTRACT

The caller's thread, whichever it is -- the boot thread, a USB hotplug thread,
the GTK main thread, the DBus dispatch. Same as every surface this replaces:
``load_page`` serializes itself under the controller's page lock, the media
thread stays the only device writer, and no UI call is made from here. The
manager's controller list is snapshotted before it is walked, because hotplug
threads append to and remove from it.

IMPORTS

``globals`` and the input identifiers at runtime, everything else under
``TYPE_CHECKING``, no ``gi`` at all. The deck controller imports this module,
so it is inside the render engine's import closure and inherits both of that
closure's standing guards: the widget-free rule (scenario_headless_engine_no_gtk)
and the named `gl` surface (scenario_engine_gl_surface -- this reads
``deck_manager`` and ``page_manager``, and nothing else). The deployment floor
(Python 3.13) evaluates annotations at definition time, which the future-import
is what prevents; scenario_floor_import executes this module body on that
interpreter to keep it true.
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

    ``code`` is the vocabulary a caller can branch on; ``message`` is the same
    thing said to a person, and is the only part any current surface renders:

      ``""``                    the request was applied
      ``"already-active"``      ok: the deck already shows that page, nothing
                                to do (still an ok result -- a no-op is a
                                fulfilled request, not a failure)
      ``"no-such-deck"``        no controller reports that serial
      ``"no-such-page"``        no page file matches that name or path
      ``"no-page-manager"``     asked before the page store exists (boot only)
      ``"bad-coords"``          coordinates are not ``x,y``
      ``"coords-out-of-bounds"``  outside THIS device's key layout
      ``"no-such-input"``       in bounds, but the deck has no input there
      ``"bad-state"``           the state number is not an integer
      ``"state-out-of-range"``  that input has no such state
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
    """The unknown-serial result, listing what IS connected -- the answer to
    "did I typo the serial?" belongs in the failure itself."""
    available = [controller.serial_number() for controller in _controllers()]
    if available:
        message = (f"StreamDeck with serial '{serial_number}' not found. "
                   f"Available devices: {', '.join(available)}")
    else:
        message = "No StreamDeck devices connected"
    return ControlResult(False, "no-such-deck", message)


def _no_such_page(page_ref: str, page_manager: PageManagerBackend) -> ControlResult:
    """The unknown-page result, listing the page names that do exist."""
    available = [os.path.splitext(os.path.basename(p))[0]
                 for p in page_manager.get_pages()]
    return ControlResult(False, "no-such-page",
                         f"Page '{page_ref}' not found. "
                         f"Available pages: {', '.join(available)}")


class ControlPlane:
    """The rules. Stateless: every call reads the `gl` slots it needs, so a
    controller list or page store rebound underneath it (which is what the
    test harness does) is honoured."""

    # ---------------------------------------------------------------- #
    # Controller-taking cores                                          #
    # ---------------------------------------------------------------- #

    def change_page_on(self, controller: DeckController, page_ref: str) -> ControlResult:
        """Show `page_ref` -- a page name or a page path -- on `controller`.

        Loading is skipped when the page is already the active one. That
        no-op is the whole reason this check has one home: a repeated switch
        request used to reload the deck on one transport and be ignored on
        the others, which on a real deck is a visible flicker and a full
        re-render of every key.
        """
        page_manager = gl.page_manager
        if page_manager is None:
            return ControlResult(False, "no-page-manager",
                                 f"Cannot change to page '{page_ref}': no page manager")

        page_path = page_manager.find_matching_page_path(page_ref)
        if page_path is None:
            return _no_such_page(page_ref, page_manager)

        # Snapshot + None-guard: active_page can be None (a racing
        # close()/clear, or a load deferred by a showing screensaver) and can
        # be swapped by another thread between the read and the compare. No
        # current page means the requested one is trivially different, so the
        # load proceeds.
        active_page = controller.active_page
        if active_page is not None and os.path.abspath(page_path) == os.path.abspath(active_page.json_path):
            return ControlResult(True, "already-active", f"Page '{page_ref}' is already active")

        controller.load_page(page_manager.get_page(page_path, controller))
        return ControlResult(True)

    def change_state_on(self, controller: DeckController, page_ref: str,
                        coords: str, state: int | str) -> ControlResult:
        """Set the input at `coords` on `page_ref` to `state`, loading the
        page first if it is not already the active one.

        The page comes first because the input being addressed is the one the
        REQUESTED page defines, and its state count is what the state number
        is checked against. Bounds are this device's own key layout and this
        input's own state list -- never a constant, because no constant is
        true of every deck.
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

        # The state number arrives as an int from the parked-request dict and
        # as a string from the stringly-typed action transports; converting
        # here is what keeps that difference the transports' business.
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

    # ---------------------------------------------------------------- #
    # Serial-resolving wrappers                                        #
    # ---------------------------------------------------------------- #

    def change_page(self, serial_number: str, page_ref: str) -> ControlResult:
        """``change_page_on`` for the deck reporting `serial_number`."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.change_page_on(controller, page_ref)

    def change_state(self, serial_number: str, page_ref: str,
                     coords: str, state: int | str) -> ControlResult:
        """``change_state_on`` for the deck reporting `serial_number`."""
        controller = self._find(serial_number)
        if controller is None:
            return _no_such_deck(serial_number)
        return self.change_state_on(controller, page_ref, coords, state)

    def _find(self, serial_number: str) -> DeckController | None:
        """The first controller reporting `serial_number`, or None. Serials
        are unique per device; a duplicate would be a deck reporting another's
        identity, and picking the first is as good an answer as exists."""
        for controller in _controllers():
            if controller.serial_number() == serial_number:
                return controller
        return None


# The process-wide control plane. A module singleton rather than a `gl` slot,
# for the reason the startup queue is one: naming a protocol should shrink the
# shared namespace, not add to it.
_control_plane = ControlPlane()


def get() -> ControlPlane:
    """The process-wide control plane. Never None."""
    return _control_plane
