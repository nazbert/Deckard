"""
Unit-tier scenario for the plugin event/callback layer (issues #33, #37,
#36): the pieces between an EventHolder/InputEvent firing and a plugin
callback actually running are deck-independent, so this exercises them
directly -- no FakeDeck, no controller.

Covers:
  (a) #33 -- a raising observer (sync AND `async def`) dispatched through
      event_dispatch produces a logged ERROR that carries the exception
      type, message and a traceback, not just the bare one-liner.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from loguru import logger as log

from fixtures import wait_until
from src.backend.PluginManager import event_dispatch


class _LogCapture:
    """Attaches a capturing loguru sink for the duration of a `with` block.
    Loguru hands text sinks the fully formatted message -- including the
    formatted traceback whenever the record carries exception info -- so
    asserting on the joined text is enough to prove a traceback was logged.
    """

    def __init__(self, level: str = "DEBUG"):
        self._level = level
        self.records: list[str] = []

    def __enter__(self):
        self._handle = log.add(lambda message: self.records.append(str(message)), level=self._level)
        return self

    def __exit__(self, *exc):
        log.remove(self._handle)
        return False

    def text(self) -> str:
        return "".join(self.records)


# ===================================================================== #
# (a) #33 -- dispatch logs the observer's traceback
# ===================================================================== #

def check_raising_sync_observer_logs_traceback():
    def exploding_observer(*args, **kwargs):
        raise RuntimeError("sync-boom-marker")

    with _LogCapture(level="ERROR") as capture:
        event_dispatch.dispatch([exploding_observer], ("evt",), {}, label="test::SyncEvent")
        assert wait_until(lambda: "could not be called" in capture.text(), timeout=5.0), (
            "dispatch never logged the failing sync observer"
        )
        # The batch runs on the shared dispatcher thread; the one-liner and
        # its exception block arrive as a single record, so once the marker
        # text is visible the assertion set below is race-free.
        text = capture.text()

    assert "exploding_observer" in text, text
    assert "test::SyncEvent" in text, text
    assert "RuntimeError" in text, f"exception type missing from log: {text!r}"
    assert "sync-boom-marker" in text, f"exception message missing from log: {text!r}"
    assert "Traceback" in text, f"no traceback in log -- #33 regressed: {text!r}"


def check_raising_async_observer_logs_traceback():
    # Every real EventHolder observer in the plugin ecosystem is an
    # `async def` -- this is the branch that mattered most for #33.
    async def exploding_coroutine_observer(*args, **kwargs):
        raise ValueError("async-boom-marker")

    with _LogCapture(level="ERROR") as capture:
        event_dispatch.dispatch([exploding_coroutine_observer], ("evt",), {}, label="test::AsyncEvent")
        assert wait_until(lambda: "could not be called" in capture.text(), timeout=5.0), (
            "dispatch never logged the failing async observer"
        )
        text = capture.text()

    assert "exploding_coroutine_observer" in text, text
    assert "ValueError" in text, f"exception type missing from log: {text!r}"
    assert "async-boom-marker" in text, f"exception message missing from log: {text!r}"
    assert "Traceback" in text, f"no traceback in log -- #33 regressed: {text!r}"


def check_raising_observer_does_not_stop_batch():
    # Exception isolation was already there -- make sure adding the
    # traceback didn't disturb it: the observer after the raising one still
    # runs.
    ran = []

    def exploding(*args, **kwargs):
        raise RuntimeError("first observer dies")

    def survivor(*args, **kwargs):
        ran.append(args)

    with _LogCapture(level="ERROR"):
        event_dispatch.dispatch([exploding, survivor], ("evt",), {}, label="test::Isolation")
        assert wait_until(lambda: len(ran) == 1, timeout=5.0), (
            "observer after a raising one never ran -- batch isolation broke"
        )


def main() -> None:
    check_raising_sync_observer_logs_traceback()
    check_raising_async_observer_logs_traceback()
    check_raising_observer_does_not_stop_batch()
    print("PASS: scenario_plugin_events")


if __name__ == "__main__":
    main()
