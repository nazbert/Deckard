"""Toolkit-free main-loop marshalling and background-pool helpers.

This module imports gi.repository.GLib and stdlib only, so an import never
pulls in the widget stack (Gtk, Adw, Gdk, Pango). Engine code under
src/backend must import from here. GtkHelper.GtkHelper re-exports every name
for plugin compatibility.

RUN_ON_MAIN_TIMEOUT_S lives here only. run_on_main reads it at call time so
tests can shrink it, and a name-only re-export elsewhere would be a dead
write.
"""
import functools
import threading
from concurrent.futures import ThreadPoolExecutor

from gi.repository import GLib

from loguru import logger as log


# How long a worker waits for the main loop to service its marshalled call.
# Module-level (read at call time) so tests can shrink it.
RUN_ON_MAIN_TIMEOUT_S = 30


def run_on_main(func, *args, **kwargs):
    """Run func on the GTK main loop and block until it returns. Runs inline
    on the main thread, because GTK4 accepts calls from that thread only.

    A timeout cancels the queued idle source, so either the timeout path or
    the idle callback proceeds, never both. An idle source left in place fires
    after the caller gives up and runs func a second time against the state
    the caller rebuilt (GenerativeUI._ensure_built builds the same row twice).
    """
    if threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)

    done = threading.Event()
    box = {}
    state_lock = threading.Lock()
    # claimed means the idle callback committed to a run of func.
    # abandoned means the caller timed out and cancelled, and the callback
    # must not run. Both transitions happen under state_lock, so only one of
    # them takes effect.
    state = {"claimed": False, "abandoned": False}

    def _cb():
        with state_lock:
            if state["abandoned"]:
                # The caller timed out and moved on. Nothing waits for this
                # result, and a run now executes func a second time.
                return GLib.SOURCE_REMOVE
            state["claimed"] = True
        try:
            box["result"] = func(*args, **kwargs)
        except BaseException as e:
            box["exc"] = e
        finally:
            done.set()
        return GLib.SOURCE_REMOVE

    timeout = RUN_ON_MAIN_TIMEOUT_S
    source_id = GLib.idle_add(_cb)
    # Bound the wait. A main loop that stops pumping, during quit for
    # example, parks this worker and every lock it holds.
    if not done.wait(timeout=timeout):
        with state_lock:
            timed_out = not state["claimed"]
            if timed_out:
                state["abandoned"] = True
                # Under the lock with claimed False the source has not started
                # to dispatch func, so it is still alive to remove. A removal
                # during dispatch only flags the source destroyed.
                GLib.source_remove(source_id)
        if timed_out:
            raise RuntimeError(
                f"main loop did not service run_on_main({getattr(func, '__name__', func)}) "
                f"within {timeout}s"
            )
        # The callback claimed the run at the deadline. The main loop is alive
        # again and func is in flight, so wait for it instead of raising a
        # timeout for a running call.
        if not done.wait(timeout=timeout):
            raise RuntimeError(
                f"run_on_main({getattr(func, '__name__', func)}) started on the main loop "
                f"but did not finish within a further {timeout}s"
            )
    if "exc" in box:
        raise box["exc"]
    return box.get("result")


def on_main(func):
    """Decorator form of run_on_main."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return run_on_main(func, *args, **kwargs)
    return wrapper


# App-lifecycle pool for @background work. It takes I/O-bound work only,
# because the GIL serializes pure-Python CPU work.
#
# The workers are non-daemon. CPython 3.9 removed daemon threads from
# ThreadPoolExecutor (bpo-39812) and offers no way back. Exit still works:
# quit ends in os._exit (src/app.py), and a normal interpreter exit wakes idle
# workers through the atexit queue of concurrent.futures.
_background_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="background")


def _log_background_exception(future):
    try:
        exc = future.exception()
    except Exception:
        return
    if exc is not None:
        log.opt(exception=exc).error("background task raised")


def run_in_background(func, *args, **kwargs):
    """Submit func to the background pool and return its Future. A .result()
    call on the GTK thread deadlocks when the work calls an on_main method."""
    future = _background_pool.submit(func, *args, **kwargs)
    future.add_done_callback(_log_background_exception)
    return future


def background(func):
    """Decorator form of run_in_background."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return run_in_background(func, *args, **kwargs)
    return wrapper


def shutdown_background_pool():
    """Stop the @background pool; call on app quit."""
    _background_pool.shutdown(wait=False, cancel_futures=True)
