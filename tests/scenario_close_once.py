"""
Scenario for issue #56 item 5: DeckController.close() must be idempotent
under CONCURRENT callers, not just sequential ones.

The `_closing` gate was an unlocked check-then-set: two teardown callers
racing it (USB unplug thread vs. app-quit) could both read False, both set
True, and both run the whole teardown sweep -- duplicate plugin on_removed
hooks, double device close. The fix makes the transition a locked
compare-and-set under a new _close_lock (sweep itself stays unlocked, since
it can block on plugin hooks).

Made deterministic with the same hook trick as scenario_touchscreen_slot_race:
the controller's class is swapped for a subclass whose `_closing` property
getter -- armed for exactly one read on the first closer's thread -- lets a
SECOND close() run in the check->set window before the first proceeds. On
the fixed code the second closer blocks on _close_lock inside that window,
so the sweep still runs exactly once (counted via media_player.stop, one
call per sweep). On the pre-fix code both callers pass the gate and the
count reaches 2.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from fixtures import start_watchdog

WATCHDOG_SECONDS = 60


def hook_closing(controller):
    """Swap in a subclass whose _closing is a property over
    __dict__['_closing_flag'], with a one-shot read hook on a chosen thread."""
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
        # First closer just read _closing (False) and has not yet set it:
        # the exact check->set window. Let a second closer run here. On the
        # fixed code it blocks on _close_lock until the first transition
        # completes; on the pre-fix code it runs the WHOLE sweep now.
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
        f"gate (issue #56 item 5); it must run exactly once"
    )
    print("PASS: scenario_close_once")


if __name__ == "__main__":
    main()
