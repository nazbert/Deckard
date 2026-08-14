from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger as log

from src.backend.PluginManager import event_dispatch
from src.Signals.weak_callbacks import CallbackRegistry

if TYPE_CHECKING:
    from src.backend.PluginManager.PluginBase import PluginBase

class EventHolder:
    """Holds the event callbacks of one event id."""
    def __init__(self, plugin_base: "PluginBase",
                 event_id: str = None,
                 event_id_suffix: str = None):
        if event_id in ["", None] and event_id_suffix in ["", None]:
            raise ValueError("Please specify a signal id")

        self.plugin_base = plugin_base
        self.event_id = event_id or f"{self.plugin_base.get_plugin_id()}::{event_id_suffix}"
        # A CallbackRegistry (src/Signals/weak_callbacks.py) holds a
        # bound-method observer weakly, so an action or a plugin that omits
        # remove_listener() on teardown stops growing this list. See
        # docs/memory-footprint-plan.md.
        self.observers = CallbackRegistry()
        # This holder's own dispatch lane. The observers of this event run in
        # order on a thread of their own, so an observer that blocks, as a
        # wedged pulsectl call does, stalls this event source's queue alone.
        # Every other holder keeps delivering. The holder owns the lane, so the
        # lane dies with it and needs no registry key. Two holders can share
        # one event_id.
        self._lane = event_dispatch.Lane(label=self.event_id)

    def add_listener(self, callback: Callable[..., Any]):
        if not self.observers.add(callback):
            # A functools.partial and other callable objects have no
            # __name__, and this warning must not break the connect.
            name = getattr(callback, "__name__", repr(callback))
            log.warning(f"Callback {name} is already subscribed to: {self.event_id}")

    def remove_listener(self, callback: Callable[..., Any]):
        self.observers.remove(callback)

    def trigger_event(self, *args, **kwargs):
        """Queue this holder's current observers onto its lane and return.

        A return does not mean the observers ran. They run after it, one at a
        time in registration order, on this holder's lane. An observer that
        blocks stalls this event source alone. The order against another
        holder's events is undefined. See event_dispatch.py.
        """
        # The contract prepends self.event_id as the observers' first
        # positional argument. AudioControl's on_pulse_device_change reads it
        # as args[0] and the pulsectl event as args[1]. Keep that order.
        try:
            self._lane.dispatch(self.observers.snapshot(), (self.event_id, *args), kwargs, label=self.event_id)
        except event_dispatch.DispatchShutdown:
            # on_quit stopped the dispatcher, and a plugin event source keeps
            # running until os._exit. AudioControl's pulse listener is a daemon
            # thread that loops on pulse.event_listen() and calls this from its
            # callback. A shutdown error out of this call kills that thread
            # with an uncaught RuntimeError on every quit that races an event,
            # and no caller can act on it. Any other RuntimeError still
            # propagates. See DispatchShutdown.
            log.debug(f"Event {self.event_id} triggered after dispatch shutdown; dropped")
