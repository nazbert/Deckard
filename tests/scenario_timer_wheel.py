"""
Unit-tier scenario for the single timer wheel in src/backend/timer_wheel.py.

One daemon scheduler thread backs a min-heap of due times, and 50 concurrent
schedule calls start no further thread. A cancel is idempotent and safe after a
fire.
"""

# A fired callback runs off the scheduler thread, so a slow one cannot delay an
# unrelated due timer.
import threading
import time

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from src.backend import timer_wheel

WATCHDOG_SECONDS = 30


def check_fires_within_tolerance() -> None:
    wheel = timer_wheel.TimerWheel(name="BasicWheel")
    fired_at = []
    t0 = time.monotonic()
    wheel.schedule(0.1, lambda: fired_at.append(time.monotonic()), name="basic")

    assert fixtures.wait_until(lambda: len(fired_at) == 1, timeout=2.0), "timer never fired"
    delta = fired_at[0] - t0
    # A loose lower bound, because a 0.1s schedule must impose a real delay,
    # and a generous liveness ceiling. A tight threshold around 0.1s flakes on
    # scheduler granularity and on a loaded runner.
    assert 0.02 <= delta <= 1.5, f"timer fired outside tolerance: {delta:.3f}s (expected ~0.1s)"

    print(f"PASS: schedule() fires within tolerance ({delta:.3f}s for a 0.1s delay)")


def check_cancel_before_fire_prevents() -> None:
    wheel = timer_wheel.TimerWheel(name="CancelBeforeWheel")
    fired = threading.Event()
    handle = wheel.schedule(0.1, fired.set, name="should-not-fire")
    handle.cancel()

    assert not fired.wait(timeout=0.4), "a timer cancelled before its due time must never fire"
    assert not handle.is_alive(), "a cancelled handle must report not-alive"

    print("PASS: cancel() before fire prevents the callback from running")


def check_cancel_after_fire_is_noop() -> None:
    wheel = timer_wheel.TimerWheel(name="CancelAfterWheel")
    calls = []
    handle = wheel.schedule(0.05, lambda: calls.append(1), name="fires-once")

    assert fixtures.wait_until(lambda: len(calls) == 1, timeout=2.0), "timer never fired"
    # Must not raise, and must not cause a second invocation.
    handle.cancel()
    handle.cancel()  # idempotent even when called twice after firing
    time.sleep(0.2)

    assert calls == [1], f"cancel() after fire must be a no-op, got {calls} calls"
    assert not handle.is_alive(), "a fired handle must report not-alive"

    print("PASS: cancel() after fire is a no-op")


def check_one_thread_for_many_schedules() -> None:
    before = set(threading.enumerate())
    wheel = timer_wheel.TimerWheel(name="ConcurrentTestWheel")
    after_construct = set(threading.enumerate())

    new_threads = after_construct - before
    assert len(new_threads) == 1, (
        f"constructing a TimerWheel must start exactly one thread, got {len(new_threads)}: "
        f"{[t.name for t in new_threads]}"
    )
    scheduler_thread = next(iter(new_threads))
    assert scheduler_thread.name == "ConcurrentTestWheel"
    assert scheduler_thread.daemon, "the scheduler thread must be a daemon thread"

    # 50 threads racing to schedule on one wheel at once. The delay is long
    # enough that none of them fires, and so spawns a dispatch thread, before
    # the thread count is sampled below.
    barrier = threading.Barrier(50)
    handles = []
    handles_lock = threading.Lock()

    def schedule_one():
        barrier.wait(timeout=5)
        h = wheel.schedule(30.0, lambda: None, name="never-fires")
        with handles_lock:
            handles.append(h)

    workers = [threading.Thread(target=schedule_one, name=f"scheduler-caller-{i}") for i in range(50)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=5)
        assert not w.is_alive(), "a scheduling worker thread hung"

    assert len(handles) == 50, f"expected 50 handles, got {len(handles)}"

    after_schedule = set(threading.enumerate())
    still_new = after_schedule - before
    assert still_new == new_threads, (
        f"50 concurrent schedule() calls on one wheel must not spawn additional threads, "
        f"got {len(still_new)}: {[t.name for t in still_new]}"
    )

    for h in handles:
        h.cancel()

    print("PASS: one TimerWheel == one scheduler thread, even under 50 concurrent schedule() calls")


def check_slow_callback_delays_no_other_timer() -> None:
    wheel = timer_wheel.TimerWheel(name="SlowCallbackWheel")
    timeline = []
    timeline_lock = threading.Lock()
    t0 = time.monotonic()

    def slow_cb():
        with timeline_lock:
            timeline.append(("slow_start", time.monotonic()))
        time.sleep(0.5)
        with timeline_lock:
            timeline.append(("slow_end", time.monotonic()))

    def fast_cb():
        with timeline_lock:
            timeline.append(("fast", time.monotonic()))

    # slow_cb is due first and blocks for 0.5s. fast_cb is due 0.1s later and
    # must fire on schedule. A scheduler thread that ran callbacks inline
    # would hold fast_cb until slow_cb returns.
    wheel.schedule(0.05, slow_cb, name="slow")
    wheel.schedule(0.15, fast_cb, name="fast")

    assert fixtures.wait_until(
        lambda: any(name == "fast" for name, _ in timeline), timeout=2.0
    ), "the unrelated fast timer never fired"

    # Let the slow callback finish, so both events are on the timeline and
    # its dispatch thread does not outlive the assertions below.
    assert fixtures.wait_until(
        lambda: any(name == "slow_end" for name, _ in timeline), timeout=2.0
    ), "the slow callback never finished"

    fast_ts = next(ts for name, ts in timeline if name == "fast")
    slow_end_ts = next(ts for name, ts in timeline if name == "slow_end")

    # The claim is about order. The unrelated fast timer must fire while the
    # slow callback is still blocked, which happens only when a callback
    # dispatches off the scheduler thread. Asserting the event order rather
    # than a wall-clock threshold cannot flake on a loaded runner.
    assert fast_ts < slow_end_ts, (
        f"the slow callback delayed the unrelated timer: fast fired at "
        f"{fast_ts - t0:.3f}s, not before the slow callback finished at "
        f"{slow_end_ts - t0:.3f}s -- callbacks are being dispatched inline"
    )
    fast_delay = fast_ts - t0
    assert fast_delay < 0.45, (
        f"fast timer fired far too late ({fast_delay:.3f}s) -- scheduler liveness ceiling"
    )

    print(f"PASS: a slow callback does not delay an unrelated due timer (fast fired at {fast_delay:.3f}s, before slow_end)")


def check_module_level_default_wheel_smoke() -> None:
    """A check on the process-wide singleton the real call sites use, such
    as the screensaver, the overlay hide and the hold timer."""
    fired = threading.Event()
    handle = timer_wheel.schedule(0.05, fired.set, name="default-wheel-smoke")
    assert fired.wait(timeout=2.0), "module-level schedule() on the default wheel never fired"
    handle.cancel()  # no-op after fire, must not raise

    print("PASS: module-level timer_wheel.schedule() smoke check")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_timer_wheel")

    check_fires_within_tolerance()
    check_cancel_before_fire_prevents()
    check_cancel_after_fire_is_noop()
    check_one_thread_for_many_schedules()
    check_slow_callback_delays_no_other_timer()
    check_module_level_default_wheel_smoke()

    print("PASS: scenario_timer_wheel")


if __name__ == "__main__":
    main()
