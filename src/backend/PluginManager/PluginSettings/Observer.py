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
        # CallbackRegistry (src/Signals/weak_callbacks.py, design doc bug
        # 3/27): bound-method observers are held weakly, so a subscriber
        # that never calls unsubscribe() on teardown doesn't keep this list
        # (and the objects it points at) growing forever.
        self.observers = CallbackRegistry()
        # This notifier's own dispatch lane: its subscribers are
        # serialized on a thread of their own, so a blocking subscriber
        # stalls only this asset stream, not plugin events app-wide. `label`
        # is what the wedge watchdog names the lane by.
        self._lane = event_dispatch.Lane(label=label)

    def subscribe(self, observer: Callable[..., Any]):
        self.observers.add(observer)

    def unsubscribe(self, observer: Callable[..., Any]):
        self.observers.remove(observer)

    def notify(self, *args, **kwargs):
        """Fire-and-forget: queues the current subscribers onto this
        notifier's dispatch lane and returns immediately.

        Returning does NOT mean the subscribers have run -- do not read it as
        "delivered". They run afterwards, sequentially and in subscription
        order, on this notifier's own lane (per-lane FIFO), so a subscriber
        that blocks stalls this asset stream only; no ordering is guaranteed
        relative to other notifiers' events. See event_dispatch.py.
        """
        # Previously: pulled/created an asyncio event loop per call (with a
        # bare `except:` around a call that could legitimately try to close
        # a *running* loop it does not own -- design doc bug 27) and ran
        # every observer through asyncio.gather/to_thread. Returning before
        # the observers run is not new either: a caller racing a *running*
        # loop already went down the `ensure_future` branch.
        try:
            self._lane.dispatch(self.observers.snapshot(), args, kwargs)
        except event_dispatch.DispatchShutdown:
            # Same fire-and-forget reasoning as EventHolder.trigger_event:
            # once on_quit has shut the dispatcher down, an asset mutation
            # racing teardown must not raise out of a notify() no caller
            # checks. Any other RuntimeError still propagates.
            log.debug("Asset notification after dispatch shutdown; dropped")
