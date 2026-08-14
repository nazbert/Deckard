"""Trailing debounce for GTK handlers whose follow-up work is expensive.

Without it, the four Settings font rows each start a reload-all-pages thread
from on_set, so a change of family, size and colour runs up to three full page
reloads at once, and one colour-picker drag alone fires several. One shared
debouncer turns a burst into a single reload after the user stops.

The timer source is injectable, so a test drives the coalescing logic with no
GTK main loop. Production keeps the GLib.source_remove and GLib.timeout_add
pattern that the saturation row uses.

One thread only. trigger() and the callback both run on the thread that drives
the scheduler, which in production is the GTK main thread, because GTK
dispatches the signal handlers there. The pending handle therefore needs no
lock.
"""
import threading
from typing import Any, Callable, Optional, Protocol


class Scheduler(Protocol):
    """The two operations that a trailing debounce needs from a timer."""

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> Any:
        """Arm a one-shot timer and return a handle for cancel."""
        ...

    def cancel(self, handle: Any) -> None:
        """Disarm a timer that has not fired yet."""
        ...


class GLibScheduler:
    """Scheduler backed by the GTK main loop.

    The methods import gi themselves, so an import of this module never pulls
    GTK into a process that does not already hold it.
    """

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> Any:
        from gi.repository import GLib
        return GLib.timeout_add(delay_ms, self._fire, callback)

    def cancel(self, handle: Any) -> None:
        from gi.repository import GLib
        GLib.source_remove(handle)

    @staticmethod
    def _fire(callback: Callable[[], None]) -> bool:
        from gi.repository import GLib
        callback()
        return GLib.SOURCE_REMOVE


class TrailingDebouncer:
    """Coalesce a burst of triggers into one trailing callback.

    Every trigger() disarms the pending timer and arms it again, so the
    callback runs once, delay_ms after the last trigger of the burst.

    Every trigger reaches the callback. A caller relies on one callback run per
    burst, so this class holds no equality check, no dirty flag and no early
    return that can drop a trigger. FontPageGroup carries the note on why the
    work must always happen. A later change may move when the callback fires,
    and it must keep every trigger reaching one fire.
    """

    def __init__(self, delay_ms: int, callback: Callable[[], None],
                 scheduler: Optional[Scheduler] = None):
        self.delay_ms = delay_ms
        self.callback = callback
        self.scheduler: Scheduler = scheduler or GLibScheduler()
        self._pending: Optional[Any] = None
        self._owner_thread: Optional[int] = None

    def trigger(self) -> None:
        """Request a callback delay_ms from now, and replace a pending one."""
        # Enforce the one-thread contract. _pending has no lock, so a second
        # triggering thread races it, which double-fires or calls
        # source_remove on a dead id. Every caller is a GTK signal handler on
        # the main thread. Raise here when that changes.
        ident = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = ident
        elif ident != self._owner_thread:
            raise RuntimeError(
                "TrailingDebouncer.trigger() called from a second thread -- "
                "this class is single-thread by contract (class docstring)")
        if self._pending is not None:
            self.scheduler.cancel(self._pending)
            self._pending = None
        self._pending = self.scheduler.schedule(self.delay_ms, self._fire)

    def _fire(self) -> None:
        # Clear before the call, so a trigger from inside the callback arms a
        # new timer instead of cancelling a source that GLib already removed.
        self._pending = None
        self.callback()
