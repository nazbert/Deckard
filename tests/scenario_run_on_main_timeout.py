"""
Integration scenario for issue #39: run_on_main's timeout path must CANCEL
the queued GLib idle source, not abandon it.

Pre-fix, a worker that timed out waiting for a stalled main loop raised and
moved on -- but the idle callback stayed queued and ran func anyway once the
loop resumed. Combined with GenerativeUI._ensure_built's deliberate
retry-on-failure (GenerativeUI.py:112-119) that produced two build()
executions: duplicate widgets, doubly-connected signals. The fix's contract:
exactly one of {caller timeout path, idle callback} proceeds.

Follows scenario_genui_lazy.py's conventions: no GTK main loop runs; the
default GLib.MainContext is pumped manually, which doubles as the "stalled
main loop" control -- the context simply isn't pumped while a timeout is
being provoked.

  (a) Timeout cancels the idle: a worker times out against an unpumped
      context; pumping the context afterwards must run func ZERO times
      (pre-fix: once).
  (b) Timeout-then-retry runs once: after a timeout, a retry (the
      _ensure_built shape) against a pumped context executes func exactly
      once in total (pre-fix: twice) and returns its result.
  (c) Normal marshalling still works: a worker's call runs on the main
      thread and returns the result.
  (d) Exceptions still propagate to the calling worker.
  (e) Inline fast-path: on the main thread func runs synchronously, no
      pumping required.
"""
import threading
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib

import GtkHelper.GtkHelper as gtk_helper
from GtkHelper.GtkHelper import run_on_main

# Shrink the marshalling bound so provoking a timeout is fast. Read at call
# time by run_on_main.
gtk_helper.RUN_ON_MAIN_TIMEOUT_S = 0.4


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

    # Stalled main loop: nothing pumps the context while the worker waits.
    worker, box = _call_in_thread(lambda: run_on_main(record))
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "worker never returned from run_on_main"
    assert isinstance(box.get("exc"), RuntimeError), (
        f"expected the timeout RuntimeError, got {box!r}"
    )
    assert runs == [], "func ran before the context was ever pumped"

    # The loop "resumes": the abandoned idle must NOT fire.
    _pump(0.3)
    assert runs == [], (
        f"cancelled idle still executed func ({len(runs)} run(s)) after the "
        f"caller timed out -- issue #39's double-execution window"
    )
    print("PASS: timed-out call is cancelled; resuming the loop runs it zero times")


def check_timeout_then_retry_runs_once() -> None:
    runs = []

    def record():
        runs.append(threading.current_thread())
        return "built"

    # First attempt: times out against the unpumped context (as in a stalled
    # main loop during _ensure_built).
    worker, box = _call_in_thread(lambda: run_on_main(record))
    worker.join(timeout=5.0)
    assert isinstance(box.get("exc"), RuntimeError)

    # Retry (what _ensure_built's un-latching enables) with the loop alive.
    worker, box = _call_in_thread(lambda: run_on_main(record))
    _pump_until_dead(worker)
    assert box.get("result") == "built", f"retry did not return the result: {box!r}"
    assert len(runs) == 1, (
        f"func executed {len(runs)} times across timeout+retry -- pre-#39 this "
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
