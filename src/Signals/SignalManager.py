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

from src.Signals.Signals import AppQuit, Signal
from src.Signals.weak_callbacks import CallbackRegistry

from gi.repository import GLib


def _invoke_signal_callback(callback: callable, args: tuple, kwargs: dict) -> bool:
    """GLib.idle_add trampoline for trigger_signal (issue #56).

    Two GLib behaviors made the raw `GLib.idle_add(callback, *args,
    **kwargs)` form wrong for signal handlers: keyword arguments are
    silently dropped (idle_add only forwards positional user_data), and a
    handler returning anything truthy is treated as GLib.SOURCE_CONTINUE --
    the idle source re-runs it on every main-loop iteration forever. The
    trampoline forwards both arg shapes intact and always returns False so
    the source fires exactly once, regardless of the handler's return
    value. A raising handler propagates into the main-loop dispatch, where
    the central exception hooks (issue #80) log it; GLib removes the source
    in that case too.
    """
    callback(*args, **kwargs)
    return False


class SignalManager:
    def __init__(self):
        # signal -> CallbackRegistry. Values are CallbackRegistry instances
        # rather than plain lists (weak storage for bound methods + a lock
        # per registry -- see weak_callbacks.py, design doc D2 / bug 28:
        # trigger_signal used to iterate this dict's lists while any thread
        # could be mutating them, unlocked). A CallbackRegistry is iterable
        # and supports `list(...)`, so `connected_signals[signal]` stays a
        # drop-in for code that read it directly.
        self.connected_signals: dict = {}
        # Guards creation of a new per-signal CallbackRegistry; the
        # registries themselves have their own internal lock for add/
        # remove/snapshot.
        self._registries_lock = threading.Lock()

    def _get_registry(self, signal: Signal, create: bool) -> CallbackRegistry | None:
        registry = self.connected_signals.get(signal)
        if registry is not None or not create:
            return registry
        with self._registries_lock:
            registry = self.connected_signals.get(signal)
            if registry is None:
                registry = CallbackRegistry()
                self.connected_signals[signal] = registry
            return registry

    def connect_signal(self, signal: Signal, callback: callable) -> None:
        # Verify signal
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        # Verify callback
        if not callable(callback):
            raise TypeError("callback must be callable")

        self._get_registry(signal, create=True).add(callback)

    def disconnect_signal(self, signal: Signal, callback: callable) -> None:
        # Verify signal
        if not issubclass(signal, Signal):
            raise TypeError("signal_name must be of type Signal")

        registry = self._get_registry(signal, create=False)
        if registry is not None:
            registry.remove(callback)

    def trigger_signal(self, signal: Signal, *args, **kwargs) -> None:
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
            if signal == AppQuit:
                callback(*args, **kwargs)
            else:
                # Via the trampoline, not GLib.idle_add(callback, *args,
                # **kwargs): that form drops kwargs and re-schedules any
                # truthy-returning handler forever (see
                # _invoke_signal_callback).
                GLib.idle_add(_invoke_signal_callback, callback, args, kwargs)