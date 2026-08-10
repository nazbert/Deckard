"""
Boot-phase deferral: work asked for before whatever must carry it out exists.

This module owns two such handshakes for the whole process. Leg A defers
CALLS until the ``App`` is running. Legs B/C park CLI REQUESTS until the deck
they name shows up. Different mechanisms, one stance: name the protocol here,
leave the data where the rest of the process already looks for it.

WHERE THE STATE LIVES

Not here. This is the protocol, not the data: tasks live on
``gl.app_loading_finished_tasks``, parked requests on
``gl.api_page_requests`` / ``gl.api_state_requests``, and every method reads
the slot it works on ON EVERY CALL, never caching it in an attribute. Plugin
code appends to the task list directly (it is reachable, therefore it is API),
and tests both swap that slot for an instrumented list and write parked
requests straight into the dicts; all of it keeps working only as long as this
module looks the slots up instead of holding the objects it found once.

NO LOCKS, DELIBERATELY

GIL-atomic list and dict operations plus the operation orders described below
ARE the synchronization. A lock cannot make ownership more exclusive than it
already is, and it would put a boot-phase acquisition in front of every
notification from every thread -- including the drain running tasks that
enqueue further tasks -- and in front of every page load.

============================================================================
LEG A -- DELIVERIES THAT NEED THE RUNNING APP
============================================================================

Its callers are the notification facade (any thread) and the plugin manager's
disabled-plugins report (the pre-GTK main thread, mid
``create_global_objects``); the drain that pairs with their appends is
``App.on_activate``. The protocol below is subtle enough that one copy of it
is the right number.

``when_app_ready(task)`` answers exactly one question: *may I deliver this
myself, right now?* True means yes -- the caller owns the delivery. False means
the task is queued and the drain owns it. Exactly one side ever owns a task.

READINESS IS LITERALLY ``gl.app is not None``

Not an internal flag. ``gl.app`` is published twice: in ``Main.__init__``
before ``app.run()``, and again in ``App.on_activate`` immediately before the
drain. Calls landing in that window must skip the queue entirely and marshal
themselves onto the main loop that is about to start. An internal "ready" flag
flipped at on_activate would queue them instead -- a different set of
deliveries waiting on the window than the app has today.

THE APPEND-VS-DRAIN RACE

The append races the drain: on_activate can publish ``gl.app`` and finish
popping the queue between the None-check and the append, stranding the task
forever. So after appending, re-check and try to take the task back --
list append/pop/remove are atomic under the GIL, so exactly one side ends up
owning it: either the drain popped it (remove raises ValueError; the drain
delivers) or the task comes back here and the caller delivers. The operation
ORDER -- append, then re-check, then remove -- is what makes ownership
exclusive; reordering it reintroduces the strand.

THREAD CONTRACT (LEG A)

``when_app_ready`` is callable from any thread at any point in startup.
``drain_app_ready`` is main-thread-only: ``App.on_activate`` calls it once,
after ``gl.app`` is published.

============================================================================
LEGS B/C -- CLI REQUESTS PARKED FOR A DECK THAT IS NOT THERE YET
============================================================================

``--change-page SERIAL PAGE`` and ``--change-state SERIAL PAGE X,Y N`` name a
deck by serial. With no instance already running, the invocation becomes the
instance: it parks its own requests here, boots, and the controller for that
serial claims them the first time it loads its default page. With an instance
already running the request travels over DBus to that process instead and
never reaches this module -- except under ``--close-running``, which parks
here like the no-instance case and then boots over the instance it shut down.

Parking is keyed by serial and last-write-wins -- two ``--change-page`` pairs
for one serial leave only the second. A request naming a serial that never
connects stays parked for the life of the process; nothing sweeps it. Both
are the shipped behavior, written down here so neither reads as an oversight.

The two legs differ in exactly one way, and it is the whole reason there are
three methods rather than two:

* A page request is CLAIMED -- read and removed in one step, because it is
  one-shot (see ``claim_page_request``).
* A state request is PEEKED, processed, and only then RESOLVED. Processing it
  means loading a page and setting an input state, work that can raise;
  leaving the request parked until that work is done is what makes the next
  load retry it rather than lose it. Collapsing the pair into one claim would
  silently turn a retry into a drop.

THREAD CONTRACT (LEGS B/C)

``park_page_request`` / ``park_state_request`` run on the main thread during
the pre-boot, single-threaded CLI phase, before any deck exists.

The claim, peek and resolve calls run on whichever thread brings a controller
up or reloads its default page, which is very nearly every thread this process
has: the main thread (the boot path, before the GTK loop starts), the boot
rescan thread, the USB hotplug monitor, a deck's own HID reader thread (a key
press dismisses a showing screensaver, and the hide reloads the default page),
plugin and action threads (the same dismissal, reached through
``Page.update_input``), and the GTK main thread (the no-pages fallback). That
spread is why claiming is one dict operation rather than a check and a pop.

Imports ``globals`` and nothing else first-party, so any layer -- including the
render engine's import closure -- can import it without risking a cycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import globals as gl

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class StartupQueue:
    """The boot-phase deferral protocols. Stateless: every method reads the
    ``gl`` slot it works on, so the queue never diverges from the list and
    dicts the rest of the process (and plugin code) sees."""

    # ---------------------------------------------------------------- #
    # Leg A -- deliveries waiting on the running App                    #
    # ---------------------------------------------------------------- #

    def when_app_ready(self, task: Callable[[], Any]) -> bool:
        """True if the caller owns delivery and must run its work now; False
        if `task` is queued and the drain will run it.

        Safe from any thread. `task` must be zero-argument; its return value
        is ignored. A True answer does NOT mean `task` ran -- the caller
        delivers however it likes (the notification facade re-enters itself
        through GLib.idle_add rather than calling the queued lambda).

        Both the readiness predicate and the reclaim race are described in the
        module docstring; the operation order below is load-bearing.
        """
        if gl.app is None:
            gl.app_loading_finished_tasks.append(task)
            if gl.app is None:
                return False
            try:
                gl.app_loading_finished_tasks.remove(task)
            except ValueError:
                # The drain took it first and will deliver it.
                return False
        return True

    def drain_app_ready(self) -> None:
        """Run every queued task on the calling thread. Main thread only,
        called from ``App.on_activate`` once ``gl.app`` is published.

        Drain by atomic pop, never iterate-then-clear: background threads race
        their appends against this drain, and a task appended mid-iteration
        would be cleared unrun. pop(0) makes every task owned by exactly one
        side -- this loop, or the appender's post-append reclaim -- and a task
        that appends further tasks while running gets those drained too.

        Return values are ignored. A NON-CALLABLE entry is skipped rather than
        raised on: the list is reachable from plugin code, and an append of
        `f()` where `f` was meant must not take the drain down with it. A task
        that RAISES is a different case and is deliberately not caught -- the
        exception propagates out of the drain, and the tasks behind it stay
        queued, exactly as when this loop lived in on_activate. Swallowing it
        here would hide failures on the activation path.
        """
        while gl.app_loading_finished_tasks:
            task = gl.app_loading_finished_tasks.pop(0)
            if callable(task):
                task()

    # ---------------------------------------------------------------- #
    # Leg B -- `--change-page` requests waiting for their deck          #
    # ---------------------------------------------------------------- #

    def park_page_request(self, serial_number: str, page_name: str) -> None:
        """Park a page change for a deck that has not appeared yet.

        Last-write-wins per serial. `page_name` is stored as the user typed
        it and resolved to a path by the claimer -- there is no page store to
        resolve it against at parking time.
        """
        gl.api_page_requests[serial_number] = page_name

    def claim_page_request(self, serial_number: str) -> str | None:
        """This serial's parked page name, removed as it is handed over, or
        None if nothing is parked for it.

        Pop, don't just read (design doc bug 13): a `--change-page` request is
        one-shot -- left in place, it silently re-applied itself on every
        future load_default_page() call for this serial (every unplug/replug,
        every "no page found" fallback).

        Lookup and removal are one dict operation, so two threads racing the
        same serial up cannot both come away holding the request.
        """
        return gl.api_page_requests.pop(serial_number, None)

    # ---------------------------------------------------------------- #
    # Leg C -- `--change-state` requests waiting for their deck         #
    # ---------------------------------------------------------------- #

    def park_state_request(self, serial_number: str, request: dict) -> None:
        """Park a state change for a deck that has not appeared yet.

        Last-write-wins per serial. The request is stored as given: argument
        validation and the state-number conversion belong to the CLI parser,
        the only layer that can still report a bad argument to the person who
        typed it.
        """
        gl.api_state_requests[serial_number] = request

    def peek_state_request(self, serial_number: str) -> dict | None:
        """This serial's parked state request, LEFT PARKED, or None.

        Peeking rather than claiming is what lets the request survive an
        exception thrown while it is being applied -- loading the page it
        names, resolving coordinates, setting the state. The next
        load_default_page() for this serial sees it again and retries.

        Every peek that goes on to process the request must be paired with
        resolve_state_request().
        """
        return gl.api_state_requests.get(serial_number)

    def resolve_state_request(self, serial_number: str) -> None:
        """Drop this serial's parked state request: it has been processed.

        Call only once the processing peek_state_request handed out has run to
        its end -- resolving earlier turns the retry above into a drop.
        Idempotent, and a serial with nothing parked is not an error.
        """
        gl.api_state_requests.pop(serial_number, None)


# The process-wide queue. A module singleton rather than a `gl` slot: the point
# of naming this protocol is to shrink what lives on the shared namespace, not
# to add to it.
_queue = StartupQueue()


def get() -> StartupQueue:
    """The process-wide startup queue. Never None."""
    return _queue
