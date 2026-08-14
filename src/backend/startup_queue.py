"""Boot-phase deferral for work asked of something that does not exist yet.

This module owns two handshakes for the whole process. Leg A defers a call
until the App runs, and App.on_activate drains it. Legs B and C park a CLI
request, named by deck serial, until that deck appears, and the controller for
the serial claims it on its first default-page load.

This module imports globals and nothing else first-party, so any layer,
including the render engine's import closure, imports it without a cycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import globals as gl

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


# No locks. The GIL-atomic list and dict operations, plus the operation
# orders in the methods below, are the synchronization. A lock cannot make the
# ownership more exclusive, and it puts a boot-phase acquire in front of every
# notification from every thread and in front of every page load.
class StartupQueue:
    """The boot-phase deferral protocols.

    This holds no state. Every method reads the gl slot it works on, per call,
    and caches nothing, so a test that swaps gl.app_loading_finished_tasks,
    and plugin code that appends to it, both keep working.
    """

    # Leg A. Deliveries that wait on the running App.

    def when_app_ready(self, task: Callable[[], Any]) -> bool:
        """True when the caller owns the delivery and must run its work now.
        False when the queue holds task and the drain runs it.

        Safe from any thread. task takes no argument, and nothing reads its
        return value. A True answer does not mean that task ran. The caller
        delivers as it likes, and the notification facade re-enters itself
        through GLib.idle_add rather than call the queued lambda.

        Readiness is gl.app is not None, and no internal flag. gl.app
        publishes in Main.__init__ before app.run(), and again in
        App.on_activate right before the drain. A call that lands in that
        window must skip the queue and marshal itself onto the main loop that
        starts next, which a ready flag flipped at on_activate prevents.

        The append races the drain. on_activate can publish gl.app and finish
        popping the queue between the None check and the append, which strands
        the task. So the order is append, then re-check, then remove. A list
        append, pop and remove are atomic under the GIL, so exactly one side
        owns the task. Another order strands it again.
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

        The last write wins per serial, so two --change-page pairs for one
        serial leave the second. A request that names a serial which never
        connects stays parked for the life of the process, and nothing sweeps
        it. page_name stores as the user typed it, and the claimer resolves it
        to a path, because no page store exists at parking time.

        The pre-boot CLI phase calls this on the main thread, before any deck
        exists.
        """
        gl.api_page_requests[serial_number] = page_name

    def claim_page_request(self, serial_number: str) -> str | None:
        """This serial's parked page name, removed as it hands it over, or
        None when nothing is parked for it.

        Pop it rather than read it. A --change-page request applies once. Left
        in place, it re-applies itself on every later load_default_page() call
        for this serial, which covers every unplug and replug and every "no
        page found" fallback. A state request instead peeks and resolves, so
        it survives a raise; see peek_state_request.

        The lookup and the removal are one dict operation, so two threads that
        race the same serial up cannot both hold the request. Nearly every
        thread in the process reaches this, which is why it is one operation.
        The main thread on the boot path, the boot rescan thread, the USB
        hotplug monitor, a deck's HID reader thread through a screensaver
        dismissal, a plugin or action thread through Page.update_input, and
        the GTK main thread on the no-pages fallback all claim from here.
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
        load_default_page() for this serial sees it again and retries. One
        claim in place of the peek and the resolve turns that retry into a
        drop, which is why a page request claims and a state request does not.

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
        of them, so a copy left behind serves a retry that never runs, and a
        retry that did run applies them twice.

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
