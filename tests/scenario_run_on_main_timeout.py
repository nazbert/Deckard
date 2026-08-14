"""
run_on_main's timeout path cancels the queued GLib idle source.

Exactly one of the caller's timeout path and the idle callback proceeds.
Otherwise a retry above it, such as GenerativeUI._ensure_built, builds twice
and leaves duplicate widgets. No GTK main loop runs here, and leaving the
default GLib.MainContext unpumped is what stalls the loop for a timeout.
"""
import threading
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib

import src.backend.main_loop as main_loop
from src.backend.main_loop import run_on_main

# Shrink the marshalling bound so a timeout arrives fast. run_on_main reads
# it at call time. The knob lives in main_loop, and GtkHelper only re-exports
# the functions, so a patch there would be a dead write.
main_loop.RUN_ON_MAIN_TIMEOUT_S = 0.4


def _pump(duration: float = 0.2) -> None:
    """Services queued idle callbacks on the default context for a while."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)


def _call_in_thread(fn) -> tuple[threading.Thread, dict]:
    """Runs fn on a worker thread, capturing its result or exception."""
    box: dict = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 -- the assertions need it
            box["exc"] = e

    t = threading.Thread(target=target, name="scenario-worker", daemon=True)
    t.start()
    return t, box


def _pump_until_dead(thread: threading.Thread, timeout: float = 5.0) -> None:
    """Pumps the default context until the worker thread finishes."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)
    thread.join(timeout=0.5)
    assert not thread.is_alive(), "worker did not finish while the context was pumped"


def check_timeout_cancels_idle() -> None:
    runs = []

    def record():
        runs.append(threading.current_thread())

    # A stalled main loop. Nothing pumps the context while the worker waits.
    worker, box = _call_in_thread(lambda: run_on_main(record))
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "worker never returned from run_on_main"
    assert isinstance(box.get("exc"), RuntimeError), (
        f"expected the timeout RuntimeError, got {box!r}"
    )
    assert runs == [], "func ran before the context was ever pumped"

    # The loop resumes, and the abandoned idle must not fire.
    _pump(0.3)
    assert runs == [], (
        f"cancelled idle still executed func ({len(runs)} run(s)) after the "
        f"caller timed out -- the double-execution window"
    )
    print("PASS: timed-out call is cancelled; resuming the loop runs it zero times")


def check_timeout_then_retry_runs_once() -> None:
    runs = []

    def record():
        runs.append(threading.current_thread())
        return "built"

    # The first attempt times out against the unpumped context, as it would
    # in a stalled main loop.
    worker, box = _call_in_thread(lambda: run_on_main(record))
    worker.join(timeout=5.0)
    assert isinstance(box.get("exc"), RuntimeError)

    # The retry runs with the loop alive.
    worker, box = _call_in_thread(lambda: run_on_main(record))
    _pump_until_dead(worker)
    assert box.get("result") == "built", f"retry did not return the result: {box!r}"
    assert len(runs) == 1, (
        f"func executed {len(runs)} times across timeout+retry -- pre-fix this "
        f"was 2 (the abandoned idle plus the retry)"
    )
    print("PASS: timeout followed by retry executes func exactly once")


def check_normal_marshalling() -> None:
    seen = {}

    def probe():
        seen["thread"] = threading.current_thread()
        return 42

    worker, box = _call_in_thread(lambda: run_on_main(probe))
    _pump_until_dead(worker)
    assert box.get("result") == 42, f"unexpected outcome: {box!r}"
    assert seen.get("thread") is threading.main_thread(), (
        "func did not run on the main thread"
    )
    print("PASS: normal off-main call marshals to main and returns the result")


def check_exception_propagates() -> None:
    def boom():
        raise ValueError("intentional")

    worker, box = _call_in_thread(lambda: run_on_main(boom))
    _pump_until_dead(worker)
    assert isinstance(box.get("exc"), ValueError), f"unexpected outcome: {box!r}"
    print("PASS: exceptions raised by func propagate to the calling worker")


def check_inline_on_main_thread() -> None:
    runs = []
    result = run_on_main(lambda: runs.append(1) or "inline")
    assert result == "inline"
    assert runs == [1], "main-thread call did not run inline"
    print("PASS: main-thread caller runs func inline")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_run_on_main_timeout")

    check_timeout_cancels_idle()
    check_timeout_then_retry_runs_once()
    check_normal_marshalling()
    check_exception_propagates()
    check_inline_on_main_thread()

    print("PASS: scenario_run_on_main_timeout")


if __name__ == "__main__":
    main()
