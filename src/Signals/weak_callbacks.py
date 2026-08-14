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

CallbackRegistry is a locked holder for callback subscriptions. See
docs/memory-footprint-plan.md.

The registry stores a bound method as a weakref.WeakMethod, so the teardown
of an action or a controller drops its callback silently. A function, a
lambda and a functools.partial have no owner to weak-ref, so the registry
stores them strong. A lambda that captures an action therefore keeps that
action alive until an explicit remove() or disconnect().

Set SC_STRONG_CALLBACKS=1 in the environment (read once at import) to store
every callback strong. This isolates a plugin regression to this file.
"""
import os
import threading
import weakref
from typing import Callable

from loguru import logger as log

# Read once at import, so this debugging knob cannot change behavior mid-run.
_STRONG_CALLBACKS = os.environ.get("SC_STRONG_CALLBACKS") == "1"

# An entry is either the callback itself (strong) or a weakref.WeakMethod
# (weak). Both shapes resolve to the live callable (or None) via
# _resolve_entry below.
_Entry = object


def _is_bound_method(cb: Callable) -> bool:
    return hasattr(cb, "__self__") and hasattr(cb, "__func__")


def describe_callback(cb: Callable) -> str:
    """Give a printable identity for a live callback.

    Public because the synchronous signal fan-out names a failed handler with
    the same string the prune log uses.
    """
    qualname = getattr(cb, "__qualname__", None) or repr(cb)
    module = getattr(cb, "__module__", None)
    return f"{module}.{qualname}" if module else qualname


class _WeakMethodEntry(weakref.WeakMethod):
    """A WeakMethod that keeps a printable description of its method.

    A dead WeakMethod resolves to None and cannot name its target, so __new__
    captures the description for the prune log. This subclass drops the
    __slots__ of WeakMethod, so description lives in the instance __dict__.
    """

    description: str

    def __new__(cls, meth: Callable):
        self = super().__new__(cls, meth)
        self.description = describe_callback(meth)
        return self


def _resolve_entry(entry: _Entry):
    """Return the live callable an entry refers to, or None if it died."""
    if isinstance(entry, weakref.WeakMethod):
        return entry()
    return entry


class CallbackRegistry:
    """Thread-safe collection of callables. It stores bound methods weakly.

    add() and remove() mutate under a lock. snapshot() returns the live
    callables and drops the entries whose owner the collector took.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # A list and not a set, because callers depend on the connect order.
        self._entries: list[_Entry] = []

    def _make_entry(self, cb: Callable) -> _Entry:
        if not _STRONG_CALLBACKS and _is_bound_method(cb):
            return _WeakMethodEntry(cb)
        return cb

    def add(self, cb: Callable) -> bool:
        """Add cb unless an equal live entry is already present.

        Returns True after an add and False after a dedupe. Also prunes the
        entries that died in the meantime.
        """
        with self._lock:
            kept = []
            already_present = False
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    continue  # the owner is gone, so prune it
                kept.append(entry)
                if live is cb or live == cb:
                    already_present = True
            self._entries = kept
            if already_present:
                return False
            try:
                entry = self._make_entry(cb)
            except TypeError:
                # WeakMethod refuses an owner that is not weak-referenceable
                # (a __slots__ class without __weakref__). Store it strong,
                # because a refused connect costs more than a pinned owner.
                log.debug(
                    f"CallbackRegistry: owner of {cb!r} is not weak-referenceable "
                    f"(__slots__ without __weakref__); storing a strong reference"
                )
                entry = cb
            self._entries.append(entry)
            return True

    def remove(self, cb: Callable) -> None:
        """Remove cb if present, else do nothing. Also prunes dead entries."""
        with self._lock:
            kept = []
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    continue  # the owner is gone, so prune it
                if live is cb or live == cb:
                    continue  # the entry to remove
                kept.append(entry)
            self._entries = kept

    def snapshot(self) -> list[Callable]:
        """Return the live callables and prune the dead entries.

        Each prune logs at DEBUG. A bound method whose owner is unreferenced
        loses its subscription at the next gc pass, with no other trace.
        """
        pruned: list[str] = []
        with self._lock:
            kept = []
            live_callbacks = []
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    # The owner is gone, so prune it. Only a dead
                    # _WeakMethodEntry resolves to None, so it has a
                    # description.
                    pruned.append(getattr(entry, "description", repr(entry)))
                    continue
                kept.append(entry)
                live_callbacks.append(live)
            self._entries = kept
        # Log outside the lock, because a sink must not re-enter the registry
        # while snapshot() holds it. The same pass drops the entry, so one process
        # logs it once. Two threads that race snapshot() on one dead entry log
        # it twice and corrupt no state.
        for description in pruned:
            log.debug(
                f"CallbackRegistry: pruning dead callback {description} "
                f"(owner was garbage-collected before it was removed)"
            )
        return live_callbacks

    def __iter__(self):
        # Direct iteration gives the same live and pruned view as snapshot().
        return iter(self.snapshot())

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for entry in self._entries if _resolve_entry(entry) is not None)
