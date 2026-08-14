"""DeckController.close() must be idempotent under concurrent callers.

The _closing transition is a locked compare-and-set under _close_lock. A
one-shot read hook lets a second closer run inside the check-and-set window.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from fixtures import start_watchdog

WATCHDOG_SECONDS = 60


def hook_closing(controller):
    """Swap in a subclass whose _closing is a property over the instance dict.

    The property carries a one-shot read hook on a chosen thread.
    """
    base = type(controller)

    class Hooked(base):
        @property
        def _closing(self):
            value = self.__dict__.get("_closing_flag", False)
            if threading.current_thread() is self.__dict__.get("_closing_hook_thread"):
                hook = self.__dict__.get("_closing_read_hook")
                if hook is not None:
                    self.__dict__["_closing_read_hook"] = None  # fire once
                    hook()
            return value

        @_closing.setter
        def _closing(self, value):
            self.__dict__["_closing_flag"] = value

    controller.__class__ = Hooked
    controller.__dict__["_closing_flag"] = bool(controller.__dict__.get("_closing", False))
    return controller


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_close_once")

    controller = fixtures.make_headless_controller(serial="close-once-1")
    hook_closing(controller)

    stop_calls = []
    original_stop = controller.media_player.stop

    def counting_stop(timeout: float = 2.0):
        stop_calls.append(threading.current_thread().name)
        return original_stop(timeout=timeout)

    controller.media_player.stop = counting_stop

    second_done = threading.Event()

    def second_closer():
        controller.close(remove_media=True)
        second_done.set()

    def on_gate_read():
        # The first closer just read _closing as False and has not set it yet,
        # which is the check-and-set window. Let a second closer run here. With
        # the lock it blocks on _close_lock until the first transition
        # completes. Without it, the second closer runs the whole sweep now.
        t = threading.Thread(target=second_closer, name="closer-2", daemon=True)
        t.start()
        time.sleep(0.4)

    result = {}

    def first_closer():
        controller.__dict__["_closing_hook_thread"] = threading.current_thread()
        controller.__dict__["_closing_read_hook"] = on_gate_read
        controller.close(remove_media=True)
        result["done"] = True

    t1 = threading.Thread(target=first_closer, name="closer-1", daemon=True)
    t1.start()
    t1.join(timeout=30)
    assert result.get("done"), "first close() never completed (deadlock?)"
    assert second_done.wait(timeout=10), "second close() never completed"

    assert len(stop_calls) == 1, (
        f"the teardown sweep ran {len(stop_calls)} times (by {stop_calls}) -- "
        f"the _closing check-then-set let a concurrent close() through the "
        f"gate; it must run exactly once"
    )
    print("PASS: scenario_close_once")


if __name__ == "__main__":
    main()
