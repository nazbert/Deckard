"""Two remaining gaps in the event-dispatch contract.

Observers in one batch run in registration order, and trigger_event and
dispatch both return before the observers complete.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from fixtures import start_watchdog, wait_until

from src.backend.PluginManager import event_dispatch
from src.backend.PluginManager.EventHolder import EventHolder


# FIFO ordering within a batch

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


# dispatch and trigger_event return before the observers complete

def check_dispatch_returns_before_observer_completes() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_observer(*args, **kwargs):
        started.set()
        # Hold the lane until the assertion below has proven dispatch already
        # returned. Bounded, so a regression cannot hang the scenario. The
        # watchdog would catch it, and failing fast is cleaner.
        release.wait(timeout=10)
        finished.set()

    event_dispatch.dispatch([blocking_observer], (), {}, label="test::AsyncReturn")

    # dispatch() must have returned here although the observer has not
    # finished, because it is still parked on release. A synchronous dispatch
    # would not reach this line until finished.is_set().
    assert not finished.is_set(), (
        "dispatch() did not return until the observer finished -- the "
        "queue-and-return contract regressed to synchronous dispatch (the "
        "AudioControl PulseEvent hot path must not block on observers)"
    )
    # Prove the observer really is running on the lane, not skipped.
    assert wait_until(started.is_set, timeout=5.0), (
        "the queued observer never started on the dispatch lane"
    )
    assert not finished.is_set(), "observer finished before it was released -- test seam broken"

    release.set()
    assert wait_until(finished.is_set, timeout=5.0), (
        "observer never completed after release -- the lane is broken"
    )
    print("PASS: dispatch() returns before the observer completes (async queue-and-return)")


def check_trigger_event_returns_before_observer() -> None:
    # The same contract one layer up, through a real EventHolder.trigger_event,
    # which is the plugin-facing API. A PluginBase is needed only for
    # get_plugin_id() inside EventHolder.__init__ when event_id_suffix is used,
    # and an explicit event_id sidesteps that.
    holder = EventHolder(plugin_base=None, event_id="test::HolderAsyncReturn")

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    async def blocking_coroutine_observer(*args, **kwargs):
        # An async def is the real ecosystem shape, because every EventHolder
        # observer today is a coroutine. trigger_event must still return at once.
        started.set()
        import asyncio
        # Poll the threading.Event from the observer's own loop, without
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
    check_trigger_event_returns_before_observer()
    print("PASS: scenario_event_dispatch_contract")


if __name__ == "__main__":
    main()
