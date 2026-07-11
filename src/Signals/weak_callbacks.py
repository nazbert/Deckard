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

CallbackRegistry: a locked, weak-by-default holder for callback
subscriptions (docs/memory-footprint-plan.md D2, bug 27/28).

Bound methods are stored as `weakref.WeakMethod`, so a subscriber's death
(action/controller teardown) silently drops its callback instead of pinning
the owner forever. Plain functions, lambdas and `functools.partial` objects
have no "owner" to weak-ref, so they are stored strong -- matching today's
behavior for that shape of callback. Note this means a closure/lambda that
captures an action (or any other object we'd want to see collected) still
keeps it alive; that pattern needs an explicit `remove()`/`disconnect()`
call, same as before this file existed.

Escape hatch: set `SC_STRONG_CALLBACKS=1` in the environment (read once at
import) to store everything strong, for bisecting a plugin-ecosystem
regression against this file without an app rebuild.
"""
import os
import threading
import weakref
from typing import Callable

from loguru import logger as log

# Read once at import -- this is a debugging knob, not something that should
# change behavior mid-run.
_STRONG_CALLBACKS = os.environ.get("SC_STRONG_CALLBACKS") == "1"

# An entry is either the callback itself (strong) or a weakref.WeakMethod
# (weak). Both shapes resolve to the live callable (or None) via
# _resolve_entry below.
_Entry = object


def _is_bound_method(cb: Callable) -> bool:
    return hasattr(cb, "__self__") and hasattr(cb, "__func__")


def _describe_callback(cb: Callable) -> str:
    """A printable identity for a callback, captured while it's still alive."""
    qualname = getattr(cb, "__qualname__", None) or repr(cb)
    module = getattr(cb, "__module__", None)
    return f"{module}.{qualname}" if module else qualname


class _WeakMethodEntry(weakref.WeakMethod):
    """A WeakMethod that remembers a printable description of the method it
    wrapped. Once the owner dies the WeakMethod resolves to None and can no
    longer say what it used to point at -- so the description has to be
    captured at add() time for snapshot()'s prune log (issue #38) to name
    what was silently dropped. WeakMethod uses __slots__; this subclass
    deliberately doesn't, so `description` can live in a normal instance
    __dict__.
    """

    def __new__(cls, meth: Callable):
        self = super().__new__(cls, meth)
        self.description = _describe_callback(meth)
        return self


def _resolve_entry(entry: _Entry):
    """Return the live callable an entry refers to, or None if it died."""
    if isinstance(entry, weakref.WeakMethod):
        return entry()
    return entry


class CallbackRegistry:
    """Thread-safe collection of callables with weak storage for bound methods.

    `add()`/`remove()` mutate under a lock; `snapshot()` returns a plain
    list of currently-live callables and, as a side effect, drops any
    bound-method entries whose owner has since been garbage collected.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Order is preserved (list, not set) -- some callers have
        # historically relied on connect/call ordering.
        self._entries: list[_Entry] = []

    def _make_entry(self, cb: Callable) -> _Entry:
        if not _STRONG_CALLBACKS and _is_bound_method(cb):
            return _WeakMethodEntry(cb)
        return cb

    def add(self, cb: Callable) -> bool:
        """Add `cb` unless it (or an equal, still-live entry) is already
        present. Returns True if it was added, False if it was a dedupe
        no-op. Also prunes any entries that have died in the meantime.
        """
        with self._lock:
            kept = []
            already_present = False
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    continue  # prune: owner is gone
                kept.append(entry)
                if live is cb or live == cb:
                    already_present = True
            self._entries = kept
            if already_present:
                return False
            self._entries.append(self._make_entry(cb))
            return True

    def remove(self, cb: Callable) -> None:
        """Remove `cb` if present. Silently a no-op otherwise. Also prunes
        any entries that have died in the meantime.
        """
        with self._lock:
            kept = []
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    continue  # prune: owner is gone
                if live is cb or live == cb:
                    continue  # this is the entry being removed
                kept.append(entry)
            self._entries = kept

    def snapshot(self) -> list[Callable]:
        """Return a list of currently-live callables, pruning dead entries
        as a side effect. Each pruned entry is logged at DEBUG (issue #38):
        weak-by-default storage means a bound method of an otherwise
        unreferenced owner silently loses its subscription at the next gc
        pass -- deliberate (D2), but without a trace it's undiagnosable when
        a plugin's events "just stop"; SC_STRONG_CALLBACKS=1 remains the
        bisect hatch.
        """
        pruned: list[str] = []
        with self._lock:
            kept = []
            live_callbacks = []
            for entry in self._entries:
                live = _resolve_entry(entry)
                if live is None:
                    # prune: owner is gone. Only a dead _WeakMethodEntry can
                    # resolve to None (strong entries are the callable itself
                    # and never die out from under us), so `.description` is
                    # always present here; the getattr fallback is pure
                    # belt-and-suspenders.
                    pruned.append(getattr(entry, "description", repr(entry)))
                    continue
                kept.append(entry)
                live_callbacks.append(live)
            self._entries = kept
        # Log outside the lock -- a sink must never be able to re-enter the
        # registry while snapshot() holds it. A given dead entry is pruned
        # from self._entries in the same pass, so it is logged at most once
        # across the process; the only way to double-log is two threads
        # racing snapshot() on the same still-dead entry (benign duplicate
        # DEBUG line, no state corruption -- the locked _entries reassignment
        # is last-write-wins).
        for description in pruned:
            log.debug(
                f"CallbackRegistry: pruning dead callback {description} "
                f"(owner was garbage-collected before it was removed)"
            )
        return live_callbacks

    def __iter__(self):
        # Callers that iterate a registry directly (`for cb in registry`,
        # `list(registry)`) get the same live/pruned view as snapshot().
        return iter(self.snapshot())

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for entry in self._entries if _resolve_entry(entry) is not None)
