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

---

Shared, single-threaded observer dispatcher for EventHolder and the
AssetManager plugin-settings Observer (docs/memory-footprint-plan.md bug
27): both used to build a brand new asyncio event loop (plus its lazily
created default executor) on every single trigger -- churn that shows up
directly in fd/thread telemetry during a PulseAudio event burst (AudioControl
fires its PulseEvent holder tens of times/sec on volume changes).

One background daemon thread now owns one persistent event loop for the
process's lifetime; every trigger_event()/notify() call hands its batch of
observers to that thread instead of building its own loop. Dispatch within a
batch is sequential (not fanned out across threads like the old
`asyncio.to_thread` path) and every observer gets its own try/except -- each
observer runs, and one failing observer never stops the rest (this is
slightly *more* isolated than the pre-existing code, which only wrapped the
non-coroutine branch in a try/except; every real observer in the plugin
ecosystem today is an `async def`, so the old code would have let one
raising observer blow up the whole batch via asyncio.gather).

trigger_event()/notify() return as soon as the batch is queued, before the
observers necessarily run. This was already true in effect for the call site
that matters: `PulseEvent.trigger_event()` is invoked synchronously from
inside `pulse.event_listen()`'s own dispatch loop, and nothing reads a
return value or depends on the observers finishing before the call returns
-- so queuing to the shared dispatcher preserves observable behavior while
removing the per-call event-loop-plus-executor churn.
"""
import asyncio
import threading
import time
from collections import deque
from typing import Callable, Iterable
from weakref import WeakSet

from loguru import logger as log

# Thread-local, keyed off whichever lane runner is executing a batch. Each
# runner keeps ONE asyncio loop alive for as long as it lives, instead of
# paying loop-creation cost per trigger; the loop is closed again when the
# runner exits (idle reap below) so an idle lane holds no epoll fd.
_thread_state = threading.local()


def _get_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_thread_state, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        # An observer's fire-and-forget create_task otherwise dies in
        # asyncio's default stderr handler when its exception is never
        # retrieved (issue #80 §3.5). Imported lazily so this module stays
        # importable without src on sys.path ordering guarantees.
        from src.backend.log_hooks import asyncio_exception_handler
        loop.set_exception_handler(asyncio_exception_handler)
        _thread_state.loop = loop
    return loop


def _close_thread_loop() -> None:
    """Closes the calling runner's loop, reclaiming its epoll fd. Called on
    every runner exit path -- keeping the loop alive past its thread would
    give back exactly the fd churn this module exists to remove."""
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


# --- wedge watchdog (issue #5) ----------------------------------------------
# The single lane means one wedged observer (real precedent: a pulsectl call
# blocking forever) stalls plugin-event delivery APP-WIDE while the queue
# grows without bound -- and used to do so silently. The watchdog cannot
# un-stall it (that is the per-holder-lanes refactor, issue #79); it makes
# the incident loud and attributable: which observer, for how long, how much
# queued behind it. Mirrors the tick loop's >10s stall warning.
_WEDGE_WARN_S = 10.0
_WEDGE_REWARN_S = 30.0
_MONITOR_INTERVAL_S = 5.0
_BACKLOG_WARN_THRESHOLD = 100

# A runner with nothing to do for this long exits (and closes its loop). The
# next dispatch on that lane spawns a fresh one -- so an app with many idle
# holders costs no threads, and the hot lane's runner never reaches the
# timeout in the first place.
_IDLE_REAP_S = 60.0

_watch_lock = threading.Lock()
# Every live lane, weakly: a lane dies with the EventHolder/Observer that
# owns it, and the monitor must not be what keeps either alive. Mutated and
# snapshotted under _watch_lock -- WeakSet tolerates GC-driven removal during
# iteration (_IterationGuard) but not a concurrent add.
_lanes: "WeakSet[Lane]" = WeakSet()
# App-wide queued-batch total across all lanes (per-lane counts live on the
# lanes themselves). Diagnostics only; the dispatch decisions are per lane.
_backlog = 0
_monitor_started = False
_shutdown = False


def _observer_name(observer) -> str:
    return getattr(observer, "__qualname__",
                   getattr(observer, "__name__", repr(observer)))


def _ensure_monitor() -> None:
    global _monitor_started
    # Fast path: once started, skip the lock entirely on the hot dispatch
    # path. A stale read here only ever costs one extra lock acquisition on
    # the very first concurrent callers before the flag is visibly True --
    # the lock below still guarantees exactly one thread is ever spawned.
    if _monitor_started:
        return
    with _watch_lock:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=_monitor_loop, name="event_dispatch_watchdog",
                     daemon=True).start()


def _monitor_loop() -> None:
    while True:
        time.sleep(_MONITOR_INTERVAL_S)
        if _shutdown:
            return
        with _watch_lock:
            lanes = list(_lanes)
        for lane in lanes:
            lane._check_wedge()


class Lane:
    """One serialized dispatch queue, serviced by at most one thread.

    A lane is the unit of wedge isolation: its runner is the only thread that
    ever executes its batches, so a blocking observer parks that one daemon
    thread and nothing else -- there is no shared pool slot to occupy and no
    exhaustion cliff, however many lanes wedge at once.

    The runner is spawned lazily on the first dispatch and exits again after
    _IDLE_REAP_S with an empty queue, so a lane that never fires costs
    nothing and an idle one costs nothing for long.
    """

    def __init__(self, label: str | None = None):
        self.label = label
        self._cond = threading.Condition()
        self._pending: deque = deque()
        self._runner: threading.Thread | None = None
        # Watchdog state, guarded by the module-wide _watch_lock (contention
        # is trivial: a handful of lanes, one short critical section per
        # observer).
        self.current = {"name": None, "label": None, "started": 0.0, "next_warn": 0.0}
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
        """Queues `observers` onto this lane and returns immediately."""
        global _backlog
        observers = list(observers)
        if not observers:
            return
        if _shutdown:
            # Checked before the accounting below so a rejected batch cannot
            # leak a backlog count.
            raise RuntimeError("event dispatch is shut down")
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
            # The batch will never run, so the backlog count must not leak.
            with _watch_lock:
                _backlog -= 1
                self.backlog -= 1
            raise

    def _enqueue(self, batch: tuple) -> None:
        with self._cond:
            if _shutdown:
                # Re-checked under the lock the runner exits on: shutdown()
                # sets the flag before waking the lanes, so nothing can slip
                # a fresh runner in behind it mid-teardown.
                raise RuntimeError("event dispatch is shut down")
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
        """Starts this lane's runner. Caller holds self._cond."""
        runner = threading.Thread(target=self._run, name=f"event_dispatch:{self.name}",
                                  daemon=True)
        self._runner = runner
        try:
            runner.start()
        except BaseException:
            # Leaving a never-started thread booked as the runner would kill
            # the lane permanently (every later dispatch would notify a
            # thread that does not exist).
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
                            # Clearing _runner here -- under the same lock
                            # that guards the emptiness check -- is what
                            # makes the reap race-free: a dispatch() that
                            # appends after this point sees no runner and
                            # spawns one, and one that appended before it
                            # leaves _pending non-empty so we do not exit.
                            self._runner = None
                            return
                        self._cond.wait(remaining)
                    batch = self._pending.popleft()
                try:
                    self._run_batch(*batch)
                except Exception:
                    # A batch-level failure (loop creation, not the
                    # per-observer try/except below) used to vanish into the
                    # pool's discarded Future; a hand-rolled runner logs it
                    # here instead, and keeps servicing the lane (issue #80).
                    log.opt(exception=True).error("event dispatch batch failed before observer dispatch")
        finally:
            self._retire()

    def _retire(self) -> None:
        # Runner-exit invariant: _runner is None whenever no thread is
        # servicing this lane, on EVERY exit path -- otherwise the lane is
        # dead forever. The identity guard matters on the reap path, which
        # already cleared _runner: a fresh runner may have been spawned since
        # and must not be un-booked here.
        try:
            with self._cond:
                if self._runner is threading.current_thread():
                    self._runner = None
                    if self._pending and not _shutdown:
                        # Only reachable when something escaped the loop
                        # above (BaseException); hand the queued work to a
                        # replacement rather than stranding it.
                        self._spawn_locked()
        except Exception:
            log.opt(exception=True).error(
                f"event dispatch lane {self.name} could not be retired cleanly")
        _close_thread_loop()

    def _run_batch(self, observers: list[Callable], label: str | None,
                   args: tuple, kwargs: dict) -> None:
        global _backlog
        try:
            # _get_loop() (loop creation, or the lazy log_hooks import inside
            # it) must be INSIDE this try: it can raise, and the finally
            # below owns the backlog decrement for THIS batch -- a raise
            # before the finally would leak the count permanently (issue #5
            # review round 1).
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
                    # opt(exception=True) attaches sys.exc_info() so the observer's
                    # full traceback lands in the log -- a bare one-liner here made
                    # raising plugin callbacks invisible (issue #33).
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
                # The observer finished (or the next one started) while this
                # warning was being assembled -- do not push the *new*
                # observer's re-warn clock out.
                return
            self.current["next_warn"] = stuck_for + _WEDGE_REWARN_S
        # The batch label is only worth printing when it says something the
        # lane's own name does not (it is the same event id for an
        # EventHolder's lane).
        where = f" in {label}" if label and label != self.label else ""
        log.error(
            f"event dispatch lane {self.name} wedged for {stuck_for:.0f}s "
            f"inside observer {name}{where} -- events on this lane are "
            f"stalled behind it ({backlog} batch(es) queued); other lanes "
            f"are unaffected"
        )


# The lane behind the module-level dispatch() below: everything that does not
# own a lane of its own shares this one.
_default_lane = Lane()


def dispatch(observers: Iterable[Callable], args: tuple, kwargs: dict, label: str | None = None) -> None:
    """Queue `observers` for sequential, exception-isolated dispatch on the
    shared background thread. Returns immediately; observers have not
    necessarily run by the time this returns (see module docstring for why
    that's safe here).

    Cross-plugin coupling (issue #5): all plugins share this ONE dispatch
    lane. A blocking observer delays every other plugin's events, not just
    its own source's -- the watchdog above names the culprit after 10s and
    warns when the backlog piles up. Plugins must not block in observers.
    """
    _default_lane.dispatch(observers, args, kwargs, label=label)


def shutdown() -> None:
    """Stops accepting new batches and wakes every lane runner so idle ones
    exit promptly.

    Queued and in-flight batches are abandoned, not drained: runners are
    daemon threads and quit ends in os._exit (src/app.py), so a wedged lane
    can never delay shutdown. Calls to dispatch() after this raise
    RuntimeError rather than silently dropping their batch.
    """
    global _shutdown
    _shutdown = True
    with _watch_lock:
        lanes = list(_lanes)
    for lane in lanes:
        with lane._cond:
            lane._cond.notify_all()
