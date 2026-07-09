from loguru import logger as log

from src.backend.PluginManager import event_dispatch
from src.Signals.weak_callbacks import CallbackRegistry

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

    def add_listener(self, callback: callable):
        if not self.observers.add(callback):
            log.warning(f"Callback {callback.__name__} is already subscribed to: {self.event_id}")

    def remove_listener(self, callback: callable):
        self.observers.remove(callback)

    def trigger_event(self, *args, **kwargs):
        # Dispatch on the shared background thread instead of spinning up a
        # new asyncio event loop (+ default executor) on every call --
        # this is the hottest callback path in the app (AudioControl's
        # PulseEvent fires per PulseAudio event, bursts of tens/sec) and the
        # old per-call loop churned an epoll fd every time (bug 27). See
        # event_dispatch.py for why returning before observers finish is
        # safe for this call site.
        #
        # NOTE: the old implementation called
        # `self._run_event(self.event_id, *args, **kwargs)`, which silently
        # prepended `self.event_id` as the observers' first positional
        # argument (AudioControl's on_pulse_device_change reads it as
        # `args[0]` and the real pulsectl event as `args[1]`). Preserve that
        # contract here.
        event_dispatch.dispatch(self.observers.snapshot(), (self.event_id, *args), kwargs, label=self.event_id)
