"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Observer dispatch for EventHolder and for the AssetManager plugin-settings
Observer, with one lane per event source.

A new asyncio event loop per trigger, and the default executor it creates,
churn file descriptors and threads. AudioControl fires its PulseEvent holder
tens of times per second during a volume change, which makes that churn visible
in telemetry. See docs/memory-footprint-plan.md. Instead, a trigger hands its
batch of observers to a background thread that keeps one loop alive across
events. Lane below states what a lane isolates, and shutdown() states what quit
does to a queue.

Ordering. Batches run FIFO inside a lane. The observers of a batch run in
registration order, one at a time, each in its own try and except, so one
failing observer never stops the rest. The observers of one holder share its
lane, so one that blocks still delays that event source's other observers. A
split of a batch across lanes would lose the registration-order FIFO that
plugins depend on, which tests/scenario_event_dispatch_contract.py pins.
Nothing is ordered across lanes, because dispatch is a queue-and-return from
arbitrary plugin threads.

trigger_event() and notify() return as soon as the batch is queued, before the
observers run. That already holds for the call site that matters.
PulseEvent.trigger_event() runs synchronously inside the dispatch loop of
pulse.event_listen(), and nothing reads a return value or waits for the
observers.

Known limitation. A lane's loop identity changes across an idle reap, so an
observer that captured its running loop for a later call_soon_threadsafe holds
a closed one. No installed plugin does that, and AudioControl is the only
producer of async observers.
"""
import asyncio
import threading
import time
from collections import deque
from typing import Callable, Iterable, TypedDict
from weakref import WeakSet

from loguru import logger as log

# Thread-local, keyed off the lane runner that executes a batch. Each runner
# keeps one asyncio loop alive for its whole life, instead of one loop creation
# per trigger. The runner closes the loop when it exits at the idle reap below,
# so an idle lane holds no epoll fd.
_thread_state = threading.local()


def _get_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_thread_state, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        # A create_task from an observer otherwise dies in asyncio's default
        # stderr handler when nothing retrieves its exception. The import is
        # lazy, so this module imports without an order rule for sys.path.
        from src.backend.log_hooks import asyncio_exception_handler
        loop.set_exception_handler(asyncio_exception_handler)
        _thread_state.loop = loop
    return loop


def _close_thread_loop() -> None:
    """Close the calling runner's loop and reclaim its epoll fd.

    Every runner exit path calls this. A loop that outlives its thread gives
    back the descriptor churn this module removes."""
    loop = getattr(_thread_state, "loop", None)
    _thread_state.loop = None
    if loop is None:
        return
    try:
        asyncio.set_event_loop(None)
        if not loop.is_closed():
            loop.close()
    except Exception:
        log.opt(exception=True).warning("failed to close an event dispatch lane's loop")


# The wedge watchdog. A lane contains a wedged observer, such as a pulsectl
# call that blocks forever, so the app keeps running. That event source stays
# dead until the observer returns, and its queue grows without bound. The
# watchdog reports which lane, which observer, how long, and how much is queued
# behind it. It mirrors the stall warning of the tick loop.
_WEDGE_WARN_S = 10.0
_WEDGE_REWARN_S = 30.0
_MONITOR_INTERVAL_S = 5.0
_BACKLOG_WARN_THRESHOLD = 100

# A runner with nothing to do for this long exits and closes its loop. The next
# dispatch on that lane spawns a new runner, so many idle holders cost no
# thread, and the runner of a hot lane never reaches the timeout.
_IDLE_REAP_S = 60.0

_watch_lock = threading.Lock()
# Every live lane, held weakly. A lane dies with the EventHolder or Observer
# that owns it, and the monitor must not keep either alive. Mutation and
# snapshot both run under _watch_lock. A WeakSet tolerates a GC-driven removal
# during an iteration through _IterationGuard, but not a concurrent add.
_lanes: "WeakSet[Lane]" = WeakSet()
# The app-wide count of queued batches over every lane. Each lane keeps its own
# count. This one serves diagnostics, and every dispatch decision is per lane.
_backlog = 0
_monitor_started = False
_shutdown = False


def _observer_name(observer) -> str:
    return getattr(observer, "__qualname__",
                   getattr(observer, "__name__", repr(observer)))


def _ensure_monitor() -> None:
    global _monitor_started
    # The fast path skips the lock once the monitor started. A stale read here
    # costs one extra lock acquisition for the first concurrent callers, before
    # the flag reads True. The lock below still spawns one thread only.
    if _monitor_started:
        return
    with _watch_lock:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=_monitor_loop, name="event_dispatch_watchdog",
                     daemon=True).start()


def _monitor_tick() -> None:
    # A function of its own, and not an inline block, so the strong lane
    # references of the snapshot die with this frame. In the monitor loop they
    # would stay bound across the next sleep, and a dead holder's lane, with
    # everything its queue pins, would live one more monitor interval.
    with _watch_lock:
        lanes = list(_lanes)
    for lane in lanes:
        lane._check_wedge()


def _monitor_loop() -> None:
    while True:
        time.sleep(_MONITOR_INTERVAL_S)
        if _shutdown:
            return
        try:
            _monitor_tick()
        except Exception:
            # This thread spawns once and never respawns, because
            # _monitor_started stays True for the life of the process. An
            # escaping exception therefore ends wedge reporting for good, and
            # the watchdog is the only thing that names a wedged observer. A
            # failed tick must cost one tick. The sources are rare and real. A
            # lane whose _check_wedge raises stops the reporting of every other
            # lane, and on Python below 3.14 a WeakSet iteration can race a
            # GC-driven removal, because the _remove callback runs on whichever
            # thread drops the last reference, outside _watch_lock.
            log.opt(exception=True).error("event dispatch watchdog tick failed")


class _CurrentObserver(TypedDict):
    """The observer this lane runs now, in Lane.current, if there is one."""
    name: str | None
    label: str | None
    started: float
    next_warn: float


class DispatchShutdown(RuntimeError):
    """dispatch() raises this after shutdown() ran.

    It subclasses RuntimeError, so a direct caller that expects one keeps
    working, and tests/scenario_dispatch_watchdog.py check 5 pins that. It is a
    distinct type, so the plugin-facing entry points swallow this alone and a
    thread-creation RuntimeError out of _spawn_locked still reaches the caller.
    """


class Lane:
    """One serialized dispatch queue, serviced by at most one thread.

    A lane is the unit of wedge isolation. Its runner is the only thread that
    executes its batches, so a blocking observer parks that one daemon thread
    and nothing else. There is no shared pool slot for a wedge to occupy.

    The runner spawns on the first dispatch and exits after _IDLE_REAP_S with
    an empty queue, so an idle lane costs no thread.
    """

    def __init__(self, label: str | None = None):
        self.label = label
        self._cond = threading.Condition()
        self._pending: deque = deque()
        self._runner: threading.Thread | None = None
        # Watchdog state, guarded by the module-wide _watch_lock. Contention
        # stays low, because there are a few lanes and one short critical
        # section per observer. The TypedDict annotation keeps the real type of
        # each slot, and the value is a plain dict at runtime.
        self.current: _CurrentObserver = {"name": None, "label": None, "started": 0.0, "next_warn": 0.0}
        self.backlog = 0
        self.backlog_warned = False
        with _watch_lock:
            _lanes.add(self)

    @property
    def name(self) -> str:
        return self.label or "default"

    # --- producer side ------------------------------------------------

    def dispatch(self, observers: Iterable[Callable], args: tuple, kwargs: dict,
                 label: str | None = None) -> None:
        """Queue observers onto this lane and return."""
        global _backlog
        observers = list(observers)
        if not observers:
            return
        if _shutdown:
            # Checked before the accounting below, so a rejected batch leaks
            # no backlog count.
            raise DispatchShutdown("event dispatch is shut down")
        _ensure_monitor()
        with _watch_lock:
            _backlog += 1
            self.backlog += 1
            backlog = self.backlog
            if backlog >= _BACKLOG_WARN_THRESHOLD and not self.backlog_warned:
                self.backlog_warned = True
                warn_backlog = True
            else:
                if backlog < _BACKLOG_WARN_THRESHOLD // 2:
                    self.backlog_warned = False
                warn_backlog = False
            stuck_name = self.current["name"]
        if warn_backlog:
            log.error(
                f"event dispatch lane {self.name} backlog reached {backlog} "
                f"queued batch(es) -- this lane is stalled"
                + (f" inside observer {stuck_name}" if stuck_name else "")
            )
        try:
            self._enqueue((observers, label, args, kwargs))
        except BaseException:
            # The batch never runs, so the backlog count must not leak.
            with _watch_lock:
                _backlog -= 1
                self.backlog -= 1
            raise

    def _enqueue(self, batch: tuple) -> None:
        with self._cond:
            if _shutdown:
                # Re-checked under the lock the runner exits on. shutdown()
                # sets the flag before it wakes the lanes, so no thread starts
                # a new runner behind it during teardown.
                raise DispatchShutdown("event dispatch is shut down")
            self._pending.append(batch)
            if self._runner is not None:
                self._cond.notify()
                return
            try:
                self._spawn_locked()
            except BaseException:
                self._pending.pop()
                raise

    def _spawn_locked(self) -> None:
        """Start this lane's runner. The caller holds self._cond."""
        runner = threading.Thread(target=self._run, name=f"event_dispatch:{self.name}",
                                  daemon=True)
        self._runner = runner
        try:
            runner.start()
        except BaseException:
            # A thread that never started, booked as the runner, kills the
            # lane. Every later dispatch would notify a thread that does not
            # exist.
            self._runner = None
            raise

    # --- consumer side ------------------------------------------------

    def _run(self) -> None:
        try:
            while True:
                with self._cond:
                    idle_deadline = time.monotonic() + _IDLE_REAP_S
                    while not self._pending:
                        remaining = idle_deadline - time.monotonic()
                        if _shutdown or remaining <= 0:
                            # This clears _runner under the same lock that
                            # guards the emptiness check, which makes the reap
                            # race-free. A dispatch() that appends after this
                            # point finds no runner and spawns one. A dispatch()
                            # that appended before it leaves _pending non-empty,
                            # which stops this exit.
                            self._runner = None
                            return
                        self._cond.wait(remaining)
                    if _shutdown:
                        # Abandon the queue instead of draining it. The
                        # emptiness check above covers an idle runner alone, so
                        # a lane that holds a backlog at quit would otherwise
                        # run plugin observers up to os._exit, against decks
                        # close_all() closed and log sinks on_quit detached. A
                        # measurement without this check counted about 43k
                        # batches dispatched after shutdown() returned. It keeps
                        # the _runner discipline of the reap path above.
                        self._runner = None
                        return
                    batch = self._pending.popleft()
                try:
                    self._run_batch(*batch)
                except Exception:
                    # A batch-level failure comes from the loop creation, and
                    # not from an observer, which the try below covers. A pool
                    # discards such a failure with its Future. This runner logs
                    # it and keeps servicing the lane.
                    log.opt(exception=True).error("event dispatch batch failed before observer dispatch")
        finally:
            self._retire()

    def _retire(self) -> None:
        # The runner-exit invariant holds _runner at None whenever no thread
        # services this lane, on every exit path. A stale runner kills the lane
        # for good. The identity guard matters on the reap path, which cleared
        # _runner already. A new runner can have spawned since, and this must
        # not un-book it.
        try:
            with self._cond:
                if self._runner is threading.current_thread():
                    self._runner = None
                    if self._pending and not _shutdown:
                        # Only a BaseException out of the loop above reaches
                        # this. Hand the queued work to a replacement runner
                        # instead of stranding it.
                        self._spawn_locked()
        except Exception:
            log.opt(exception=True).error(
                f"event dispatch lane {self.name} could not be retired cleanly")
        _close_thread_loop()

    def _run_batch(self, observers: list[Callable], label: str | None,
                   args: tuple, kwargs: dict) -> None:
        global _backlog
        try:
            # _get_loop() must stay inside this try. It creates the loop and
            # imports log_hooks lazily, and both can raise. The finally below
            # owns the backlog decrement of this batch, so a raise before it
            # leaks the count for good.
            loop = _get_loop()
            asyncio.set_event_loop(loop)
            for observer in observers:
                with _watch_lock:
                    self.current["name"] = _observer_name(observer)
                    self.current["label"] = label
                    self.current["started"] = time.monotonic()
                    self.current["next_warn"] = _WEDGE_WARN_S
                try:
                    if asyncio.iscoroutinefunction(observer):
                        loop.run_until_complete(observer(*args, **kwargs))
                    else:
                        observer(*args, **kwargs)
                except Exception:
                    name = getattr(observer, "__name__", repr(observer))
                    where = f" in {label}" if label else ""
                    # opt(exception=True) attaches sys.exc_info(), so the log
                    # gets the observer's whole traceback. A one-line message
                    # here hides a raising plugin callback.
                    log.opt(exception=True).error(f"Callback {name}{where} could not be called")
        finally:
            with _watch_lock:
                self.current["name"] = None
                self.backlog -= 1
                _backlog -= 1

    # --- watchdog -----------------------------------------------------

    def _check_wedge(self) -> None:
        with _watch_lock:
            name = self.current["name"]
            label = self.current["label"]
            started = self.current["started"]
            next_warn = self.current["next_warn"]
            backlog = self.backlog
        if name is None:
            return
        stuck_for = time.monotonic() - started
        if stuck_for < next_warn:
            return
        with _watch_lock:
            if self.current["name"] is not name or self.current["started"] != started:
                # The observer ended, or the next one started, while this
                # warning took shape. Do not move the re-warn clock of the new
                # observer.
                return
            self.current["next_warn"] = stuck_for + _WEDGE_REWARN_S
        # Print the batch label only when it adds to the lane's own name. For
        # an EventHolder's lane the two hold the same event id.
        where = f" in {label}" if label and label != self.label else ""
        log.error(
            f"event dispatch lane {self.name} wedged for {stuck_for:.0f}s "
            f"inside observer {name}{where} -- events on this lane are "
            f"stalled behind it ({backlog} batch(es) queued); other lanes "
            f"are unaffected"
        )


# The lane behind the module-level dispatch() below. Every caller without a
# lane of its own shares this one.
_default_lane = Lane()


def dispatch(observers: Iterable[Callable], args: tuple, kwargs: dict, label: str | None = None) -> None:
    """Queue observers on the shared default lane and return.

    This lane serves the callers that own none. EventHolder and the
    plugin-settings Observer each dispatch on their own lane, so everything
    queued here shares one lane and one fate. A plugin must not block in an
    observer, and the watchdog above names one that does.
    """
    _default_lane.dispatch(observers, args, kwargs, label=label)


def shutdown() -> None:
    """Stop accepting batches and wake every lane runner, so an idle one exits.

    A woken runner returns instead of taking another batch, so the queued
    batches are abandoned and not drained. This interrupts no running batch and
    joins nothing, and the runners are daemon threads that die at os._exit, so
    a wedged lane cannot delay quit. A later dispatch() raises DispatchShutdown.
    """
    global _shutdown
    _shutdown = True
    with _watch_lock:
        lanes = list(_lanes)
    for lane in lanes:
        with lane._cond:
            lane._cond.notify_all()
