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
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from loguru import logger as log

# A single persistent worker. max_workers=1 is deliberate: it guarantees
# every batch lands on the *same* OS thread across the process's lifetime,
# so that thread can keep one asyncio event loop alive indefinitely instead
# of paying loop-creation cost per trigger. It also means observer batches
# are dispatched one at a time -- fine, since nothing in the app today
# relies on concurrent observer execution (see module docstring).
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


def _dispatch_batch(observers: list[Callable], label: str | None, args: tuple, kwargs: dict) -> None:
    loop = _get_loop()
    asyncio.set_event_loop(loop)
    for observer in observers:
        try:
            if asyncio.iscoroutinefunction(observer):
                loop.run_until_complete(observer(*args, **kwargs))
            else:
                observer(*args, **kwargs)
        except Exception:
            name = getattr(observer, "__name__", repr(observer))
            where = f" in {label}" if label else ""
            log.error(f"Callback {name}{where} could not be called")


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
    """
    observers = list(observers)
    if not observers:
        return
    future = _dispatch_executor.submit(_dispatch_batch, observers, label, args, kwargs)
    future.add_done_callback(_log_batch_failure)
