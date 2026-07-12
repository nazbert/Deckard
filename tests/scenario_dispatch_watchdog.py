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
  1. A wedged observer produces the watchdog error naming it within the
     monitor interval.
  2. Backlog crossing the threshold produces the backlog error.
  3. After the wedge releases, dispatch drains and later events still run
     (no regression from the accounting).
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
