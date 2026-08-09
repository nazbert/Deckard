from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger as log

from src.backend.PluginManager import event_dispatch
from src.Signals.weak_callbacks import CallbackRegistry

if TYPE_CHECKING:
    from src.backend.PluginManager.PluginBase import PluginBase

class EventHolder:
    """
        Holder for Event Callbacks for the specified Event ID
    """
    def __init__(self, plugin_base: "PluginBase",
                 event_id: str = None,
                 event_id_suffix: str = None):
        if event_id in ["", None] and event_id_suffix in ["", None]:
            raise ValueError("Please specify a signal id")

        self.plugin_base = plugin_base
        self.event_id = event_id or f"{self.plugin_base.get_plugin_id()}::{event_id_suffix}"
        # CallbackRegistry (src/Signals/weak_callbacks.py): bound-method
        # observers are held weakly, so an action/plugin that forgets to
        # remove_listener() on teardown no longer keeps growing this list
        # forever (docs/memory-footprint-plan.md bug 3/27 -- this was the
        # dominant steady-state growth mechanism for event-using plugins
        # like AudioControl).
        self.observers = CallbackRegistry()
        # This holder's own dispatch lane. Observers of THIS
        # event are serialized on one thread of their own, so an observer
        # that blocks (the pulsectl-wedge precedent) stalls only this event
        # source's queue -- every other holder keeps delivering. The lane is
        # owned by the holder, so it dies with it and no registry keying is
        # needed (two holders can legitimately share an event_id).
        self._lane = event_dispatch.Lane(label=self.event_id)

    def add_listener(self, callback: Callable[..., Any]):
        if not self.observers.add(callback):
            # functools.partial (and other callable objects) have no
            # __name__ -- the warning must not crash the connect.
            name = getattr(callback, "__name__", repr(callback))
            log.warning(f"Callback {name} is already subscribed to: {self.event_id}")

    def remove_listener(self, callback: Callable[..., Any]):
        self.observers.remove(callback)

    def trigger_event(self, *args, **kwargs):
        """Fire-and-forget: queues this holder's current observers onto its
        dispatch lane and returns immediately.

        Returning does NOT mean the observers have run -- do not read it as
        "delivered". They run afterwards, sequentially and in registration
        order, on this holder's own lane (per-lane FIFO), so an observer that
        blocks stalls this event source only; no ordering is guaranteed
        relative to other holders' events. See event_dispatch.py.
        """
        # NOTE: the old implementation called
        # `self._run_event(self.event_id, *args, **kwargs)`, which silently
        # prepended `self.event_id` as the observers' first positional
        # argument (AudioControl's on_pulse_device_change reads it as
        # `args[0]` and the real pulsectl event as `args[1]`). Preserve that
        # contract here.
        try:
            self._lane.dispatch(self.observers.snapshot(), (self.event_id, *args), kwargs, label=self.event_id)
        except event_dispatch.DispatchShutdown:
            # on_quit stopped the dispatcher, but plugin event sources keep
            # running until os._exit -- AudioControl's pulse listener is a
            # `while True: pulse.event_listen()` daemon thread whose callback
            # calls this. Letting the shutdown error out of a documented
            # fire-and-forget call would kill that thread with an uncaught
            # RuntimeError (a CRITICAL traceback in logs.log) on every quit
            # that races an event, for nothing a caller could act on. Any
            # OTHER RuntimeError still propagates (see DispatchShutdown).
            log.debug(f"Event {self.event_id} triggered after dispatch shutdown; dropped")
