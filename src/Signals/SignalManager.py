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

    idle_add forwards no keyword arguments, and it repeats a handler that
    returns a truthy value. This trampoline forwards both argument shapes and
    returns False, so the idle source fires once.
    """
    callback(*args, **kwargs)
    return False


def _safe_describe(callback: Callable[..., Any]) -> str:
    """Name a callback for a log line, without raising.

    describe_callback reads __qualname__ and __module__. On an rpyc netref that
    attribute access goes over the socket and raises EOFError after shutdown.
    The fallbacks read no attribute of the object.
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
        # signal -> CallbackRegistry. A registry holds bound methods weakly and
        # locks its own contents, so trigger_signal never iterates a list that
        # another thread mutates. A registry is iterable and accepts list(), so
        # connected_signals[signal] stays a drop-in for a direct reader.
        self.connected_signals: dict[type[Signal], CallbackRegistry] = {}
        # Guards creation of a per-signal CallbackRegistry. Each registry locks
        # its own add, remove and snapshot.
        self._registries_lock = threading.Lock()

    # create=True always returns a registry and makes one on miss. Only
    # create=False returns None. The overloads keep connect_signal from
    # guarding a branch that cannot occur.
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
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        if not callable(callback):
            raise TypeError("callback must be callable")

        self._get_registry(signal, create=True).add(callback)

    def disconnect_signal(self, signal: type[Signal], callback: Callable[..., Any]) -> None:
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is not None:
            registry.remove(callback)

    def trigger_signal(self, signal: type[Signal], *args: Any, **kwargs: Any) -> None:
        """Dispatch signal asynchronously, from any thread.

        Each observer runs as its own idle callback on the GTK main loop, so
        this returns before any observer runs. A caller that must wait for the
        observers calls trigger_signal_sync instead.
        """
        if not issubclass(signal, Signal):
            raise TypeError("signal must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is None:
            return

        # snapshot() takes the registry's lock and returns a list of the live
        # callbacks. A concurrent connect or disconnect is safe here.
        for callback in registry.snapshot():
            # The trampoline keeps the kwargs and stops the source after one
            # run. See _invoke_signal_callback.
            GLib.idle_add(_invoke_signal_callback, callback, args, kwargs)

    def trigger_signal_sync(self, signal: type[Signal], *args: Any, **kwargs: Any) -> None:
        """Dispatch signal on the calling thread and return after the last one.

        The shutdown fan-out (AppQuit) needs this, because os._exit follows and
        an observer on the main loop never runs. This marshals nothing, so a
        worker thread runs its own observers, GTK-touching ones included.
        """
        if not issubclass(signal, Signal):
            raise TypeError("signal must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is None:
            return

        for callback in registry.snapshot():
            # One failed observer must not deny the others the notification.
            # BaseException also catches a plugin that calls sys.exit() in its
            # quit hook. The os._exit that follows makes this safe to swallow.
            try:
                callback(*args, **kwargs)
            except BaseException:
                log.opt(exception=True).warning(
                    f"{signal.__name__} handler {_safe_describe(callback)} "
                    f"failed; continuing with the remaining handlers"
                )

