"""
Scenario: a wedged plugin observer must be loud and attributable
(issue #5 / B-05).

The single-lane dispatcher means one blocking observer (pulsectl-wedge
precedent) stalls plugin events APP-WIDE while the queue grows -- and it
used to do so with zero diagnostics. The un-stalling itself is the
per-holder-lanes refactor (#79); this covers the bug half: a >N-second
observer produces an error log naming the observer and the queued backlog,
and a piled-up backlog warns on the submit side too.

Checks (real dispatcher, thresholds patched down):
  1.  A wedged observer produces the watchdog error naming it within the
      monitor interval.
  1b. While still wedged, the watchdog RE-warns with a climbing stall
      duration (a persistent stall must not look resolved after one log).
  2.  Backlog crossing the threshold produces the backlog error.
  3.  After the wedge releases, dispatch drains and later events still run
      (no regression from the accounting).
  4.  Backlog accounting does not leak when a batch raises before the
      observer loop (_get_loop failure): the finally's decrement still runs.
  5.  A submit that fails because the executor is shut down rolls the
      increment back and re-raises.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from loguru import logger as log

from fixtures import start_watchdog, wait_until

import src.backend.PluginManager.event_dispatch as ed


def main() -> int:
    start_watchdog(40, "dispatch_watchdog")

    # Tighten the thresholds so the scenario runs in seconds.
    ed._WEDGE_WARN_S = 0.3
    ed._WEDGE_REWARN_S = 0.5
    ed._MONITOR_INTERVAL_S = 0.1
    ed._BACKLOG_WARN_THRESHOLD = 10

    records: list[str] = []
    log.add(lambda msg: records.append(str(msg)), level="ERROR")

    gate = threading.Event()

    def wedged_observer():
        gate.wait(timeout=20)

    ed.dispatch([wedged_observer], (), {}, label="wedge-test-holder")

    # 1) watchdog names the wedged observer
    if not wait_until(lambda: any("wedged" in r and "wedged_observer" in r
                                  for r in records), timeout=5):
        print("FAIL(1): no watchdog error naming the wedged observer -- a "
              "pulsectl-style wedge would stall all plugin events silently")
        gate.set()
        return 1
    print("PASS: wedged observer is named in the watchdog error")

    # 1b) the watchdog RE-warns while still stuck, with a climbing duration --
    # a wedge that logged once and then went quiet would look resolved. Parse
    # the "wedged for Ns" field out of every wedge record and require at least
    # two distinct warns whose reported stall duration increased.
    def _wedge_durations() -> list[float]:
        out = []
        for r in records:
            if "wedged for" not in r:
                continue
            try:
                out.append(float(r.split("wedged for")[1].split("s inside")[0]))
            except (IndexError, ValueError):
                pass
        return out

    if not wait_until(lambda: len(set(_wedge_durations())) >= 2, timeout=5):
        print("FAIL(1b): watchdog warned once but never re-warned while still "
              f"wedged (durations seen: {_wedge_durations()}) -- a persistent "
              "stall would look resolved after the first log")
        gate.set()
        return 1
    durations = _wedge_durations()
    if max(durations) <= min(durations):
        print(f"FAIL(1b): re-warn durations did not climb ({durations}) -- the "
              "stall clock is not advancing across re-warns")
        gate.set()
        return 1
    print(f"PASS: watchdog re-warns with a climbing stall duration ({durations})")

    # 2) backlog warning while the lane is stalled
    ran = []
    for i in range(15):
        ed.dispatch([lambda i=i: ran.append(i)], (), {})
    if not wait_until(lambda: any("backlog" in r for r in records), timeout=5):
        print("FAIL(2): no backlog warning after "
              f"{ed._BACKLOG_WARN_THRESHOLD}+ queued batches")
        gate.set()
        return 1
    print("PASS: backlog pile-up warns on the submit side")

    # 3) drain after unwedging
    gate.set()
    if not wait_until(lambda: len(ran) == 15, timeout=10):
        print(f"FAIL(3): queued events did not drain after the wedge "
              f"released ({len(ran)}/15 ran)")
        return 1
    later = []
    ed.dispatch([lambda: later.append(1)], (), {})
    if not wait_until(lambda: later == [1], timeout=5):
        print("FAIL(3): dispatch broken after a wedge incident")
        return 1
    print("PASS: lane drains and keeps working after the wedge releases")

    # 4) backlog accounting survives a batch that raises BEFORE the observer
    # loop -- _get_loop() (loop creation / lazy log_hooks import) sits inside
    # the try whose finally owns the decrement, so a raise there must not leak
    # the count. Red-checked: with _get_loop() outside the try, this leaks +1
    # permanently (issue #5 review round 1).
    baseline = ed._backlog
    orig_get_loop = ed._get_loop

    def boom_get_loop():
        raise RuntimeError("simulated loop-creation failure")

    ed._get_loop = boom_get_loop
    try:
        ed.dispatch([lambda: None], (), {}, label="leak-probe")
        # the batch runs on the worker, blows up in _get_loop, and its finally
        # must still decrement -- backlog returns to baseline, does not leak.
        leaked = not wait_until(lambda: ed._backlog == baseline, timeout=5)
    finally:
        ed._get_loop = orig_get_loop
    if leaked:
        print(f"FAIL(4): backlog leaked when _get_loop raised "
              f"(baseline={baseline}, now={ed._backlog}) -- the finally's "
              "decrement was skipped")
        return 1
    print("PASS: backlog does not leak when a batch raises before dispatch")

    # 5) a submit that fails because the executor is shut down must roll the
    # increment back and re-raise (not leave the count stuck +1 forever). This
    # is destructive to the lane, so it runs last.
    baseline = ed._backlog
    ed._dispatch_executor.shutdown(wait=True)
    raised = False
    try:
        ed.dispatch([lambda: None], (), {}, label="shutdown-probe")
    except RuntimeError:
        raised = True
    if not raised:
        print("FAIL(5): dispatch after executor shutdown did not re-raise "
              "RuntimeError -- callers can't tell the batch was dropped")
        return 1
    if ed._backlog != baseline:
        print(f"FAIL(5): backlog leaked on a failed submit "
              f"(baseline={baseline}, now={ed._backlog}) -- the increment was "
              "not rolled back")
        return 1
    print("PASS: a failed submit rolls the backlog back and re-raises")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
