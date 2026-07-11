"""
Unit-tier scenario for the single timer wheel
(docs/memory-footprint-impl-plan.md P5.3, src/backend/timer_wheel.py).

Replaces N sleeping threading.Timer threads (screensaver reset-per-keypress,
overlay hide, key-hold) with one daemon scheduler thread backing a min-heap
of due times. This is the regression net for that thread-count claim plus
the handle semantics ported call sites depend on (idempotent cancel, safe
after fire) and the dispatch-model decision (fired callbacks run off the
scheduler thread, so a slow one can't delay an unrelated due timer).

Covers:
  (a) schedule() fires within tolerance of the requested delay.
  (b) cancel() before fire prevents the callback from ever running.
  (c) cancel() after fire is a no-op -- doesn't raise, doesn't re-fire.
  (d) constructing a TimerWheel starts exactly one thread, and 50 concurrent
      schedule() calls on that one wheel start no additional threads (each
      one only pushes onto the shared heap under the wheel's Condition).
  (e) a slow callback (sleeps past a second timer's due time) does not delay
      that unrelated timer -- proof the scheduler thread only pops+dispatches
      and never runs a callback inline.
"""
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
    assert 0.08 <= delta <= 0.6, f"timer fired outside tolerance: {delta:.3f}s (expected ~0.1s)"

    print(f"PASS: schedule() fires within tolerance ({delta:.3f}s for a 0.1s delay)")


def check_cancel_before_fire_prevents_it() -> None:
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


def check_one_scheduler_thread_for_many_schedules() -> None:
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

    # 50 threads racing to schedule on the SAME wheel at once. Delay is long
    # enough that none of them fire (and so spawn a dispatch thread) before
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


def check_slow_callback_does_not_delay_unrelated_timer() -> None:
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

    # slow_cb is due first and blocks for 0.5s; fast_cb is due 0.1s later and
    # must fire on schedule regardless -- if the scheduler thread ran
    # callbacks inline, fast_cb couldn't fire until slow_cb returns (~0.55s).
    wheel.schedule(0.05, slow_cb, name="slow")
    wheel.schedule(0.15, fast_cb, name="fast")

    assert fixtures.wait_until(
        lambda: any(name == "fast" for name, _ in timeline), timeout=2.0
    ), "the unrelated fast timer never fired"

    fast_delay = next(ts for name, ts in timeline if name == "fast") - t0
    assert fast_delay < 0.35, (
        f"the slow callback delayed the unrelated timer: fast fired at {fast_delay:.3f}s "
        f"(expected ~0.15s, well before the slow callback's 0.55s finish)"
    )

    # Let the slow callback finish so its dispatch thread doesn't outlive the
    # assertions below (harmless either way, just tidy).
    assert fixtures.wait_until(
        lambda: any(name == "slow_end" for name, _ in timeline), timeout=2.0
    )

    print(f"PASS: a slow callback does not delay an unrelated due timer (fast fired at {fast_delay:.3f}s)")


def check_module_level_default_wheel_smoke() -> None:
    """Sanity check on the process-wide singleton the real call sites
    (ScreenSaver, overlay hide, hold timer) actually use."""
    fired = threading.Event()
    handle = timer_wheel.schedule(0.05, fired.set, name="default-wheel-smoke")
    assert fired.wait(timeout=2.0), "module-level schedule() on the default wheel never fired"
    handle.cancel()  # no-op after fire, must not raise

    print("PASS: module-level timer_wheel.schedule() smoke check")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_timer_wheel")

    check_fires_within_tolerance()
    check_cancel_before_fire_prevents_it()
    check_cancel_after_fire_is_noop()
    check_one_scheduler_thread_for_many_schedules()
    check_slow_callback_does_not_delay_unrelated_timer()
    check_module_level_default_wheel_smoke()

    print("PASS: scenario_timer_wheel")


if __name__ == "__main__":
    main()
