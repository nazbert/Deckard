"""
Trailing debounce for GTK-side handlers whose follow-up work is expensive.

Motivating case (#78): the four Settings font rows each spawned their own
``reload-all-pages`` thread straight out of ``on_set``, so changing family,
size and colour back to back ran up to three concurrent full page reloads
(a colour-picker drag alone can fire several). One shared debouncer turns a
burst into a single reload once the user stops fiddling.

The timer source is injectable: the coalescing logic is then testable with
no GTK main loop at all (``tests/scenario_font_reload_debounce.py``), while
production keeps the ``GLib.source_remove``/``GLib.timeout_add`` pattern the
saturation row already uses
(``src/windows/mainWindow/elements/DeckSettings/DeckGroup.py``).

Single-thread only: ``trigger()`` and the callback both run on whichever
thread drives the scheduler -- the GTK main thread in production, since
signal handlers are dispatched there -- so the pending handle needs no lock.
"""
import threading
from typing import Any, Callable, Optional, Protocol


class Scheduler(Protocol):
    """The two operations a trailing debounce needs from a timer source."""

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> Any:
        """Arm a one-shot timer and return a handle for ``cancel``."""
        ...

    def cancel(self, handle: Any) -> None:
        """Disarm a timer that has not fired yet."""
        ...


class GLibScheduler:
    """``Scheduler`` backed by the GTK main loop.

    ``gi`` is imported inside the methods on purpose, so that importing this
    module never drags GTK into a process that does not already have it.
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
    """Coalesce a burst of triggers into exactly one trailing callback.

    Every ``trigger()`` disarms the pending timer and re-arms it, so the
    callback runs once, ``delay_ms`` after the *last* trigger of the burst.

    The callback is DELAYED, NEVER ELIDED. Callers rely on "some change
    happened -> the callback definitely runs, exactly once"; there is
    deliberately no equality check, no dirty flag and no early return that
    could swallow a trigger, because the callers' correctness (see the #78
    note in ``FontPageGroup``) depends on the work always happening. A
    future simplification may change *when* the callback fires; it must not
    make any trigger able to end with no fire at all.
    """

    def __init__(self, delay_ms: int, callback: Callable[[], None],
                 scheduler: Optional[Scheduler] = None):
        self.delay_ms = delay_ms
        self.callback = callback
        self.scheduler: Scheduler = scheduler or GLibScheduler()
        self._pending: Optional[Any] = None
        self._owner_thread: Optional[int] = None

    def trigger(self) -> None:
        """Request a callback ``delay_ms`` from now, replacing any pending one."""
        # Single-thread contract, enforced: _pending has no lock, so a
        # second triggering thread would race it (double fire, or a
        # source_remove on a dead id). All current callers are GTK signal
        # handlers on the main thread; fail loudly if that ever changes
        # instead of racing quietly.
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
        # Cleared before the call, so a trigger raised from inside the
        # callback arms a fresh timer instead of trying to cancel a source
        # that has already been removed.
        self._pending = None
        self.callback()
