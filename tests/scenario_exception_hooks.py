"""
Scenario for the central
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
     boot-marked all-thread dump to <dir>/faulthandler.log;
  8. SC_NO_ERROR_HOOKS -- in a flagged child process, install
     leaves the three interpreter hooks stock, redirect_faulthandler writes
     no file, and the redaction patcher still rides along (the flag is a
     hook switch, not a privacy switch);
  9. per-site rate limiting -- a 100x storm from one site yields
     one record plus a suppressed-count summary on the next one; two
     distinct sites never mask each other; non-repeating failures are
     untouched (no throttling, no summary noise); the state dict stays
     bounded under a broad storm, suppression SURVIVES past that bound
     (eviction by last hit, not by window start), a pending count whose entry
     is evicted is reported rather than discarded, and the TERMINAL shape of
     sys.excepthook bypasses suppression while its callback shape does not;
 10. atexit flush -- a storm that stops still reports its last
     window's count, in a child process that exits after storming;
 11. lock re-entrancy -- a raising __del__ collected INSIDE the
     guarded region re-enters the guard on the same thread and must not
     deadlock, which is the only thing pinning the RLock.

Out of harness scope (manual QA): the real PyGObject-callback -> excepthook
path under a live GTK loop, and the config_logger()/main() ordering.
"""
import fixtures  # must be first: isolates DATA_PATH before any src import

import gc
import io
import os
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

    # 8. SC_NO_ERROR_HOOKS=1: the flag is read at import, and the
    # hooks are process-global, so this has to be a child process.
    flagged_dir = os.path.join(fixtures.DATA_DIR, "logs_flagged")
    flagged_code = (
        "import os, sys, threading\n"
        f"sys.path.insert(0, {REPO_ROOT!r})\n"
        "from loguru import logger\n"
        "from src.backend import log_hooks\n"
        "from src.backend.log_redaction import redact_record\n"
        "assert log_hooks._HOOKS_DISABLED, 'the flag must be read at import time'\n"
        "log_hooks.install_exception_hooks()\n"
        "assert sys.excepthook is sys.__excepthook__, 'sys.excepthook must stay stock'\n"
        "assert threading.excepthook is threading.__excepthook__, "
        "'threading.excepthook must stay stock'\n"
        "assert sys.unraisablehook is sys.__unraisablehook__, "
        "'sys.unraisablehook must stay stock'\n"
        "assert not log_hooks._installed, 'a flagged install must not latch _installed'\n"
        "assert logger._core.patcher is redact_record, "
        "'the flag is a hook switch, not a privacy switch: redaction must survive it'\n"
        "lines = []\n"
        "logger.add(lambda m: lines.append(str(m)), level='TRACE')\n"
        "log_hooks.redirect_faulthandler(sys.argv[1])\n"
        "log_hooks.redirect_faulthandler(sys.argv[1])\n"
        "assert log_hooks._fault_file is None, 'no dump file may be opened under the flag'\n"
        "assert not os.path.exists(sys.argv[1]), 'the log dir must not even be created'\n"
        "assert sum('SC_NO_ERROR_HOOKS=1' in ln for ln in lines) == 1, "
        "'a flagged run must self-identify in the log exactly once'\n"
        "class FakeLoop:\n"
        "    seen = []\n"
        "    def default_exception_handler(self, context): FakeLoop.seen.append(context)\n"
        "log_hooks.asyncio_exception_handler(FakeLoop(), {'message': 'flagged'})\n"
        "assert FakeLoop.seen == [{'message': 'flagged'}], "
        "'the asyncio surface must fall back to asyncio own handler'\n"
        "print('flagged-ok')\n"
    )
    flagged = subprocess.run(
        [sys.executable, "-c", flagged_code, flagged_dir],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "SC_NO_ERROR_HOOKS": "1"},
    )
    assert flagged.returncode == 0, f"flagged child failed: {flagged.stderr}"
    assert "flagged-ok" in flagged.stdout

    # ...and ONLY "1" disables them. "false"/"off"/"0" is what an operator
    # writes to keep the safety net ON; a truthiness test would read those as
    # "on" and silently drop the whole safety net on that run.
    off_code = (
        "import sys\n"
        f"sys.path.insert(0, {REPO_ROOT!r})\n"
        "from src.backend import log_hooks\n"
        "assert not log_hooks._HOOKS_DISABLED, 'only SC_NO_ERROR_HOOKS=1 may disable the hooks'\n"
        "log_hooks.install_exception_hooks()\n"
        "assert sys.excepthook is not sys.__excepthook__, 'the hooks must be installed'\n"
        "print('hooks-on')\n"
    )
    for value in ("0", "false", "off", "no", ""):
        off = subprocess.run(
            [sys.executable, "-c", off_code],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "SC_NO_ERROR_HOOKS": value},
        )
        assert off.returncode == 0, (
            f"SC_NO_ERROR_HOOKS={value!r} must NOT disable the hooks: {off.stderr}"
        )
        assert "hooks-on" in off.stdout

    # 9. per-site rate limiting. Every leg above fires each site
    # exactly once, which is why they are unaffected by the guard.
    original_window = log_hooks.RATE_LIMIT_WINDOW_S
    log_hooks._rate_state.clear()
    try:
        # 9a. 100 identical thread exceptions -> ONE record. Fired under the
        # real 5s window (the whole burst takes well under it), so this is
        # the production configuration, not a test-only one.
        records.clear()

        def storm() -> None:
            raise ValueError("boom-storm")  # one fixed site, fired 100x

        for _ in range(100):
            st = threading.Thread(target=storm, name="storm-worker")
            st.start()
            st.join()
        assert sum("boom-storm" in r for r in records) == 1, (
            f"a 100x storm from one site must log once, got "
            f"{sum('boom-storm' in r for r in records)} records"
        )

        # ...and the suppressed count rides on that site's NEXT record.
        # Shrinking the window (read per call) expires the open one without
        # a sleep.
        records.clear()
        log_hooks.RATE_LIMIT_WINDOW_S = 0.001
        time.sleep(0.01)
        st = threading.Thread(target=storm, name="storm-worker")
        st.start()
        st.join()
        assert sum("boom-storm" in r for r in records) == 1
        assert "99 further failures at" in joined(), (
            f"the next record must carry the suppressed count, got: {joined()[:300]!r}"
        )
        assert "since the last record" in joined(), (
            "the summary must not claim a window it cannot know (the gap "
            "between two records from one site is unbounded)"
        )
        assert "scenario_exception_hooks.py" in joined() and "[ValueError]" in joined(), (
            "the summary must name the site it is summarizing"
        )

        # 9b. two distinct sites, same exception type, interleaved: each gets
        # its own budget -- a storm on one must never silence the other.
        log_hooks.RATE_LIMIT_WINDOW_S = original_window
        log_hooks._rate_state.clear()
        records.clear()

        def site_a() -> None:
            raise ValueError("boom-site-a")

        def site_b() -> None:
            raise ValueError("boom-site-b")

        for target in (site_a, site_b, site_a, site_b, site_a):
            st = threading.Thread(target=target, name="two-sites")
            st.start()
            st.join()
        assert sum("boom-site-a" in r for r in records) == 1
        assert sum("boom-site-b" in r for r in records) == 1, (
            "a repeating site must not consume another site's budget"
        )

        # 9c. non-repeating failures are unchanged: every distinct site logs,
        # with no summary noise. Includes the sys.excepthook surface, proving
        # the guard is inherited from _log_exc rather than wired per hook.
        log_hooks._rate_state.clear()
        records.clear()

        def uniq_one() -> None:
            raise ValueError("uniq-1")

        def uniq_two() -> None:
            raise ValueError("uniq-2")

        def uniq_three() -> None:
            raise RuntimeError("uniq-3")

        for target in (uniq_one, uniq_two, uniq_three):
            st = threading.Thread(target=target, name=target.__name__)
            st.start()
            st.join()
        try:
            raise KeyError("uniq-4")
        except KeyError:
            sys.excepthook(*sys.exc_info())
        for tag in ("uniq-1", "uniq-2", "uniq-3", "uniq-4"):
            assert sum(tag in r for r in records) == 1, f"{tag} must log exactly once"
        assert "suppressed" not in joined(), (
            "non-repeating failures must not gain suppression noise"
        )

        # ...and the main-thread surface throttles too (same line, 3 hits).
        records.clear()
        for _ in range(3):
            try:
                raise TypeError("boom-main-storm")
            except TypeError:
                sys.excepthook(*sys.exc_info())
        assert sum("boom-main-storm" in r for r in records) == 1, (
            "sys.excepthook must inherit the same per-site guard"
        )

        # 9d. a BROAD storm (many distinct sites) must not leak: the dict is
        # pruned, never unbounded.
        log_hooks._rate_state.clear()
        for i in range(4 * log_hooks._RATE_LIMIT_MAX_KEYS):
            log_hooks._rate_limit(("SyntheticError", (f"/fake/mod_{i}.py", i)))
        assert len(log_hooks._rate_state) <= log_hooks._RATE_LIMIT_MAX_KEYS, (
            f"rate-limit state must stay bounded, got {len(log_hooks._rate_state)} keys"
        )

        # 9e. ...and SUPPRESSION must survive past the cap, which the bound in
        # 9d does not pin. 100 hot sites storming while 300 one-shot sites
        # push the dict over the cap: evicting by window start (refreshed only
        # on an allowed record) makes the hot sites look oldest and re-admits
        # every one of them on its next hit, collapsing the guard to ~0%
        # protection exactly when it matters. Eviction by LAST HIT keeps them.
        log_hooks._rate_state.clear()
        records.clear()
        hot = [("HotError", (f"/fake/hot_{i}.py", i)) for i in range(100)]
        cold = [("ColdError", (f"/fake/cold_{i}.py", i)) for i in range(800)]
        hot_allowed = cold_allowed = 0
        cold_iter = iter(cold)
        for _round in range(40):
            for key in hot:
                if not log_hooks._rate_limit(key)[0]:
                    hot_allowed += 1
            for _ in range(len(cold) // 40):
                if not log_hooks._rate_limit(next(cold_iter))[0]:
                    cold_allowed += 1
        assert cold_allowed == len(cold), "every distinct one-shot site must log"
        assert hot_allowed <= len(hot) * 1.1, (
            f"a hot site must log about once, not once per eviction: "
            f"{hot_allowed} records for {len(hot)} sites x 40 hits "
            f"({len(hot) * 40} occurrences)"
        )

        # 9f. an evicted entry's pending count is REPORTED, not discarded --
        # the guard may make a flood quiet, never invisible.
        log_hooks._rate_state.clear()
        records.clear()
        quiet = ("QuietError", ("/fake/quiet_site.py", 7))
        for _ in range(6):
            log_hooks._rate_limit(quiet)  # 1 allowed + 5 suppressed
        log_hooks.RATE_LIMIT_WINDOW_S = 0.01
        time.sleep(0.02)  # the quiet site is now idle, and first out
        for i in range(log_hooks._RATE_LIMIT_MAX_KEYS + 2):
            log_hooks._rate_limit(("FloodError", (f"/fake/flood_{i}.py", i)))
        assert quiet not in log_hooks._rate_state, (
            "an idle site must be evicted before an active one"
        )
        assert "5 further failures at /fake/quiet_site.py:7" in joined(), (
            f"an evicted pending count must be reported, got: {joined()[-400:]!r}"
        )
        log_hooks.RATE_LIMIT_WINDOW_S = original_window

        # 9g. the TERMINAL shape of sys.excepthook is never throttled: a fatal
        # exception must not die inside a window some hot callback opened. The
        # callback shape of the SAME hook (and the same site) stays throttled,
        # because PyGObject routes every uncaught GTK/GLib callback exception
        # through sys.excepthook -- exempting the whole surface would reopen
        # the 20-30Hz storm vector this guard exists for.
        log_hooks._rate_state.clear()
        records.clear()

        def shared_raise() -> None:
            raise SystemError("boom-shared")  # one site, reached two ways

        for _ in range(4):  # callback shape: outermost frame is main()
            try:
                shared_raise()
            except SystemError:
                sys.excepthook(*sys.exc_info())
        assert sum("boom-shared" in r for r in records) == 1, (
            "the callback shape must stay rate-limited"
        )

        # <module>-framed code in a __main__ namespace is the shape the
        # interpreter hands to excepthook when an exception unwinds the
        # program.
        terminal_ns = {"__name__": "__main__", "sys": sys, "shared_raise": shared_raise}
        terminal_code = compile(
            "try:\n"
            "    shared_raise()\n"
            "except SystemError:\n"
            "    sys.excepthook(*sys.exc_info())\n",
            "fake_main.py", "exec",
        )
        for _ in range(3):
            exec(terminal_code, terminal_ns)
        assert sum("boom-shared" in r for r in records) == 4, (
            f"every terminal invocation must be logged, got "
            f"{sum('boom-shared' in r for r in records)} records"
        )
        assert "3 further failures at" in joined(), (
            "the terminal record must carry what the window swallowed -- "
            "there is no next record to carry it"
        )
    finally:
        log_hooks.RATE_LIMIT_WINDOW_S = original_window
        log_hooks._rate_state.clear()

    # 10. a storm that simply STOPS must not take its last window's failures
    # to the grave: atexit flushes every pending count. Needs a child process
    # -- the flush fires at interpreter shutdown, and loguru's default sink
    # puts it on stderr.
    atexit_code = (
        "import sys, threading\n"
        f"sys.path.insert(0, {REPO_ROOT!r})\n"
        "from src.backend import log_hooks\n"
        "log_hooks.install_exception_hooks()\n"
        "def boom():\n"
        "    raise ValueError('boom-atexit')\n"
        "for _ in range(5):\n"
        "    t = threading.Thread(target=boom, name='atexit-storm')\n"
        "    t.start(); t.join()\n"
        "print('storm-done')\n"
    )
    at_exit = subprocess.run(
        [sys.executable, "-c", atexit_code],
        capture_output=True, text=True, timeout=60,
    )
    assert at_exit.returncode == 0, f"atexit child failed: {at_exit.stderr}"
    assert "storm-done" in at_exit.stdout
    assert "4 further failures at" in at_exit.stderr, (
        f"the trailing count must be flushed at exit, got: {at_exit.stderr[-400:]!r}"
    )
    assert "process exiting" in at_exit.stderr

    # 11. the guard's lock must be RE-ENTRANT. GC can fire on an allocation
    # inside the guarded region, and a raising __del__ collected there
    # re-enters _log_exc on the SAME thread -- with a plain Lock that is a
    # self-deadlock inside the crash handler, and no other leg notices (a
    # Lock swap passes the entire rest of the suite). Driven on a worker with
    # a bounded join, so a regression fails loudly instead of hanging.
    #
    # Two assertions, because the acquire timeout that keeps a wedged guard
    # from hanging a hook ALSO softens a Lock swap from a deadlock into a
    # 0.5s stall: the wait pins "no deadlock", and the state check pins that
    # the re-entrant call actually did its work under the lock rather than
    # bailing out on the timeout.
    records.clear()
    log_hooks._rate_state.clear()

    class BoomOnDelReentrant:
        def __del__(self):
            raise RuntimeError("boom-reentrant-del")

    reentered = threading.Event()

    def hold_and_collect() -> None:
        with log_hooks._rate_lock:
            obj = BoomOnDelReentrant()
            del obj  # refcount hits zero HERE, inside the guarded region
            gc.collect()
        reentered.set()

    worker = threading.Thread(target=hold_and_collect, name="reentrancy", daemon=True)
    worker.start()
    assert reentered.wait(10.0), (
        "a re-entrant hook deadlocked on the rate-limit lock: it must stay an "
        "RLock -- a raising __del__ collected inside the guarded region "
        "re-enters _log_exc on the same thread"
    )
    assert "boom-reentrant-del" in joined(), (
        "the re-entrant unraisable must still be logged, not just survive"
    )
    assert [k for k in log_hooks._rate_state if k[0] == "RuntimeError"], (
        "the re-entrant call must have completed its work under the lock: an "
        "empty guard state means it bailed on the acquire timeout, i.e. the "
        "lock is no longer re-entrant"
    )

    print("PASS: scenario_exception_hooks")


if __name__ == "__main__":
    main()
