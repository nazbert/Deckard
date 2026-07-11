"""
Author: G4PLS
Year: 2024
"""

from src.backend.PluginManager import event_dispatch
from src.Signals.weak_callbacks import CallbackRegistry

class Observer:
    def __init__(self):
        # CallbackRegistry (src/Signals/weak_callbacks.py, design doc bug
        # 3/27): bound-method observers are held weakly, so a subscriber
        # that never calls unsubscribe() on teardown doesn't keep this list
        # (and the objects it points at) growing forever.
        self.observers = CallbackRegistry()

    def subscribe(self, observer: callable):
        self.observers.add(observer)

    def unsubscribe(self, observer: callable):
        self.observers.remove(observer)

    def notify(self, *args, **kwargs):
        # Previously: pulled/created an asyncio event loop per call (with a
        # bare `except:` around a call that could legitimately try to close
        # a *running* loop it does not own -- design doc bug 27) and ran
        # every observer through asyncio.gather/to_thread. Dispatch is now
        # queued onto the same shared single-thread dispatcher EventHolder
        # uses (event_dispatch.py): one persistent loop for the process's
        # lifetime instead of one per notify() call, each observer isolated
        # in its own try/except. notify() returns before observers
        # necessarily run, same as it effectively already did for a caller
        # racing a *running* loop via the `ensure_future` branch above.
        event_dispatch.dispatch(self.observers.snapshot(), args, kwargs)
