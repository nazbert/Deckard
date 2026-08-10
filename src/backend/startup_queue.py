"""
Boot-phase deferral: work that needs the running ``App`` but is asked for
before it exists.

Two call sites used to carry their own copy of this dance -- the notification
facade (any thread) and the plugin manager's disabled-plugins report (the
pre-GTK main thread, mid ``create_global_objects``) -- while ``App.on_activate``
carried the drain that pairs with them. The protocol is subtle enough that one
copy is the right number, so it lives here.

``when_app_ready(task)`` answers exactly one question: *may I deliver this
myself, right now?* True means yes -- the caller owns the delivery. False means
the task is queued and the drain owns it. Exactly one side ever owns a task.

WHERE THE STATE LIVES

Not here. This is the protocol, not the data: tasks live on
``gl.app_loading_finished_tasks``, and that slot is read on EVERY call, never
cached in an attribute. Plugin code appends to the list directly (it is
reachable, therefore it is API) and tests swap the slot for an instrumented
list; both keep working only as long as this module keeps looking the slot up
instead of holding the object it found once.

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

NO LOCKS, DELIBERATELY

GIL-atomic list operations plus the ordering above ARE the synchronization.
A lock cannot make ownership more exclusive than it already is, and it would
put a boot-phase acquisition in front of every notification from every thread,
including the drain running tasks that enqueue further tasks.

THREAD CONTRACT

``when_app_ready`` is callable from any thread at any point in startup.
``drain_app_ready`` is main-thread-only: ``App.on_activate`` calls it once,
after ``gl.app`` is published.

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
    """The app-ready deferral protocol. Stateless: every method reads the
    ``gl`` slot it works on, so the queue never diverges from the list the
    rest of the process (and plugin code) sees."""

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

        Return values are ignored. A non-callable entry is skipped rather than
        raised on: the list is reachable from plugin code, and one bad entry
        must not strand the tasks queued behind it.
        """
        while gl.app_loading_finished_tasks:
            task = gl.app_loading_finished_tasks.pop(0)
            if callable(task):
                task()


# The process-wide queue. A module singleton rather than a `gl` slot: the point
# of naming this protocol is to shrink what lives on the shared namespace, not
# to add to it.
_queue = StartupQueue()


def get() -> StartupQueue:
    """The process-wide startup queue. Never None."""
    return _queue
