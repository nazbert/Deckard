"""
Scenario for issue #80 (plan §6, posted on the GitLab issue): the central
exception hooks in src/backend/log_hooks.py.

Covers, in one subprocess-isolated process (process-global hooks are exactly
why run_all.py's per-scenario interpreter matters):

  1. threading.excepthook -- a raising Thread target lands in a loguru sink
     with message, thread name AND traceback text;
  2. sys.excepthook -- captures a simulated PyErr_Print call, and passes
     KeyboardInterrupt through to the pre-install hook without logging;
  3. sys.unraisablehook -- a raising __del__ is logged;
  4. asyncio -- the handler wired by event_dispatch._get_loop logs both the
     exception and the message-only context forms; an exception escaping
     _dispatch_batch itself surfaces via the Future done-callback;
  5. idempotence -- double install neither re-wraps the hook nor double-logs;
  6. re-entrancy -- if the logging call itself raises, the fallback prints
     the original traceback to sys.__stderr__ and the process survives;
  7. faulthandler redirection -- a SIGQUIT in a child process appends a
     boot-marked all-thread dump to <dir>/faulthandler.log.

Out of harness scope (manual QA): the real PyGObject-callback -> excepthook
path under a live GTK loop, and the config_logger()/main() ordering.
"""
import fixtures  # must be first: isolates DATA_PATH before any src import

import gc
import io
import os
import signal
import subprocess
import sys
import threading
import time

from loguru import logger

from src.backend import log_hooks

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_exception_hooks")
    records: list[str] = []
    logger.add(lambda message: records.append(str(message)), level="TRACE")

    def joined() -> str:
        return "".join(records)

    # Install with a spy as the pre-existing hook so the KeyboardInterrupt
    # passthrough is observable.
    prev_calls: list[type] = []
    sys.excepthook = lambda t, v, tb: prev_calls.append(t)
    log_hooks.install_exception_hooks()

    # 1. threading.excepthook
    def boom_thread() -> None:
        raise ValueError("boom-thread")

    t = threading.Thread(target=boom_thread, name="boom-worker")
    t.start()
    t.join()
    assert "boom-thread" in joined(), "thread exception message must reach the sink"
    assert "boom-worker" in joined(), "the thread NAME is what makes these actionable"
    assert 'raise ValueError("boom-thread")' in joined(), (
        "the full traceback (source line), not just the message, must be logged"
    )

    # 2. sys.excepthook -- simulate PyErr_Print's call from an except block
    records.clear()
    try:
        raise TypeError("boom-main")
    except TypeError:
        sys.excepthook(*sys.exc_info())
    assert "boom-main" in joined() and "[main]" in joined()
    assert 'raise TypeError("boom-main")' in joined()

    records.clear()
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert prev_calls == [KeyboardInterrupt], "KeyboardInterrupt must delegate to the previous hook"
    assert not records, "KeyboardInterrupt must not be logged"

    # 3. sys.unraisablehook -- raising __del__ under GC
    records.clear()

    class BoomOnDel:
        def __del__(self):
            raise RuntimeError("boom-del")

    obj = BoomOnDel()
    del obj
    gc.collect()
    assert "boom-del" in joined() and "[unraisable]" in joined()

    # 4a. asyncio handler, via the REAL wiring in event_dispatch._get_loop
    from src.backend.PluginManager import event_dispatch

    records.clear()
    loop = event_dispatch._get_loop()
    loop.call_exception_handler({"message": "ctx", "exception": ValueError("boom-asyncio")})
    assert "boom-asyncio" in joined() and "[asyncio]" in joined()

    records.clear()
    loop.call_exception_handler({"message": "boom-asyncio-msgonly"})
    assert "boom-asyncio-msgonly" in joined(), "message-only contexts (no exception) must log too"

    # 4b. an exception escaping _dispatch_batch itself is pool-swallowed
    # (never reaches threading.excepthook) -- the Future done-callback must
    # surface it.
    records.clear()
    original_get_loop = event_dispatch._get_loop
    event_dispatch._get_loop = lambda: (_ for _ in ()).throw(RuntimeError("boom-batch"))
    try:
        event_dispatch.dispatch([lambda: None], (), {}, label="hooks-scenario")
        deadline = time.monotonic() + 5.0
        while "boom-batch" not in joined() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        event_dispatch._get_loop = original_get_loop
    assert "boom-batch" in joined(), "a batch-level failure must not vanish into the dropped Future"
    assert "dispatch batch failed" in joined()

    # 5. idempotence
    hook_after_first = sys.excepthook
    log_hooks.install_exception_hooks()
    assert sys.excepthook is hook_after_first, "second install must not re-wrap the hook"
    records.clear()

    def boom_once() -> None:
        raise ValueError("boom-once")

    t2 = threading.Thread(target=boom_once, name="boom-once-worker")
    t2.start()
    t2.join()
    assert sum("boom-once" in r for r in records) == 1, (
        f"double install must not double-log (got {sum('boom-once' in r for r in records)} records)"
    )

    # 6. re-entrancy: the logging call itself raising must fall back to
    # sys.__stderr__ with the ORIGINAL traceback, and never propagate.
    class RaisingLogger:
        def opt(self, **kwargs):
            raise RuntimeError("sink down")

    buf = io.StringIO()
    orig_log, orig_dunder_stderr = log_hooks._LOG, sys.__stderr__
    log_hooks._LOG = RaisingLogger()
    sys.__stderr__ = buf
    try:
        try:
            raise ValueError("boom-fallback")
        except ValueError:
            sys.excepthook(*sys.exc_info())
    finally:
        log_hooks._LOG = orig_log
        sys.__stderr__ = orig_dunder_stderr
    assert "boom-fallback" in buf.getvalue(), "fallback must print the original exception to __stderr__"

    # 7. faulthandler redirection: SIGQUIT in a child appends a dump to the
    # file; the child survives (register(), not a fatal signal default).
    fh_dir = os.path.join(fixtures.DATA_DIR, "logs")
    child_code = (
        "import os, signal, sys, time\n"
        f"sys.path.insert(0, {REPO_ROOT!r})\n"
        "from src.backend import log_hooks\n"
        "log_hooks.redirect_faulthandler(sys.argv[1])\n"
        "os.kill(os.getpid(), signal.SIGQUIT)\n"
        "time.sleep(0.3)\n"
        "print('child-survived')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child_code, fh_dir],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"child died: {proc.stderr}"
    assert "child-survived" in proc.stdout
    with open(os.path.join(fh_dir, "faulthandler.log")) as f:
        dump = f.read()
    assert "===== boot " in dump, "boot marker must separate appended dumps"
    assert "Thread" in dump or "Current thread" in dump, (
        f"SIGQUIT must produce an all-thread dump, got: {dump[:200]!r}"
    )

    print("PASS: scenario_exception_hooks")


if __name__ == "__main__":
    main()
