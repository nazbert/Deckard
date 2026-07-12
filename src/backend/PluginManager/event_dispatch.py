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
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from loguru import logger as log

# A single persistent worker. max_workers=1 is deliberate: it guarantees
# every batch lands on the *same* OS thread across the process's lifetime,
# so that thread can keep one asyncio event loop alive indefinitely instead
# of paying loop-creation cost per trigger. It also means observer batches
# are dispatched one at a time -- fine, since nothing in the app today
# relies on concurrent observer execution (see module docstring).
#
# The worker is non-daemon: CPython >= 3.9 removed daemon threads from
# ThreadPoolExecutor (bpo-39812) with no restore knob (issue #56, accepted
# residual). Exit is covered anyway -- quit ends in os._exit (src/app.py),
# and a normal interpreter exit unblocks the idle worker via
# concurrent.futures' atexit queue wake-up.
_dispatch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="event_dispatch")

# Keyed off the executor's single worker thread. A thread-local (rather than
# a module global guarded by a lock) is sufficient precisely because
# max_workers=1 above ensures only one thread ever touches it.
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

_watch_lock = threading.Lock()
_current = {"name": None, "label": None, "started": 0.0, "next_warn": 0.0}
_backlog = 0
_backlog_warned = False
_monitor_started = False


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
        with _watch_lock:
            name = _current["name"]
            label = _current["label"]
            started = _current["started"]
            next_warn = _current["next_warn"]
            backlog = _backlog
        if name is None:
            continue
        stuck_for = time.monotonic() - started
        if stuck_for >= next_warn:
            with _watch_lock:
                _current["next_warn"] = stuck_for + _WEDGE_REWARN_S
            where = f" in {label}" if label else ""
            log.error(
                f"event dispatch wedged for {stuck_for:.0f}s inside observer "
                f"{name}{where} -- ALL plugin events app-wide are stalled "
                f"behind it ({backlog} batch(es) queued); see #79 for the "
                f"per-holder-lane refactor"
            )


def _dispatch_batch(observers: list[Callable], label: str | None, args: tuple, kwargs: dict) -> None:
    global _backlog
    try:
        # _get_loop() (loop creation, or the lazy log_hooks import inside it)
        # must be INSIDE this try: it can raise, and the finally below owns
        # the backlog decrement for THIS batch -- a raise before the finally
        # would leak the count permanently (issue #5 review round 1).
        loop = _get_loop()
        asyncio.set_event_loop(loop)
        for observer in observers:
            with _watch_lock:
                _current["name"] = _observer_name(observer)
                _current["label"] = label
                _current["started"] = time.monotonic()
                _current["next_warn"] = _WEDGE_WARN_S
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
            _current["name"] = None
            _backlog -= 1


def _log_batch_failure(future) -> None:
    # Pool-task exceptions live only on the Future -- they never reach
    # threading.excepthook (issue #80). Without this callback an exception
    # escaping _dispatch_batch itself (loop creation, not the per-observer
    # try/except) would vanish into the discarded Future forever.
    try:
        exc = future.exception()
    except Exception:
        return
    if exc is not None:
        log.opt(exception=exc).error("event dispatch batch failed before observer dispatch")


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
    global _backlog, _backlog_warned
    observers = list(observers)
    if not observers:
        return
    _ensure_monitor()
    with _watch_lock:
        _backlog += 1
        backlog = _backlog
        if backlog >= _BACKLOG_WARN_THRESHOLD and not _backlog_warned:
            _backlog_warned = True
            warn_backlog = True
        else:
            if backlog < _BACKLOG_WARN_THRESHOLD // 2:
                _backlog_warned = False
            warn_backlog = False
        stuck_name = _current["name"]
    if warn_backlog:
        log.error(
            f"event dispatch backlog reached {backlog} queued batch(es) -- "
            f"the lane is stalled"
            + (f" inside observer {stuck_name}" if stuck_name else "")
        )
    try:
        future = _dispatch_executor.submit(_dispatch_batch, observers, label, args, kwargs)
    except RuntimeError:
        # Executor already shut down: the batch will never run, so the
        # backlog count must not leak.
        with _watch_lock:
            _backlog -= 1
        raise
    future.add_done_callback(_log_batch_failure)
