"""
Pins the app-ready startup queue (src/backend/startup_queue.py) -- the
deferral protocol the notification facade, the plugin manager's
disabled-plugins report and App.on_activate's drain all share.

The protocol is a race protocol, so most of these guards are about ownership:
a task queued before `gl.app` exists must be delivered exactly once, by
exactly one of the two sides that can claim it (the caller, via the
post-append reclaim, or the drain).

Guards:
  1. Before gl.app exists, when_app_ready() queues the task and answers False
     (the caller does NOT deliver); the drain then runs the queued tasks FIFO,
     on the drain caller's thread -- appends may come from any thread, the
     delivery happens where App.on_activate runs.
  2. A task that appends further tasks while the drain runs gets those drained
     too (the iterate-then-clear drain silently discarded them).
  3. Non-callable entries are skipped, not raised on: the list is reachable
     from plugin code, and one bad entry must not strand the tasks behind it.
  4. The append-vs-drain race, both interleavings: gl.app flips between the
     None-check and the append. Whichever side owns the task, it runs exactly
     once -- the reclaim path answers True and leaves nothing queued, the
     drain-wins path answers False and the drain has already delivered.
  5. Readiness is `gl.app`, not an internal flag: it is re-read on every call,
     including after a drain has already run (a "ready" flag would make every
     later call answer True even with gl.app back to None).
  6. The gl slot is read per call and never cached: swapping
     gl.app_loading_finished_tasks for another list must move both the
     appends and the drain onto the new list.
  7. The module stays engine-closure-safe and lock-free: its runtime imports
     are `globals` plus stdlib, and no threading primitive among them --
     GIL-atomic list ops plus the operation order ARE the synchronization.

No GTK: the queue knows nothing about GLib, and neither does this scenario.
"""
import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import ast
import os
import sys
import threading

import globals as gl  # noqa: E402

WATCHDOG_SECONDS = 30

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(_REPO_ROOT, "src", "backend", "startup_queue.py")


class FakeApp:
    """Stands in for the published gl.app. The queue only ever tests it for
    None, so nothing more is needed."""


def call_from_worker(fn, *args):
    """Run fn on a worker thread and hand back its return value -- appends
    come from background threads in production (gl.notify)."""
    result: list = []
    errors: list[BaseException] = []

    def worker():
        try:
            result.append(fn(*args))
        except BaseException as e:  # noqa: BLE001 -- surfaced below
            errors.append(e)

    t = threading.Thread(target=worker, name="startup_queue_caller")
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "when_app_ready hung on the worker thread"
    assert not errors, f"when_app_ready raised on the worker thread: {errors[0]!r}"
    return result[0]


def check_defer_then_drain_fifo(queue) -> None:
    gl.app = None
    gl.app_loading_finished_tasks.clear()

    ran: list[tuple[str, threading.Thread]] = []
    owned_a = call_from_worker(
        queue.when_app_ready, lambda: ran.append(("a", threading.current_thread())))
    owned_b = call_from_worker(
        queue.when_app_ready, lambda: ran.append(("b", threading.current_thread())))

    assert owned_a is False and owned_b is False, (
        "with gl.app None the queue owns the delivery -- when_app_ready must "
        f"answer False, got {owned_a!r}/{owned_b!r}"
    )
    assert len(gl.app_loading_finished_tasks) == 2, (
        f"both tasks must be queued for the drain: {gl.app_loading_finished_tasks}"
    )
    assert ran == [], f"a queued task must not run at call time: {ran}"

    # What App.on_activate does: publish, then drain.
    gl.app = FakeApp()
    queue.drain_app_ready()

    assert [name for name, _ in ran] == ["a", "b"], f"drain order is not FIFO: {ran}"
    assert all(th is threading.main_thread() for _, th in ran), (
        f"queued tasks must run on the drain caller's thread, not the "
        f"appender's: {ran}"
    )
    assert gl.app_loading_finished_tasks == [], (
        f"the drain must leave the queue empty: {gl.app_loading_finished_tasks}"
    )

    print("PASS: pre-app calls defer and drain FIFO on the drain caller's thread")


def check_tasks_appending_tasks(queue) -> None:
    gl.app = FakeApp()
    gl.app_loading_finished_tasks.clear()

    ran: list[str] = []
    gl.app_loading_finished_tasks.append(
        lambda: (ran.append("outer"),
                 gl.app_loading_finished_tasks.append(lambda: ran.append("nested")))
    )
    queue.drain_app_ready()

    assert ran == ["outer", "nested"], (
        f"a task appended during the drain was dropped: {ran}"
    )
    print("PASS: tasks appended during the drain are drained too")


def check_non_callable_entries_skipped(queue) -> None:
    gl.app = FakeApp()
    gl.app_loading_finished_tasks.clear()

    ran: list[str] = []
    gl.app_loading_finished_tasks.append(lambda: ran.append("before"))
    gl.app_loading_finished_tasks.append(None)          # what an append(f()) leaves
    gl.app_loading_finished_tasks.append("not a task")
    gl.app_loading_finished_tasks.append(lambda: ran.append("after"))

    queue.drain_app_ready()

    assert ran == ["before", "after"], (
        f"a non-callable entry must be skipped without stranding the tasks "
        f"queued behind it: {ran}"
    )
    assert gl.app_loading_finished_tasks == [], (
        f"the drain must consume non-callable entries too: "
        f"{gl.app_loading_finished_tasks}"
    )
    print("PASS: non-callable queue entries are skipped, not raised on")


class _FlipOnAppend(list):
    """Drives the append-vs-drain interleaving deterministically: the moment
    the queue appends, on_activate has already published gl.app (and, in the
    drain_first variant, drained before the reclaim can go through)."""

    def __init__(self, app, queue, drain_first: bool = False):
        super().__init__()
        self._app = app
        self._queue = queue
        self._drain_first = drain_first

    def append(self, task):
        gl.app = self._app  # on_activate's publish, racing the append
        super().append(task)

    def remove(self, task):
        if self._drain_first:
            # The drain wins the race to the task: the real drain pops and
            # runs everything before the reclaim attempt goes through.
            self._queue.drain_app_ready()
        super().remove(task)


def check_reclaim_race_both_ways(queue) -> None:
    original = gl.app_loading_finished_tasks

    # A: the drain has already finished when the append lands. The queue must
    # notice, take the task back, and hand ownership to the caller.
    ran: list[str] = []
    gl.app = None
    gl.app_loading_finished_tasks = _FlipOnAppend(FakeApp(), queue)
    try:
        owned = call_from_worker(queue.when_app_ready, lambda: ran.append("reclaimed"))
        assert owned is True, (
            "the app came up during the append: the caller must reclaim the "
            "task and own the delivery"
        )
        assert list(gl.app_loading_finished_tasks) == [], (
            f"the reclaimed task was left stranded on the queue: "
            f"{list(gl.app_loading_finished_tasks)}"
        )
        assert ran == [], (
            "the queue must not run the task itself -- True means the CALLER "
            f"delivers, however it likes: {ran}"
        )

        # B: the drain pops and runs the task before the reclaim. The queue
        # must back off -- exactly one delivery, not two, and not zero.
        ran.clear()
        gl.app = None
        gl.app_loading_finished_tasks = _FlipOnAppend(FakeApp(), queue, drain_first=True)
        owned = call_from_worker(queue.when_app_ready, lambda: ran.append("drained"))
        assert owned is False, (
            "the drain already took the task: when_app_ready must answer "
            "False or the delivery happens twice"
        )
        assert ran == ["drained"], (
            f"the drain-owned task must run exactly once: {ran}"
        )
        assert list(gl.app_loading_finished_tasks) == []
    finally:
        gl.app_loading_finished_tasks = original

    print("PASS: append-vs-drain race is owned by exactly one side (both interleavings)")


def check_readiness_is_gl_app(queue) -> None:
    gl.app = FakeApp()
    gl.app_loading_finished_tasks.clear()

    assert queue.when_app_ready(lambda: None) is True, (
        "with gl.app published the caller owns the delivery immediately"
    )
    assert gl.app_loading_finished_tasks == [], (
        f"nothing may be queued once gl.app exists: {gl.app_loading_finished_tasks}"
    )

    # A drain has now run at least once in this process. Readiness must still
    # be re-read from gl.app: an internal ready-flag would answer True here
    # and skip the queue for calls the app has not come up for. The window is
    # real -- gl.app is published in Main.__init__ before app.run(), so the
    # slot is the only thing that tracks reality across the boot phase.
    queue.drain_app_ready()
    gl.app = None
    assert queue.when_app_ready(lambda: None) is False, (
        "readiness is gl.app, not a latched flag: with gl.app back to None "
        "the queue must own the delivery again"
    )
    assert len(gl.app_loading_finished_tasks) == 1, (
        f"the task must be queued: {gl.app_loading_finished_tasks}"
    )
    gl.app_loading_finished_tasks.clear()

    print("PASS: readiness tracks gl.app on every call")


def check_slot_is_read_per_call(queue) -> None:
    original = gl.app_loading_finished_tasks
    first: list = []
    second: list = []
    try:
        gl.app = None
        gl.app_loading_finished_tasks = first
        assert queue.when_app_ready(lambda: None) is False
        assert len(first) == 1, (
            f"the append must land on the list gl points at now: {first}"
        )

        # Plugins append to the slot directly and tests swap it wholesale; a
        # queue holding the list it found once would silently diverge.
        ran: list[str] = []
        second.append(lambda: ran.append("second-list"))
        gl.app_loading_finished_tasks = second
        assert queue.when_app_ready(lambda: None) is False
        assert len(second) == 2 and len(first) == 1, (
            f"the swap was not honored: first={first} second={second}"
        )

        gl.app = FakeApp()
        queue.drain_app_ready()
        assert ran == ["second-list"], (
            f"the drain must consume the swapped-in list: {ran}"
        )
        assert second == [], f"the swapped-in list must be drained empty: {second}"
        assert len(first) == 1, (
            f"the drain must not touch the list gl no longer points at: {first}"
        )
    finally:
        gl.app_loading_finished_tasks = original

    print("PASS: the gl slot is read per call, never cached")


def check_runtime_imports_are_lock_free_and_engine_safe() -> None:
    """The module must stay importable from the render engine's closure (no
    first-party runtime import but globals) and must not synchronize with a
    lock -- the GIL-atomic list ops plus the append/re-check/remove order are
    the whole protocol, and a lock would change the ownership story."""
    tree = ast.parse(open(MODULE_PATH, encoding="utf-8").read(), MODULE_PATH)

    type_checking_bodies: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            name = getattr(test, "id", None) or getattr(test, "attr", None)
            if name == "TYPE_CHECKING":
                for child in ast.walk(node):
                    type_checking_bodies.add(id(child))

    roots: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_checking_bodies:
            continue
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    assert roots, f"no runtime imports found in {MODULE_PATH}: this check would pass vacuously"
    first_party = {r for r in roots if r not in sys.stdlib_module_names and r != "globals"}
    assert not first_party, (
        f"startup_queue must import nothing first-party but globals -- the "
        f"engine closure imports it: {sorted(first_party)}"
    )
    locking = roots & {"threading", "_thread", "multiprocessing", "asyncio"}
    assert not locking, (
        f"the startup queue is deliberately lock-free (GIL-atomic list ops "
        f"plus the append/re-check/remove order): {sorted(locking)}"
    )

    print("PASS: runtime imports are globals + stdlib, with no locking primitive")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_startup_queue")

    from src.backend import startup_queue

    queue = startup_queue.get()
    assert queue is startup_queue.get(), "startup_queue.get() must be a singleton"

    try:
        check_defer_then_drain_fifo(queue)
        check_tasks_appending_tasks(queue)
        check_non_callable_entries_skipped(queue)
        check_reclaim_race_both_ways(queue)
        check_readiness_is_gl_app(queue)
        check_slot_is_read_per_call(queue)
        check_runtime_imports_are_lock_free_and_engine_safe()
    finally:
        gl.app = None
        gl.app_loading_finished_tasks.clear()

    print("ALL PASS: scenario_startup_queue")


if __name__ == "__main__":
    main()
