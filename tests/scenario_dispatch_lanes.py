"""A wedged observer starves only its own holder's lane.

Per-holder lanes park one daemon thread while every other holder keeps
delivering. The watchdog still names the culprit and now also names the lane.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

from loguru import logger as log

from fixtures import start_watchdog, wait_until

import src.backend.PluginManager.event_dispatch as ed
from src.backend.PluginManager.EventHolder import EventHolder
from src.backend.PluginManager.PluginSettings.Observer import Observer


# How long an un-wedged lane gets to deliver. Real delivery latency here is
# sub-millisecond, one condition notify or one thread spawn on a cold lane, so
# this leaves three orders of magnitude of headroom on a loaded machine while
# staying 15 times shorter than the wedge a single lane would impose.
DELIVER_S = 2.0
# How long a wedged observer holds its lane. Bounded, so a regression cannot
# leave the daemon threads of this scenario parked past the run.
WEDGE_HOLD_S = 30.0

# Every gate handed to a wedging observer, so a failing check can release them
# all on the way out instead of leaving threads parked.
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

    # Tighten the watchdog, so check 2 runs in fractions of a second. The idle
    # reap stays alone until check 5, because lanes must stay hot for 1 to 4.
    ed._WEDGE_WARN_S = 0.3
    ed._WEDGE_REWARN_S = 0.5
    ed._MONITOR_INTERVAL_S = 0.1

    records: list[str] = []
    log.add(lambda msg: records.append(str(msg)), level="ERROR")

    # 1. A wedged holder must not delay another holder.
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
            # The pulsectl precedent, a callback that never returns.
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
              "stall the per-holder lanes exist to remove")
        release_all()
        return 1
    if a_finished:
        print(f"FAIL(1): holder A's observer completed ({a_finished}) -- the "
              "test seam is broken, nothing was actually wedged")
        release_all()
        return 1
    print("PASS: a wedged holder does not delay another holder's events")

    # 2. The watchdog still fires and names the culprit and the lane.
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

    # 3. Per-lane FIFO survives the wedge, and the lane keeps working.
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

    # 4. Two simultaneous wedges do not couple, so there is no exhaustion cliff.
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

    # 5. A reaped runner must be replaced, not missed. An idle lane gives its
    # thread, and its asyncio loop fd, back after _IDLE_REAP_S. Wrong
    # runner-exit bookkeeping leaves the lane holding an exited thread and dead
    # forever, which shows up long after the fact. Patched down from 60 s here.
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

    # 6. The watchdog survives a failing monitor tick. _ensure_monitor() spawns
    # the monitor once and leaves _monitor_started True forever, so nothing
    # respawns it. An exception escaping its loop ends wedge reporting for every
    # lane, permanently.
    orig_check_wedge = ed.Lane._check_wedge
    boom_ticks: list[int] = []

    def boom_check_wedge(self):
        boom_ticks.append(1)
        raise RuntimeError("simulated watchdog tick failure")

    ed.Lane._check_wedge = boom_check_wedge
    try:
        if not wait_until(lambda: boom_ticks, timeout=5):
            print("FAIL(6): the monitor never ticked, so the check below "
                  "would prove nothing about surviving a failing one")
            release_all()
            return 1
    finally:
        ed.Lane._check_wedge = orig_check_wedge

    records.clear()
    holder_f = EventHolder(plugin_base=None, event_id="test::lane-f")
    gate_f = new_gate()
    f_started = threading.Event()

    def f_observer(event_id):
        f_started.set()
        gate_f.wait(timeout=WEDGE_HOLD_S)

    holder_f.add_listener(f_observer)
    holder_f.trigger_event()
    if not wait_until(f_started.is_set, timeout=5):
        print("FAIL(6): the post-failure probe observer never started")
        release_all()
        return 1
    if not wait_until(lambda: any("wedged" in r and "f_observer" in r
                                  for r in records), timeout=5):
        print("FAIL(6): no wedge was reported after a failing monitor tick "
              "-- the watchdog thread died and is never respawned, so every "
              "later wedge in this process is silent")
        release_all()
        return 1
    gate_f.set()
    print("PASS: the watchdog keeps reporting after a failing monitor tick")

    release_all()

    # 7. shutdown() retires the runners and abandons the queue. No lane thread
    # may outlive the dispatcher, and nothing still queued may run afterwards.
    # on_quit carries on to os._exit while these threads do, so a batch taken
    # after shutdown() would dispatch observers against closed decks and
    # detached log sinks.
    def lane_threads() -> list[threading.Thread]:
        return [t for t in threading.enumerate()
                if t.name.startswith("event_dispatch:")]

    holder_g = EventHolder(plugin_base=None, event_id="test::lane-abandon")
    gate_g = new_gate()
    g_started = threading.Event()
    g_ran: list[int] = []

    def g_observer(event_id, payload):
        if payload == 0:
            g_started.set()
            gate_g.wait(timeout=WEDGE_HOLD_S)
        g_ran.append(payload)

    holder_g.add_listener(g_observer)
    holder_g.trigger_event(0)
    if not wait_until(g_started.is_set, timeout=5):
        print("FAIL(7): the abandon-probe observer never started")
        release_all()
        return 1
    for payload in (1, 2, 3):
        holder_g.trigger_event(payload)

    ed.shutdown()
    gate_g.set()
    if not wait_until(lambda: not lane_threads(), timeout=5):
        print(f"FAIL(7): shutdown() left lane runners alive "
              f"({[t.name for t in lane_threads()]}) -- a runner must exit "
              "when the dispatcher is shut down")
        return 1
    if g_ran != [0]:
        print(f"FAIL(7): batches queued before shutdown() were dispatched "
              f"after it ({g_ran}) -- the in-flight batch may finish, but "
              "the queue must be abandoned, not drained into a torn-down app")
        return 1
    print("PASS: shutdown() retires every runner and abandons the queue")

    # 8. After shutdown trigger_event() and notify() drop quietly.
    # Both promise not to raise, and plugin event
    # sources keep firing until os._exit. The pulse listener of AudioControl is
    # a daemon loop whose callback calls trigger_event(), and a raise there
    # kills that thread with a CRITICAL traceback no caller can act on.
    try:
        holder_b.trigger_event(1234)
    except BaseException as exc:
        print(f"FAIL(8): trigger_event raised after shutdown ({exc!r}) -- a "
              "plugin thread still firing events during quit dies with an "
              "uncaught exception")
        return 1

    notifier = Observer(label="test::post-shutdown-notify")

    def post_shutdown_subscriber(*args):
        pass

    notifier.subscribe(post_shutdown_subscriber)
    try:
        notifier.notify("evt")
    except BaseException as exc:
        print(f"FAIL(8): Observer.notify raised after shutdown ({exc!r}) -- "
              "an asset mutation racing teardown must not propagate")
        return 1

    # Direct callers keep the signal that their batch was dropped.
    raised = False
    try:
        ed.dispatch([lambda: None], (), {}, label="post-shutdown-probe")
    except ed.DispatchShutdown:
        raised = True
    if not raised:
        print("FAIL(8): event_dispatch.dispatch() no longer reports a "
              "post-shutdown batch as dropped -- direct callers lose the "
              "only signal that their batch will never run")
        return 1
    print("PASS: entry points drop quietly post-shutdown, dispatch() still raises")

    print("PASS: scenario_dispatch_lanes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
