"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import threading
from collections.abc import Callable
from typing import Any, Literal, overload

from loguru import logger as log

from src.Signals.Signals import Signal
from src.Signals.weak_callbacks import CallbackRegistry, describe_callback

from gi.repository import GLib


def _invoke_signal_callback(callback: Callable[..., Any], args: tuple[Any, ...],
                            kwargs: dict[str, Any]) -> bool:
    """GLib.idle_add trampoline for trigger_signal.

    Two GLib behaviors made the raw `GLib.idle_add(callback, *args,
    **kwargs)` form wrong for signal handlers: keyword arguments are
    silently dropped (idle_add only forwards positional user_data), and a
    handler returning anything truthy is treated as GLib.SOURCE_CONTINUE --
    the idle source re-runs it on every main-loop iteration forever. The
    trampoline forwards both arg shapes intact and always returns False so
    the source fires exactly once, regardless of the handler's return
    value. A raising handler propagates into the main-loop dispatch, where
    the central exception hooks log it; GLib removes the source
    in that case too.
    """
    callback(*args, **kwargs)
    return False


def _safe_describe(callback: Callable[..., Any]) -> str:
    """Name a callback for a log line, without being able to raise.

    describe_callback() reads the callback's __qualname__/__module__, which is
    an attribute access like any other -- and an observer can be a proxy whose
    attribute accesses go over a socket (an rpyc netref held by a plugin
    backend). Once that connection is gone, naming the handler raises EOFError:
    during shutdown, precisely when the synchronous fan-out is trying to report
    that the handler failed. An error path that can raise its own error is no
    error path, hence the fallbacks -- object.__repr__ reads no attribute of
    the object itself, and the literal covers even that going wrong.
    """
    try:
        return describe_callback(callback)
    except BaseException:
        try:
            return object.__repr__(callback)
        except BaseException:
            return "<unnameable callback>"


class SignalManager:
    def __init__(self):
        # signal -> CallbackRegistry. Values are CallbackRegistry instances
        # rather than plain lists (weak storage for bound methods + a lock
        # per registry -- see weak_callbacks.py, design doc D2 / bug 28:
        # trigger_signal used to iterate this dict's lists while any thread
        # could be mutating them, unlocked). A CallbackRegistry is iterable
        # and supports `list(...)`, so `connected_signals[signal]` stays a
        # drop-in for code that read it directly.
        self.connected_signals: dict[type[Signal], CallbackRegistry] = {}
        # Guards creation of a new per-signal CallbackRegistry; the
        # registries themselves have their own internal lock for add/
        # remove/snapshot.
        self._registries_lock = threading.Lock()

    # create=True always returns a registry (it makes one on miss); only the
    # create=False lookup can come back empty. Two overloads so connect_signal
    # doesn't have to guard a branch that cannot happen.
    @overload
    def _get_registry(self, signal: type[Signal], create: Literal[True]) -> CallbackRegistry: ...
    @overload
    def _get_registry(self, signal: type[Signal], create: bool) -> CallbackRegistry | None: ...

    def _get_registry(self, signal: type[Signal], create: bool) -> CallbackRegistry | None:
        registry = self.connected_signals.get(signal)
        if registry is not None or not create:
            return registry
        with self._registries_lock:
            registry = self.connected_signals.get(signal)
            if registry is None:
                registry = CallbackRegistry()
                self.connected_signals[signal] = registry
            return registry

    def connect_signal(self, signal: type[Signal], callback: Callable[..., Any]) -> None:
        # Verify signal
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        # Verify callback
        if not callable(callback):
            raise TypeError("callback must be callable")

        self._get_registry(signal, create=True).add(callback)

    def disconnect_signal(self, signal: type[Signal], callback: Callable[..., Any]) -> None:
        # Verify signal
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is not None:
            registry.remove(callback)

    def trigger_signal(self, signal: type[Signal], *args: Any, **kwargs: Any) -> None:
        """Dispatch `signal` asynchronously: each observer runs as its own idle
        callback on the GTK main loop, so this returns before any of them has
        been called, from whatever thread called it.

        Uniform for every signal type. A caller that needs the observers to
        have finished before it continues -- the shutdown fan-out, which is
        followed by os._exit -- calls trigger_signal_sync instead.
        """
        # Verify signal
        if not issubclass(signal, Signal):
            raise TypeError("signal must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is None:
            return

        # snapshot() takes the registry's own lock and returns a plain list
        # of currently-live callbacks -- safe to iterate here even while
        # another thread concurrently connects/disconnects.
        for callback in registry.snapshot():
            # Via the trampoline, not GLib.idle_add(callback, *args,
            # **kwargs): that form drops kwargs and re-schedules any
            # truthy-returning handler forever (see _invoke_signal_callback).
            GLib.idle_add(_invoke_signal_callback, callback, args, kwargs)

    def trigger_signal_sync(self, signal: type[Signal], *args: Any, **kwargs: Any) -> None:
        """Dispatch `signal` on the calling thread, one observer after another,
        returning only once all of them have run.

        This exists for the shutdown fan-out (AppQuit): the process ends in
        os._exit a few statements later, so observers queued on the main loop
        would never run at all -- they have to complete inline. Observers run
        on whatever thread calls this, with no marshalling of any kind; a
        caller reaching it from a worker thread hands its observers that
        thread, GTK-touching ones included.

        The observers are strangers to each other's failure modes, so each one
        is invoked inside its own except-BaseException: a third-party plugin
        raising in its quit hook -- or calling sys.exit() in it, which is
        SystemExit and would otherwise unwind the caller just the same --
        cannot deny its peers the notification, and cannot abort the caller's
        teardown either. Swallowing that much is defensible only because this
        path ends in os._exit regardless. Each failure is logged with the
        handler's identity (itself failure-tolerant: naming a handler must not
        be able to raise a second time from inside the error path), and the
        fan-out continues.

        Observers are otherwise treated exactly as trigger_signal treats them:
        one locked snapshot taken up front, and dead weak references skipped
        silently.
        """
        # Verify signal
        if not issubclass(signal, Signal):
            raise TypeError("signal must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is None:
            return

        for callback in registry.snapshot():
            try:
                callback(*args, **kwargs)
            except BaseException:
                log.opt(exception=True).warning(
                    f"{signal.__name__} handler {_safe_describe(callback)} "
                    f"failed; continuing with the remaining handlers"
                )

