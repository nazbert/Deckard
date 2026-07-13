"""
Scenario (issue #66): the event-dispatch contract gaps not already pinned by
scenario_plugin_events.py / scenario_onready_ordering.py / scenario_dispatch_
watchdog.py.

Those three (from !13, !31, !39) already cover: a raising observer logging a
traceback and the batch continuing past it; the event-id-prepend contract
(load-bearing for AudioControl); the wedged-observer starvation mode (B-05);
and on_ready ordering / exactly-once. This file adds the two genuine
remainders:

  (a) FIFO ordering -- observers in a single batch run in registration order.
      The dispatcher runs a batch sequentially on one lane
      (event_dispatch.py, max_workers=1); nothing asserted that ordering, so
      a plugin that connects two observers and depends on the first running
      before the second had only convention to lean on.

  (b) trigger_event / dispatch RETURN BEFORE the observers complete. The
      dispatch went async in the branch (queue-and-return) -- a contract that
      is load-bearing for the AudioControl hot path (PulseEvent fires
      synchronously from inside pulse.event_listen()'s own loop and must not
      block on observer completion). Pin it so a future "just await it"
      change can't silently make trigger_event synchronous again.

Deck-independent -- exercises event_dispatch + a real EventHolder directly, no
FakeDeck, no controller.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

import globals as gl
from fixtures import start_watchdog, wait_until

from src.backend.PluginManager import event_dispatch
from src.backend.PluginManager.EventHolder import EventHolder


# ===================================================================== #
# (a) FIFO ordering within a batch
# ===================================================================== #

def check_batch_runs_in_registration_order() -> None:
    order: list[int] = []

    def make(n):
        def observer(*args, **kwargs):
            order.append(n)
        observer.__name__ = f"observer_{n}"
        return observer

    observers = [make(n) for n in range(10)]
    event_dispatch.dispatch(observers, ("evt",), {}, label="test::FIFO")

    assert wait_until(lambda: len(order) == 10, timeout=5.0), (
        f"not all observers ran (order so far: {order})"
    )
    assert order == list(range(10)), (
        f"batch did not run in registration order: {order} -- a plugin that "
        "connects ordered observers relies on FIFO delivery"
    )
    print("PASS: a batch dispatches its observers in registration (FIFO) order")


# ===================================================================== #
# (b) dispatch / trigger_event return before observers complete
# ===================================================================== #

def check_dispatch_returns_before_observer_completes() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_observer(*args, **kwargs):
        started.set()
        # Hold the lane until the assertion below has proven dispatch already
        # returned. Bounded so a regression can't hang the scenario -- the
        # watchdog would catch it, but failing fast is cleaner.
        release.wait(timeout=10)
        finished.set()

    event_dispatch.dispatch([blocking_observer], (), {}, label="test::AsyncReturn")

    # dispatch() must have returned here even though the observer has NOT
    # finished (it is still parked on `release`). If dispatch had become
    # synchronous, control would not reach this line until finished.is_set().
    assert not finished.is_set(), (
        "dispatch() did not return until the observer finished -- the "
        "queue-and-return contract regressed to synchronous dispatch (the "
        "AudioControl PulseEvent hot path must not block on observers)"
    )
    # Prove the observer really is running (queued, on the lane), not skipped.
    assert wait_until(started.is_set, timeout=5.0), (
        "the queued observer never started on the dispatch lane"
    )
    assert not finished.is_set(), "observer finished before it was released -- test seam broken"

    release.set()
    assert wait_until(finished.is_set, timeout=5.0), (
        "observer never completed after release -- the lane is broken"
    )
    print("PASS: dispatch() returns before the observer completes (async queue-and-return)")


def check_trigger_event_returns_before_observer_completes() -> None:
    # Same contract, one layer up, through a real EventHolder.trigger_event
    # (the actual plugin-facing API). A PluginBase is only needed for
    # get_plugin_id() inside EventHolder.__init__ when using event_id_suffix;
    # passing an explicit event_id sidesteps that, so no plugin/manager setup
    # is required here.
    holder = EventHolder(plugin_base=None, event_id="test::HolderAsyncReturn")

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    async def blocking_coroutine_observer(*args, **kwargs):
        # `async def` is the real ecosystem shape (every EventHolder observer
        # today is a coroutine). trigger_event must still return immediately.
        started.set()
        import asyncio
        # Poll the threading.Event from the observer's own loop without
        # blocking that loop's thread against the release for the whole time.
        while not release.is_set():
            await asyncio.sleep(0.01)
        finished.set()

    holder.add_listener(blocking_coroutine_observer)
    holder.trigger_event(123)

    assert not finished.is_set(), (
        "trigger_event() blocked until the observer finished -- it must "
        "queue-and-return (see EventHolder.trigger_event / event_dispatch)"
    )
    assert wait_until(started.is_set, timeout=5.0), (
        "trigger_event's observer never started on the dispatch lane"
    )

    release.set()
    assert wait_until(finished.is_set, timeout=5.0), (
        "trigger_event's observer never completed after release"
    )
    print("PASS: EventHolder.trigger_event returns before its observer completes")


def main() -> None:
    start_watchdog(40, label="scenario_event_dispatch_contract")
    check_batch_runs_in_registration_order()
    check_dispatch_returns_before_observer_completes()
    check_trigger_event_returns_before_observer_completes()
    print("PASS: scenario_event_dispatch_contract")


if __name__ == "__main__":
    main()
