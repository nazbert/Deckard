"""
Author: G4PLS
Year: 2024
"""

from collections.abc import Callable
from typing import Any

from loguru import logger as log

from src.backend.PluginManager import event_dispatch
from src.Signals.weak_callbacks import CallbackRegistry

class Observer:
    def __init__(self, label: str | None = None):
        # A CallbackRegistry (src/Signals/weak_callbacks.py) holds a
        # bound-method observer weakly. A subscriber that never calls
        # unsubscribe() on teardown therefore stops growing this list and the
        # set of objects it points at.
        self.observers = CallbackRegistry()
        # This notifier's own dispatch lane. Its subscribers run in order on a
        # thread of their own, so a blocking subscriber stalls this asset
        # stream and no other. The wedge watchdog names the lane by label.
        self._lane = event_dispatch.Lane(label=label)

    def subscribe(self, observer: Callable[..., Any]):
        self.observers.add(observer)

    def unsubscribe(self, observer: Callable[..., Any]):
        self.observers.remove(observer)

    def notify(self, *args, **kwargs):
        """Queue the current subscribers onto the lane and return.

        A return does not mean the subscribers ran. They run after it, one at
        a time in subscription order, on this notifier's lane. A blocking
        subscriber stalls this asset stream alone. The order against another
        notifier's events is undefined. See event_dispatch.py.
        """
        try:
            self._lane.dispatch(self.observers.snapshot(), args, kwargs)
        except event_dispatch.DispatchShutdown:
            # For the reason EventHolder.trigger_event gives. After on_quit
            # shuts the dispatcher down, an asset mutation that races the
            # teardown must not raise out of notify(), which no caller checks.
            # Any other RuntimeError still propagates.
            log.debug("Asset notification after dispatch shutdown; dropped")
