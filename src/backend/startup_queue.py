"""Boot-phase deferral for work asked of something that does not exist yet.

This module owns two such handshakes for the whole process. Leg A defers a
call until the App runs. Legs B and C park a CLI request until the deck it
names appears. The mechanisms differ, and the stance is one. Name the protocol
here, and leave the data where the rest of the process already looks for it.

Where the state lives

Not here. This module holds the protocol and no data. The tasks live on
gl.app_loading_finished_tasks, and the parked requests on gl.api_page_requests
and gl.api_state_requests. Every method reads the slot it works on per call,
and caches it in no attribute. Plugin code appends to the task list directly,
because that list is reachable and is therefore API, and a test both swaps
that slot for an instrumented list and writes parked requests into the dicts.
All of that works only while this module looks the slots up rather than hold
the objects it found once.

No locks

The GIL-atomic list and dict operations, plus the operation orders below, are
the synchronization. A lock cannot make the ownership more exclusive than it
already is, and it would put a boot-phase acquire in front of every
notification from every thread, including the drain that runs tasks which
enqueue further tasks, and in front of every page load.

Leg A. Deliveries that need the running app

Its callers are the notification facade, from any thread, and the plugin
manager's disabled-plugins report, from the pre-GTK main thread inside
create_global_objects. App.on_activate drains what they append. The protocol
below is subtle enough that one copy of it is the right number.

when_app_ready(task) answers one question. May the caller deliver this itself,
right now? True means yes, and the caller owns the delivery. False means the
task is queued and the drain owns it. Exactly one side owns a task.

Readiness is gl.app is not None

It is no internal flag. gl.app publishes twice, in Main.__init__ before
app.run(), and again in App.on_activate right before the drain. A call that
lands in that window must skip the queue and marshal itself onto the main loop
that starts next. An internal ready flag flipped at on_activate queues them
instead, which leaves a different set of deliveries waiting on the window.

The append against the drain

The append races the drain. on_activate can publish gl.app and finish popping
the queue between the None check and the append, which strands the task. So
after the append, re-check and take the task back. A list append, pop and
remove are atomic under the GIL, so exactly one side ends up owning it. Either
the drain popped it, and remove raises ValueError and the drain delivers, or
the task comes back here and the caller delivers. The order of the operations,
which is append, then re-check, then remove, makes the ownership exclusive,
and another order strands the task again.

Thread contract for leg A

when_app_ready accepts a call from any thread at any point in startup.
drain_app_ready runs on the main thread alone. App.on_activate calls it once,
after gl.app publishes.

Legs B and C. CLI requests parked for a deck that is not there yet

--change-page SERIAL PAGE and --change-state SERIAL PAGE X,Y N name a deck by
serial. With no instance running, the invocation becomes the instance. It
parks its own requests here, boots, and the controller for that serial claims
them the first time it loads its default page. With an instance running the
request travels over D-Bus to that process and never reaches this module. The
one exception is --close-running, which parks here like the no-instance case
and then boots over the instance it shut down.

Parking keys on the serial and the last write wins, so two --change-page pairs
for one serial leave the second. A request that names a serial which never
connects stays parked for the life of the process, and nothing sweeps it. Both
are the shipped behaviour, written down here so that neither reads as an
oversight.

The two legs differ in one way, which is why three methods exist rather than
two.

A page request is claimed, which reads and removes it in one step, because it
applies once (see claim_page_request).

A state request is peeked, processed, and only then resolved. The processing
loads a page and sets an input state, and that work can raise. The request
stays parked until the work ends, so the next load retries it rather than lose
it. One claim in place of the pair turns a retry into a drop.

One caller wants the whole parking rather than one serial's, and it applies
none of it. An invocation parks before the race for the application name
settles, so a launch can park its requests and then learn that it is the
second instance. claim_parked_requests takes those to the instance that won,
rather than let them leave with this process.

Thread contract for legs B and C

park_page_request and park_state_request run on the main thread during the
pre-boot, single-threaded CLI phase, before any deck exists.

The claim, peek and resolve calls run on whichever thread brings a controller
up or reloads its default page, which is nearly every thread this process has.
That is the main thread on the boot path before the GTK loop starts, the boot
rescan thread, the USB hotplug monitor, a deck's own HID reader thread,
because a key press dismisses a showing screensaver and the hide reloads the
default page, a plugin or action thread through the same dismissal reached by
Page.update_input, and the GTK main thread on the no-pages fallback. That
spread is why a claim is one dict operation rather than a check and a pop.

This module imports globals and nothing else first-party, so any layer,
including the render engine's import closure, imports it without a cycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import globals as gl

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class StartupQueue:
    """The boot-phase deferral protocols. It holds no state, and every
    method reads the gl slot it works on, so the queue never diverges from the
    list and the dicts that the rest of the process, and plugin code, see."""

    # Leg A. Deliveries that wait on the running App.

    def when_app_ready(self, task: Callable[[], Any]) -> bool:
        """True when the caller owns the delivery and must run its work now.
        False when the queue holds task and the drain runs it.

        Safe from any thread. task takes no argument, and nothing reads its
        return value. A True answer does not mean that task ran. The caller
        delivers as it likes, and the notification facade re-enters itself
        through GLib.idle_add rather than call the queued lambda.

        The module docstring describes the readiness test and the reclaim
        race. The order of the operations below decides the outcome.
        """
        if gl.app is None:
            gl.app_loading_finished_tasks.append(task)
            if gl.app is None:
                return False
            try:
                gl.app_loading_finished_tasks.remove(task)
            except ValueError:
                # The drain took it first and delivers it.
                return False
        return True

    def drain_app_ready(self) -> None:
        """Run every queued task on the calling thread. Main thread only.
        App.on_activate calls it once gl.app publishes.

        Drain by atomic pop, and never iterate and then clear. A background
        thread races its append against this drain, and a clear drops a task
        appended mid-iteration unrun. pop(0) gives every task to exactly one
        side, this loop or the appender's reclaim after its append, and a task
        that appends further tasks while it runs gets those drained too.

        Nothing reads a return value. This skips an entry that is not callable
        rather than raise on it, because plugin code reaches the list, and an
        append of f() where f was meant must not end the drain. A task that
        raises is another case, and nothing catches it. The exception
        propagates out of the drain and the tasks behind it stay queued. A
        catch here hides a failure on the activation path.
        """
        while gl.app_loading_finished_tasks:
            task = gl.app_loading_finished_tasks.pop(0)
            if callable(task):
                task()

    # Leg B. --change-page requests that wait for their deck.

    def park_page_request(self, serial_number: str, page_name: str) -> None:
        """Park a page change for a deck that has not appeared yet.

        The last write wins per serial. page_name stores as the user typed
        it, and the claimer resolves it to a path, because no page store
        exists to resolve it against at parking time.
        """
        gl.api_page_requests[serial_number] = page_name

    def claim_page_request(self, serial_number: str) -> str | None:
        """This serial's parked page name, removed as it hands it over, or
        None when nothing is parked for it.

        Pop it rather than read it. A --change-page request applies once. Left
        in place, it re-applies itself on every later load_default_page() call
        for this serial, which covers every unplug and replug and every "no
        page found" fallback.

        The lookup and the removal are one dict operation, so two threads that
        race the same serial up cannot both hold the request.
        """
        return gl.api_page_requests.pop(serial_number, None)

    # Leg C. --change-state requests that wait for their deck.

    def park_state_request(self, serial_number: str, request: dict) -> None:
        """Park a state change for a deck that has not appeared yet.

        The last write wins per serial. The request stores as given. The CLI
        parser owns the argument validation and the state-number conversion,
        because it is the one layer that can still report a bad argument to
        the person who typed it.
        """
        gl.api_state_requests[serial_number] = request

    def peek_state_request(self, serial_number: str) -> dict | None:
        """This serial's parked state request, left parked, or None.

        A peek rather than a claim lets the request survive an exception
        thrown while something applies it, which loads the page it names,
        resolves the coordinates and sets the state. The next
        load_default_page() for this serial sees it again and retries.

        Every peek that goes on to process the request must call
        resolve_state_request() afterwards.
        """
        return gl.api_state_requests.get(serial_number)

    def resolve_state_request(self, serial_number: str) -> None:
        """Drop this serial's parked state request, which is now processed.

        Call it once the processing that peek_state_request handed out ends. A
        call before that turns the retry above into a drop. Idempotent, and a
        serial with nothing parked is no error.
        """
        gl.api_state_requests.pop(serial_number, None)

    # Legs B and C. The whole parking, for a process that leaves.

    def claim_parked_requests(self) -> tuple[list[tuple[str, str]],
                                             list[tuple[str, dict]]]:
        """Everything parked, removed as it hands it over.

        This claims rather than peeks. The caller is a launch that lost the
        race for the application name and passes its requests to the instance
        that won (see src/backend/cli_forward.py). This process applies none
        of them, so a copy left behind serves a retry that never runs, and
        would apply them twice if one ever did.

        The insertion order is the argv order within each kind. The CLI parks
        pages and states in the order it read the flags, and the forwarder
        sends every page change and then every state change, each group in
        that order. Each removal is one dict operation, the same discipline
        the per-serial claim keeps.

        The pre-boot CLI phase calls this, single-threaded and before any deck
        exists, which is the phase the parking happens in.
        """
        pages = [(serial, gl.api_page_requests.pop(serial))
                 for serial in list(gl.api_page_requests)]
        states = [(serial, gl.api_state_requests.pop(serial))
                  for serial in list(gl.api_state_requests)]
        return pages, states


# The process-wide queue. A module singleton rather than a gl slot. A named
# protocol should shrink what lives on the shared namespace.
_queue = StartupQueue()


def get() -> StartupQueue:
    """The process-wide startup queue. Never None."""
    return _queue
