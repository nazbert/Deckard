"""
Scenario: a wedged observer starves only its own holder's lane (issue #178).

scenario_dispatch_watchdog.py covers the diagnostics half of the same defect
(B-05): a blocking observer used to stall plugin events APP-WIDE, and the
watchdog made that loud. This file covers the containment half, which is what
per-holder lanes buy: an observer that never returns parks exactly one daemon
thread -- its own holder's -- while every other holder keeps delivering.

Checks (real dispatcher, real EventHolders, watchdog thresholds patched
down):
  1. A wedged observer on holder A does not delay holder B: five B events
     land inside a deadline far shorter than A's wedge, and A's observer is
     provably still parked.
  2. The watchdog still fires and still names the culprit -- now also naming
     the lane, and no longer claiming an app-wide stall.
  3. Per-lane FIFO survives the incident: events queued behind the wedge run
     in trigger order once it releases, and the lane keeps working.
  4. Two simultaneous wedges do not couple. This is the property a shared
     thread pool cannot offer: with max_workers=N the N-th wedge silently
     restores the app-wide stall, whereas an independent thread per lane has
     no exhaustion cliff.
  5. The runner-exit invariant: a lane whose idle runner was reaped must
     spawn a fresh one on the next event, not go quietly dead.
  6. shutdown() retires every idle lane runner (the on_quit contract).

Deck-independent -- exercises event_dispatch + real EventHolders directly, no
FakeDeck, no controller.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from loguru import logger as log

from fixtures import start_watchdog, wait_until

import src.backend.PluginManager.event_dispatch as ed
from src.backend.PluginManager.EventHolder import EventHolder


# How long an un-wedged lane gets to deliver. Real delivery latency here is
# sub-millisecond (a condition notify, or one thread spawn on a cold lane),
# so this is ~3 orders of magnitude of headroom for a loaded machine -- while
# still being 15x shorter than the wedge a single-lane implementation would
# make the other holder wait behind. There is no plausible middle ground for
# this assertion to flake in.
DELIVER_S = 2.0
# How long a wedged observer holds its lane. Bounded (not forever) so a
# regression cannot leave the scenario's daemon threads parked past the run.
WEDGE_HOLD_S = 30.0

# Every gate handed to a wedging observer, so a failing check can release
# them all on the way out instead of leaving threads parked.
_GATES: list[threading.Event] = []


def new_gate() -> threading.Event:
    gate = threading.Event()
    _GATES.append(gate)
    return gate


def release_all() -> None:
    for gate in _GATES:
        gate.set()


def main() -> int:
    start_watchdog(40, "dispatch_lanes")

    # Tighten the watchdog so check 2 runs in fractions of a second. The idle
    # reap is left alone until check 5 -- lanes must stay hot for 1-4.
    ed._WEDGE_WARN_S = 0.3
    ed._WEDGE_REWARN_S = 0.5
    ed._MONITOR_INTERVAL_S = 0.1

    records: list[str] = []
    log.add(lambda msg: records.append(str(msg)), level="ERROR")

    # ---------------------------------------------------------------- #
    # 1) a wedged holder must not delay another holder
    # ---------------------------------------------------------------- #
    holder_a = EventHolder(plugin_base=None, event_id="test::lane-a")
    holder_b = EventHolder(plugin_base=None, event_id="test::lane-b")

    gate_a = new_gate()
    a_started = threading.Event()
    a_payloads: list[int] = []   # recorded on entry (before the wedge)
    a_finished: list[int] = []   # recorded on exit (after the wedge releases)
    b_ran: list[int] = []

    def a_observer(event_id, payload):
        a_payloads.append(payload)
        if payload == 1:
            # The pulsectl precedent: a callback that simply never returns.
            a_started.set()
            gate_a.wait(timeout=WEDGE_HOLD_S)
        a_finished.append(payload)

    def b_observer(event_id, payload):
        b_ran.append(payload)

    holder_a.add_listener(a_observer)
    holder_b.add_listener(b_observer)

    holder_a.trigger_event(1)
    if not wait_until(a_started.is_set, timeout=5):
        print("FAIL(1): holder A's observer never started -- the lane never ran it")
        release_all()
        return 1

    for i in range(5):
        holder_b.trigger_event(i)
    if not wait_until(lambda: len(b_ran) == 5, timeout=DELIVER_S):
        print(f"FAIL(1): holder B delivered only {len(b_ran)}/5 events within "
              f"{DELIVER_S}s while holder A was wedged -- B's events are "
              "queued behind A's blocking observer, which is the app-wide "
              "stall #178 removes")
        release_all()
        return 1
    if a_finished:
        print(f"FAIL(1): holder A's observer completed ({a_finished}) -- the "
              "test seam is broken, nothing was actually wedged")
        release_all()
        return 1
    print("PASS: a wedged holder does not delay another holder's events")

    # ---------------------------------------------------------------- #
    # 2) the watchdog still fires, names the culprit AND the lane
    # ---------------------------------------------------------------- #
    if not wait_until(lambda: any("wedged" in r and "a_observer" in r
                                  for r in records), timeout=5):
        print("FAIL(2): no watchdog error naming the wedged observer -- a "
              "wedge is contained now, but it must still be reported")
        release_all()
        return 1
    wedge_records = [r for r in records if "wedged" in r and "a_observer" in r]
    if not any("test::lane-a" in r for r in wedge_records):
        print(f"FAIL(2): the watchdog error does not name the wedged lane "
              f"({wedge_records[-1]!r}) -- with per-holder lanes the lane is "
              "the actionable part of the report")
        release_all()
        return 1
    if any("app-wide" in r for r in wedge_records):
        print(f"FAIL(2): the watchdog still claims an app-wide stall "
              f"({wedge_records[-1]!r}) -- that is no longer true and would "
              "misdirect whoever reads the log")
        release_all()
        return 1
    print("PASS: the watchdog names the wedged observer and its lane")

    # ---------------------------------------------------------------- #
    # 3) per-lane FIFO survives the wedge, and the lane keeps working
    # ---------------------------------------------------------------- #
    holder_a.trigger_event(2)
    holder_a.trigger_event(3)
    gate_a.set()
    if not wait_until(lambda: len(a_finished) == 3, timeout=10):
        print(f"FAIL(3): holder A did not drain after its wedge released "
              f"({a_finished} finished of 3 queued)")
        release_all()
        return 1
    if a_payloads != [1, 2, 3] or a_finished != [1, 2, 3]:
        print(f"FAIL(3): holder A's events did not run in trigger order "
              f"(entered {a_payloads}, finished {a_finished}) -- per-lane "
              "FIFO must survive a wedge incident")
        release_all()
        return 1
    holder_b.trigger_event(99)
    if not wait_until(lambda: b_ran[-1:] == [99], timeout=DELIVER_S):
        print("FAIL(3): holder B stopped delivering after A's wedge incident")
        release_all()
        return 1
    print("PASS: a lane drains in FIFO order after its wedge releases")

    # ---------------------------------------------------------------- #
    # 4) two simultaneous wedges do not couple (no pool-exhaustion cliff)
    # ---------------------------------------------------------------- #
    holder_c = EventHolder(plugin_base=None, event_id="test::lane-c")
    holder_d = EventHolder(plugin_base=None, event_id="test::lane-d")

    gate_c = new_gate()
    gate_d = new_gate()
    c_started, d_started = threading.Event(), threading.Event()
    c_done, d_done = [], []

    def c_observer(event_id):
        c_started.set()
        gate_c.wait(timeout=WEDGE_HOLD_S)
        c_done.append(1)

    def d_observer(event_id):
        d_started.set()
        gate_d.wait(timeout=WEDGE_HOLD_S)
        d_done.append(1)

    holder_c.add_listener(c_observer)
    holder_d.add_listener(d_observer)
    holder_c.trigger_event()
    holder_d.trigger_event()
    if not wait_until(lambda: c_started.is_set() and d_started.is_set(), timeout=5):
        print("FAIL(4): the two wedging observers did not both start -- "
              "their lanes are not independent")
        release_all()
        return 1

    before = len(b_ran)
    holder_b.trigger_event(100)
    if not wait_until(lambda: len(b_ran) == before + 1, timeout=DELIVER_S):
        print(f"FAIL(4): holder B did not deliver within {DELIVER_S}s with two "
              "other lanes wedged -- lanes share a bounded resource, so the "
              "app-wide stall comes back once enough of them wedge at once")
        release_all()
        return 1
    if not wait_until(lambda: any("test::lane-c" in r and "wedged" in r for r in records)
                      and any("test::lane-d" in r and "wedged" in r for r in records),
                      timeout=5):
        print("FAIL(4): the watchdog did not report both wedged lanes -- "
              "simultaneous wedges must each be attributable")
        release_all()
        return 1

    gate_c.set()
    gate_d.set()
    if not wait_until(lambda: c_done and d_done, timeout=10):
        print(f"FAIL(4): the two wedged lanes did not both drain after release "
              f"(c={c_done}, d={d_done})")
        release_all()
        return 1
    print("PASS: two simultaneous wedges stay independent and both recover")

    # ---------------------------------------------------------------- #
    # 5) a reaped runner must be replaced, not missed
    # ---------------------------------------------------------------- #
    # An idle lane gives its thread (and its asyncio loop's fd) back after
    # _IDLE_REAP_S. If the runner-exit bookkeeping were wrong -- the lane
    # still holding a reference to a thread that has exited -- the lane would
    # be dead forever, and the failure would only show up long after the fact
    # in production. Patched down from 60s so the reap actually happens here.
    ed._IDLE_REAP_S = 0.2
    try:
        holder_e = EventHolder(plugin_base=None, event_id="test::lane-reap")
        e_ran: list[int] = []

        def e_observer(event_id, payload):
            e_ran.append(payload)

        holder_e.add_listener(e_observer)
        holder_e.trigger_event(1)
        if not wait_until(lambda: e_ran == [1], timeout=5):
            print("FAIL(5): the lane never delivered its first event")
            release_all()
            return 1
        if not wait_until(lambda: holder_e._lane._runner is None, timeout=5):
            print("FAIL(5): an idle lane never reaped its runner -- idle "
                  "holders would keep a thread and an epoll fd each")
            release_all()
            return 1
        holder_e.trigger_event(2)
        if not wait_until(lambda: e_ran == [1, 2], timeout=5):
            print(f"FAIL(5): the lane went dead after its runner was reaped "
                  f"(delivered {e_ran}) -- no later event on this holder "
                  "would ever run again")
            release_all()
            return 1
    finally:
        ed._IDLE_REAP_S = 60.0
    print("PASS: a reaped lane spawns a fresh runner for the next event")

    release_all()

    # ---------------------------------------------------------------- #
    # 6) shutdown() retires the idle runners
    # ---------------------------------------------------------------- #
    # What on_quit relies on: no lane thread outlives the dispatcher. It also
    # keeps this scenario from exiting with daemon runners still parked on an
    # asyncio loop, which the interpreter's teardown complains about.
    def lane_threads() -> list[threading.Thread]:
        return [t for t in threading.enumerate()
                if t.name.startswith("event_dispatch:")]

    ed.shutdown()
    if not wait_until(lambda: not lane_threads(), timeout=5):
        print(f"FAIL(6): shutdown() left lane runners alive "
              f"({[t.name for t in lane_threads()]}) -- an idle runner must "
              "exit when the dispatcher is shut down")
        return 1
    print("PASS: shutdown() retires every idle lane runner")

    print("PASS: scenario_dispatch_lanes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
