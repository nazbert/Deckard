"""
Toolkit-free main-loop marshalling and background-pool helpers.

This module is the engine-legal home of the thread kit that used to live in
``GtkHelper.GtkHelper``: it imports ``gi.repository.GLib`` and stdlib only, so
importing it never pulls in the widget stack (Gtk/Adw/Gdk/Pango). Engine code
(``src/backend/**``) must import from here; ``GtkHelper.GtkHelper`` re-exports
every name for plugin compatibility.

``RUN_ON_MAIN_TIMEOUT_S`` lives here too, and here only -- it is read at call
time so tests can shrink it, which means a name-only re-export elsewhere would
be a dead write.
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
    """Run func on the GTK main loop and block until it returns; runs inline if
    already on the main thread (GTK4 is main-thread-only).

    On timeout the queued idle source is CANCELLED, not abandoned: exactly one
    of {timeout path, idle callback} proceeds. An abandoned idle firing after
    the caller gave up would re-run func against whatever state the caller
    rebuilt in the meantime (e.g. GenerativeUI._ensure_built's retry building
    the same row twice)."""
    if threading.current_thread() is threading.main_thread():
        return func(*args, **kwargs)

    done = threading.Event()
    box = {}
    state_lock = threading.Lock()
    # claimed: the idle callback committed to running func.
    # abandoned: the caller timed out and cancelled; the callback must not run.
    # Both transitions happen under state_lock, so they are mutually exclusive.
    state = {"claimed": False, "abandoned": False}

    def _cb():
        with state_lock:
            if state["abandoned"]:
                # The caller timed out and moved on; nobody is waiting for
                # this result and running func now would double-execute it.
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
    # Bounded: if the main loop stops pumping (e.g. during quit), waiting
    # forever would park this worker and every lock it holds.
    if not done.wait(timeout=timeout):
        with state_lock:
            timed_out = not state["claimed"]
            if timed_out:
                state["abandoned"] = True
                # Safe: under the lock with claimed False the source hasn't
                # started dispatching func, so it's still alive to remove
                # (removal during dispatch just flags it destroyed).
                GLib.source_remove(source_id)
        if timed_out:
            raise RuntimeError(
                f"main loop did not service run_on_main({getattr(func, '__name__', func)}) "
                f"within {timeout}s"
            )
        # The callback claimed execution right at the deadline -- the main
        # loop is alive again and func is in flight, so wait it out rather
        # than raising a timeout for a call that IS running.
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


# App-lifecycle pool for @background work; I/O-bound work only (the GIL
# serializes pure-Python CPU).
#
# Workers are non-daemon: CPython >= 3.9 removed daemon threads from
# ThreadPoolExecutor (bpo-39812) and exposes no knob to restore them
# (accepted residual). Exit is covered anyway -- quit ends in
# os._exit (src/app.py), and a normal interpreter exit unblocks idle
# workers via concurrent.futures' atexit queue wake-ups.
_background_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="background")


def _log_background_exception(future):
    try:
        exc = future.exception()
    except Exception:
        return
    if exc is not None:
        log.opt(exception=exc).error("background task raised")


def run_in_background(func, *args, **kwargs):
    """Submit func to the background pool and return its Future. Never .result()
    it on the GTK thread while the work calls an on_main method -- that deadlocks."""
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
