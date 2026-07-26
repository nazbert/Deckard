"""
Unit-tier scenario for the FIFO transport lock
(issue #164, src/backend/DeckManagement/fair_lock.py).

FairLock replaces the stock threading.Lock the Stream Deck transport uses as
its per-device mutex. The stock lock is unfair: a writer that releases and
immediately re-acquires can out-race the library's HID read poll for many
cycles, which is what made dial input arrive coalesced under a video-write
burst. This is the regression net for the ordering guarantee that replaces
the write-rate cap that used to paper over it.

Covers:
  (a) service order equals acquisition order for N queued threads.
  (b) a hot acquire/release loop cannot overtake a waiter more than the one
      acquisition already in flight -- asserted as an overtake COUNT (a
      logical invariant) plus a generous wall-clock ceiling, so a loaded
      runner can't flake it.
  (c) the context manager releases on the exception path.
  (d) non-blocking acquire, timeout, and release-when-unlocked semantics
      match what a threading.Lock stand-in has to provide, and a timed-out
      waiter never wedges the queue behind its abandoned ticket.
"""
import threading
import time

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from src.backend.DeckManagement.fair_lock import FairLock

WATCHDOG_SECONDS = 60


def _wait_for_queue_depth(lock: FairLock, depth: int) -> None:
    """Blocks until `depth` tickets have been handed out. _next_ticket is
    bumped under the lock's condition at acquire() entry, so it is the exact
    'this thread has queued' signal -- polling a state flag in the worker
    instead would race the ticket draw and make the order under test
    non-deterministic."""
    assert fixtures.wait_until(lambda: lock._next_ticket >= depth, timeout=10.0), (
        f"only {lock._next_ticket} of {depth} tickets were drawn"
    )


def check_service_order_is_arrival_order() -> None:
    lock = FairLock()
    served = []

    lock.acquire()  # Held, so every worker below is forced to queue.

    workers = []
    for index in range(8):
        def _worker(index=index):
            with lock:
                served.append(index)

        t = threading.Thread(target=_worker, name=f"fifo-{index}", daemon=True)
        t.start()
        workers.append(t)
        # One ticket per started thread, drawn before the next thread starts:
        # arrival order is now known, not merely likely.
        _wait_for_queue_depth(lock, index + 2)

    lock.release()
    for t in workers:
        t.join(timeout=10.0)
        assert not t.is_alive(), "a queued waiter never got served"

    assert served == list(range(8)), f"served out of arrival order: {served}"
    assert not lock.locked(), "lock still owned after every waiter released"
    print("PASS: service order equals arrival order")


def check_hot_loop_cannot_starve_a_waiter() -> None:
    HOLD_S = 0.0005
    RUN_S = 2.0

    lock = FairLock()
    acquisitions = 0
    stop = threading.Event()

    def _hot():
        nonlocal acquisitions
        while not stop.is_set():
            with lock:
                acquisitions += 1
                end = time.monotonic() + HOLD_S
                while time.monotonic() < end:
                    pass

    hot = threading.Thread(target=_hot, name="fair-lock-hot", daemon=True)
    hot.start()

    worst_latency = 0.0
    worst_overtakes = 0
    samples = 0
    deadline = time.monotonic() + RUN_S
    while time.monotonic() < deadline:
        before = acquisitions
        t0 = time.monotonic()
        with lock:
            latency = time.monotonic() - t0
            overtakes = acquisitions - before
        worst_latency = max(worst_latency, latency)
        worst_overtakes = max(worst_overtakes, overtakes)
        samples += 1
        time.sleep(0.05)  # The library's 20Hz read poll cadence.

    stop.set()
    hot.join(timeout=10.0)

    assert samples > 10, f"poller only got {samples} samples in {RUN_S}s"
    assert acquisitions > 100, (
        f"hot loop only managed {acquisitions} acquisitions -- FairLock "
        f"throughput collapsed"
    )
    # FIFO bounds the overtakes at the acquisition in flight when the
    # poller drew its ticket; the sampled `before` is read a few bytecodes
    # earlier, so allow a small slack. An unfair lock loses this by orders
    # of magnitude (the hot loop turns over ~1000x per second here).
    assert worst_overtakes <= 5, (
        f"hot loop overtook the waiter {worst_overtakes} times -- ordering "
        f"is not FIFO"
    )
    # Loose ceiling: the read poll needs one slot per 50ms window. The real
    # figure is sub-millisecond; this only has to catch starvation.
    assert worst_latency < 0.05, (
        f"worst acquisition latency {worst_latency * 1000:.1f}ms exceeds one "
        f"poll window"
    )
    print(
        f"PASS: hot loop ({acquisitions} acquisitions) never starved the "
        f"poller (worst {worst_overtakes} overtakes, "
        f"{worst_latency * 1000:.2f}ms)"
    )


def check_exception_path_releases() -> None:
    lock = FairLock()

    class _Boom(Exception):
        pass

    try:
        with lock:
            raise _Boom()
    except _Boom:
        pass
    else:
        raise AssertionError("__exit__ swallowed the exception")

    assert not lock.locked(), "lock still owned after an exception in the body"
    assert lock.acquire(timeout=1.0), "lock unusable after an exception in the body"
    lock.release()
    print("PASS: context manager releases on the exception path")


def check_lock_protocol_semantics() -> None:
    lock = FairLock()

    assert lock.acquire(blocking=False), "non-blocking acquire failed on a free lock"
    assert lock.locked()
    assert not lock.acquire(blocking=False), "non-blocking acquire succeeded while owned"

    t0 = time.monotonic()
    assert not lock.acquire(timeout=0.05), "timed acquire succeeded while owned"
    assert time.monotonic() - t0 < 5.0, "timed acquire ignored its timeout"

    # The abandoned ticket must not stall the queue behind it.
    lock.release()
    assert not lock.locked(), "abandoned ticket left the lock owned"
    assert lock.acquire(timeout=1.0), "abandoned ticket wedged the queue"
    lock.release()

    try:
        lock.release()
    except RuntimeError:
        pass
    else:
        raise AssertionError("releasing an unowned FairLock must raise")

    print("PASS: non-blocking, timeout and release-unlocked semantics hold")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_fair_lock")

    check_service_order_is_arrival_order()
    check_hot_loop_cannot_starve_a_waiter()
    check_exception_path_releases()
    check_lock_protocol_semantics()

    print("PASS: scenario_fair_lock")


if __name__ == "__main__":
    main()
