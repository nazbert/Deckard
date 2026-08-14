"""
Wiring scenario for SIGTERM and SIGHUP running App.on_quit's teardown.

Plugin backends spawn with start_new_session=True, so no killpg aimed at the
app's group reaches them and only terminate_all_backends does. The checks
drive the real methods unbound on stub objects, because constructing App
needs a live display and the whole main.py bootstrap.
"""
import fixtures  # noqa: F401  (must be first: isolates the data dir)

import contextlib
import os
import signal
import sys
import threading
import time

import globals as gl

from src.app import App
# src.app has already done the gi.require_version calls by now.
from gi.repository import GLib  # noqa: E402


class Obj:
    """Attribute bag."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _ForcedExit(BaseException):
    """Stands in for os._exit, so a check observes the exit instead of dying
    with the process. It is a BaseException, so an except Exception in the
    code under test cannot swallow it."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@contextlib.contextmanager
def no_real_exit():
    """Turns os._exit into a raise for the duration. src.app calls it through
    the same module object, so this patch covers on_quit and force_quit.
    Without it a check that ran a real teardown would exit and report a false
    PASS."""
    saved = os._exit
    main_thread = threading.main_thread()

    def _fake_exit(code):
        if threading.current_thread() is not main_thread:
            # fixtures' deadlock watchdog hard-exits from its own daemon
            # thread. Raising there would only kill that thread and leave the
            # hang it was meant to break in place, so let it through.
            saved(code)
        raise _ForcedExit(code)

    os._exit = _fake_exit
    try:
        yield
    finally:
        os._exit = saved


class _ReachedAppQuit(BaseException):
    """Sentinel raised from a stub gl.signal_manager to prove on_quit got
    past the main_win teardown line."""


class _ProbeDone(BaseException):
    """Sentinel raised from a stub gl.loggers to stop a driven on_quit once
    it is past the section under test. Everything below it needs a real app
    to stand on."""


class QuitRecorder:
    """Stands in for App as the handler's self, and records every on_quit
    call with the args its route passed.

    The routes differ. A GLib unix-signal source and a queued Ctrl+C pass no
    args, the Gio quit action passes (action, param), and a signal.signal
    fallback passes (signum, frame). Hence on_quit(self, *args).
    """

    # The real wrappers the signals are registered against, under test unbound
    # on this stub exactly like the other App methods here.
    _on_unix_signal = App._on_unix_signal
    _on_sigint = App._on_sigint

    def __init__(self):
        self.calls = []
        self.loop = None
        self.force_quits = 0
        # The two pieces of App state _on_sigint reads. This stub's on_quit
        # does not latch _quit_started, because it must stay re-runnable, so
        # every check that delivers a SIGINT clears _sigint_first_at itself.
        # Otherwise a later first press counts as a follow-up to an old stamp.
        self._quit_started = False
        self._sigint_first_at = None

    def force_quit(self):
        self.force_quits += 1

    def on_quit(self, *args):
        self.calls.append(args)
        if self.loop is not None:
            self.loop.quit()
        # Mirrors the real on_quit's return value of None, which GLib reads
        # as SOURCE_REMOVE. _on_unix_signal wraps it and returns
        # SOURCE_CONTINUE, so the source and GLib's sigaction survive the
        # latch's early return.
        return None


def pump_main_context(max_iterations: int = 25) -> None:
    """Dispatch pending sources on the default main context, bounded so a
    forever-rescheduling source cannot hang the scenario."""
    ctx = GLib.MainContext.default()
    for _ in range(max_iterations):
        if not ctx.pending():
            break
        ctx.iteration(False)


def check_sigint_stays_python_handler() -> QuitRecorder:
    recorder = QuitRecorder()
    App.register_signal_handlers(recorder)

    assert signal.getsignal(signal.SIGINT) == recorder._on_sigint, (
        f"SIGINT must stay wired through signal.signal, got "
        f"{signal.getsignal(signal.SIGINT)!r}. A GLib unix-signal source is "
        f"invisible to signal.getsignal(), so moving SIGINT there would let "
        f"Gio.Application.run's register_sigint_fallback install its own "
        f"handler on top and route Ctrl+C to app.quit(), skipping on_quit's "
        f"whole teardown."
    )
    print("  PASS: SIGINT still routed through signal.signal")
    return recorder


def check_sigint_defers_teardown(recorder: QuitRecorder) -> None:
    """The SIGINT handler must queue on_quit, not run it.

    A Python-level handler runs between bytecodes on the main thread, so it
    can interrupt any statement in the app. Running the teardown there would
    destroy the window on top of an unrelated stack frame, so the handler
    hands on_quit to the main loop instead.
    """
    recorder._sigint_first_at = None
    before = len(recorder.calls)
    recorder._on_sigint(signal.SIGINT, None)

    assert len(recorder.calls) == before, (
        f"the SIGINT handler ran the teardown in signal-handler context "
        f"instead of queueing it on the main loop (got "
        f"{recorder.calls[before:]!r})"
    )
    pump_main_context()
    fired = recorder.calls[before:]
    assert fired == [()], (
        f"the queued teardown must reach on_quit exactly once when the main "
        f"loop next dispatches, got {fired!r}"
    )
    print("  PASS: SIGINT queues the teardown onto the main loop")


def check_sigint_escalates_on_wedged_loop(recorder: QuitRecorder) -> None:
    """A Ctrl+C left undispatched must force the quit, and only that one.

    On a wedged loop the idle never dispatches, and TERM and HUP are loop
    sources too, so nothing short of SIGKILL ends the process. The escalation
    gates on elapsed time and on the quit-started latch, and it restores
    SIG_DFL first so a force_quit that itself wedges stays killable.
    """
    import src.app as app_mod

    recorder._sigint_first_at = None
    recorder._quit_started = False
    before_quits = recorder.force_quits
    before_calls = len(recorder.calls)

    recorder._on_sigint(signal.SIGINT, None)
    assert recorder.force_quits == before_quits, (
        "the first Ctrl+C must defer to the main loop, not force the quit"
    )
    assert recorder._sigint_first_at is not None, (
        "the first Ctrl+C must stamp when it asked for the quit -- without it "
        "the escalation has no elapsed time to judge and falls back to "
        "counting presses"
    )

    # An immediate second press, a double-tap or the first repeat of a held
    # key. The app may simply be busy and has had no chance to dispatch.
    recorder._on_sigint(signal.SIGINT, None)
    assert recorder.force_quits == before_quits, (
        "a double-tapped Ctrl+C must NOT force the quit -- a busy-but-live app "
        "answers a moment later, and force_quit here skips the AppQuit fan-out "
        "and close_all(), leaving a deck open for the next startup"
    )

    # The same press, but the quit it follows went unanswered past the
    # threshold. That is a loop which is not dispatching at all.
    recorder._sigint_first_at -= app_mod.SIGINT_ESCALATE_AFTER_S
    recorder._on_sigint(signal.SIGINT, None)
    assert recorder.force_quits == before_quits + 1, (
        f"a Ctrl+C arriving more than {app_mod.SIGINT_ESCALATE_AFTER_S}s after "
        f"an undispatched one must force the quit -- otherwise a wedged main "
        f"loop leaves no signal able to end the process and SIGKILL orphans "
        f"the plugin backends"
    )
    assert signal.getsignal(signal.SIGINT) == signal.SIG_DFL, (
        f"the escalation must hand SIGINT back to the default disposition "
        f"before force_quit (got {signal.getsignal(signal.SIGINT)!r}), so a "
        f"force_quit that wedges in a log sink can still be killed with a "
        f"further Ctrl+C instead of re-entering the same deadlock"
    )
    # Put the handler back for the checks that follow. From here a SIGINT
    # would otherwise kill this scenario outright.
    signal.signal(signal.SIGINT, recorder._on_sigint)
    assert len(recorder.calls) == before_calls, (
        f"the escalation must not run on_quit in signal-handler context, got "
        f"{recorder.calls[before_calls:]!r}"
    )

    # Once a teardown is in flight, no press escalates, however long it has
    # been waiting.
    recorder._quit_started = True
    recorder._sigint_first_at = time.monotonic() - 60
    recorder._on_sigint(signal.SIGINT, None)
    assert recorder.force_quits == before_quits + 1, (
        "a Ctrl+C during a running teardown must not force-quit on top of it "
        "-- that cuts the ordered shutdown short with os._exit(1)"
    )
    recorder._quit_started = False

    # Drain the idles the deferred presses queued, so none lands in a later
    # check.
    pump_main_context()
    recorder.calls[before_calls:] = []
    recorder._sigint_first_at = None
    print("  PASS: Ctrl+C escalates only on a loop that stopped dispatching")


def report_signal_path(signum: int, name: str) -> None:
    """Print which of the two mechanisms register_signal_handlers landed on.

    GLib's sigaction is invisible to signal.getsignal(), so a SIG_DFL reading
    means the GLib unix-signal source is armed. Anything else means
    unix_signal_add degraded to signal.signal on this runtime. This is
    informational, because degrading is a supported outcome.
    """
    handler = signal.getsignal(signum)
    if handler == signal.SIG_DFL:
        print(f"  INFO: {name} armed via a GLib unix-signal source")
    else:
        print(f"  INFO: {name} DEGRADED to a Python-level handler ({handler!r}) "
              f"-- the GLib unix-signal source path is NOT covered here")


def check_unix_signal_keeps_source_armed() -> None:
    probe = QuitRecorder()
    result = probe._on_unix_signal()

    assert probe.calls == [()], (
        f"the TERM/HUP source callback must invoke on_quit, got {probe.calls!r}"
    )
    assert result == GLib.SOURCE_CONTINUE, (
        f"the TERM/HUP source callback must return SOURCE_CONTINUE, got "
        f"{result!r}. A falsy return destroys the unix-signal source and GLib "
        f"restores SIG_DFL with it, so once on_quit's re-entry latch has "
        f"tripped (it then returns None) a repeated `kill -TERM` would disarm "
        f"the handler and let the next signal kill the process outright, "
        f"mid-teardown, with the plugin backends still running -- the exact "
        f"orphan this issue fixes."
    )
    print("  PASS: the TERM/HUP source callback keeps its source armed")


def check_signal_reaches_on_quit(recorder: QuitRecorder, signum: int, name: str) -> None:
    # A SIGINT delivery must look like a first press. This stub's on_quit
    # never latches _quit_started, so a stamp carried over from an earlier
    # check would age past the threshold and take the escalation.
    recorder._sigint_first_at = None
    loop = GLib.MainLoop()
    recorder.loop = loop
    before = len(recorder.calls)
    timed_out = []

    def _raise_at_self():
        os.kill(os.getpid(), signum)
        return GLib.SOURCE_REMOVE

    def _give_up():
        timed_out.append(True)
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(_raise_at_self)
    give_up_id = GLib.timeout_add_seconds(10, _give_up)
    loop.run()
    if not timed_out:
        GLib.source_remove(give_up_id)

    assert not timed_out, (
        f"{name} never reached on_quit -- the loop ran 10s after the signal "
        f"was raised. (With no handler at all the default disposition would "
        f"have killed this interpreter instead, so a source exists but never "
        f"dispatched.)"
    )
    fired = recorder.calls[before:]
    assert len(fired) == 1, (
        f"{name} must invoke on_quit exactly once, got {len(fired)}: {fired!r}"
    )
    print(f"  PASS: {name} reached on_quit with the GLib main loop running")


def check_unix_signal_add_degrades() -> None:
    """A symbol that resolves but blows up on call must still degrade.

    unix_signal_add runs from App.__init__ through register_signal_handlers,
    so anything it lets escape aborts startup outright. A refused signum, a
    GLib built without UNIX signal support, or an argument mismatch between
    the two spellings all raise at call time.
    """
    import src.app as app_mod

    class _BoomGLib:
        @staticmethod
        def unix_signal_add(*args):
            raise TypeError("simulated marshalling mismatch")

    saved_glib = app_mod.GLib
    app_mod.GLib = _BoomGLib
    try:
        installed = app_mod.unix_signal_add(0, signal.SIGTERM, lambda *a: None)
    finally:
        app_mod.GLib = saved_glib

    assert installed is False, (
        f"unix_signal_add must report failure rather than raise when the "
        f"install itself fails (got {installed!r}); an exception here "
        f"propagates out of App.__init__ and the app never starts"
    )
    print("  PASS: a failing unix-signal install degrades instead of raising")


def check_degraded_fallback_reaches_quit(recorder: QuitRecorder) -> None:
    """The path with no GLib unix-signal source must still run the teardown.

    It must run after the GLib-source checks, because signal.signal overwrites
    GLib's sigaction for these signums and the source path is then unreachable
    in this process.
    """
    import src.app as app_mod

    saved = app_mod.unix_signal_add
    app_mod.unix_signal_add = lambda *args, **kwargs: False
    try:
        App.register_signal_handlers(recorder)
    finally:
        app_mod.unix_signal_add = saved

    for signum, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGHUP, "SIGHUP")):
        assert signal.getsignal(signum) == recorder._on_unix_signal, (
            f"with no unix-signal source available, {name} must fall back to a "
            f"Python-level handler running the same teardown, got "
            f"{signal.getsignal(signum)!r}"
        )
        check_signal_reaches_on_quit(recorder, signum, f"{name} (degraded fallback)")


def check_quit_is_idempotent() -> None:
    import src.app as app_mod

    stub = Obj(_quit_started=True)
    dbus = Recorder()
    saved_dbus = app_mod.stop_dbus_service
    app_mod.stop_dbus_service = dbus
    try:
        with no_real_exit():
            try:
                App.on_quit(stub)
            except BaseException:
                # Without the guard the fall-through dies further down the
                # teardown, because the harness has no main_win and no
                # gl.signal_manager. The recorder below is the verdict.
                pass
    finally:
        app_mod.stop_dbus_service = saved_dbus

    assert dbus.calls == [], (
        "on_quit must return immediately when a teardown is already in "
        "flight -- it re-entered and started tearing down a second time. A "
        "Ctrl+C during a TERM-initiated quit does exactly this: Python "
        "signal handlers run between bytecodes on the main thread."
    )
    print("  PASS: a re-entrant on_quit is a no-op")


def check_quit_tolerates_missing_main_win() -> None:
    import src.app as app_mod

    def _trigger_signal(*args, **kwargs):
        raise _ReachedAppQuit()

    # No main_win, so this is a quit before on_activate. The real
    # App._destroy_main_window binds to the stub, because its
    # missing-attribute branch is what this check exercises. force_quit and
    # the timer wheel are stubs, or a real schedule() would leave a live 6s
    # timer running for the rest of the scenario.
    stub = Obj(_quit_started=False, force_quit=Recorder())
    stub._destroy_main_window = lambda: App._destroy_main_window(stub)
    saved_dbus = app_mod.stop_dbus_service
    saved_timer_wheel = app_mod.timer_wheel
    saved_sm = getattr(gl, "signal_manager", None)
    app_mod.stop_dbus_service = Recorder()
    app_mod.timer_wheel = Obj(schedule=Recorder())
    gl.signal_manager = Obj(trigger_signal_sync=_trigger_signal)
    reached = False
    try:
        with no_real_exit():
            try:
                App.on_quit(stub)
            except _ReachedAppQuit:
                reached = True
            except AttributeError as e:
                raise AssertionError(
                    f"on_quit must tolerate a quit landing before on_activate "
                    f"built the window (autostart + immediate logout, startup "
                    f"crash-loop kill) -- it raised {e!r} and so never reached "
                    f"terminate_all_backends()"
                )
    finally:
        app_mod.stop_dbus_service = saved_dbus
        app_mod.timer_wheel = saved_timer_wheel
        gl.signal_manager = saved_sm

    assert reached, (
        "on_quit must get past the window teardown and on to the AppQuit "
        "signal when there is no main_win"
    )
    assert stub._quit_started is True, (
        "on_quit must latch _quit_started on the way in, not on the way out"
    )
    print("  PASS: on_quit survives a quit that precedes the main window")


class _StoreCacheProbe:
    """Stands in for gl.store_backend.store_cache and notes, per flush, how
    far on_quit had got when it was called."""

    def __init__(self, watchdog: Recorder):
        self._watchdog = watchdog
        self.flushes = []  # per flush, whether the watchdog was armed yet

    def flush_index(self) -> None:
        self.flushes.append(bool(self._watchdog.calls))


class _RaisingLoggers:
    """gl.loggers stand-in that cuts a driven on_quit short at the log-sink
    loop. Everything below it needs a real app to stand on."""

    def values(self):
        raise _ProbeDone()


def drive_on_quit(signal_manager, store_cache=None, watchdog=None) -> Obj:
    """Run the real App.on_quit unbound on a stub, down to the log-sink loop.

    Everything on_quit touches before that cut is stubbed out. Returns the
    stub, the Recorder standing in for timer_wheel.schedule, and the one
    standing in for stop_boot_rescan, which dates how far the teardown got.
    """
    import src.app as app_mod
    from src.backend import ui_port

    watchdog = Recorder() if watchdog is None else watchdog
    stub = Obj(_quit_started=False, force_quit=lambda: None)
    stub._destroy_main_window = lambda: App._destroy_main_window(stub)

    saved = {
        "dbus": app_mod.stop_dbus_service,
        "timer_wheel": app_mod.timer_wheel,
        "port": ui_port.get(),
        "signal_manager": getattr(gl, "signal_manager", None),
        "deck_manager": getattr(gl, "deck_manager", None),
        "store_backend": getattr(gl, "store_backend", None),
        "loggers": gl.loggers,
        "threads_running": gl.threads_running,
    }
    stop_boot_rescan = Recorder()
    app_mod.stop_dbus_service = Recorder()
    app_mod.timer_wheel = Obj(schedule=watchdog)
    gl.signal_manager = signal_manager
    gl.deck_manager = Obj(stop_boot_rescan=stop_boot_rescan)
    gl.store_backend = None if store_cache is None else Obj(store_cache=store_cache)
    gl.loggers = _RaisingLoggers()
    reached = False
    try:
        with no_real_exit():
            try:
                App.on_quit(stub)
            except _ProbeDone:
                reached = True
    finally:
        app_mod.stop_dbus_service = saved["dbus"]
        app_mod.timer_wheel = saved["timer_wheel"]
        ui_port.install(saved["port"])
        gl.signal_manager = saved["signal_manager"]
        gl.deck_manager = saved["deck_manager"]
        gl.store_backend = saved["store_backend"]
        gl.loggers = saved["loggers"]
        gl.threads_running = saved["threads_running"]

    assert reached, (
        "the driven on_quit never reached the log-sink loop -- the assertions "
        "that follow cannot be trusted; something on the teardown path raised "
        "before the cut"
    )
    return Obj(stub=stub, watchdog=watchdog, stop_boot_rescan=stop_boot_rescan)


def check_appquit_handlers_isolated() -> None:
    """A raising AppQuit handler must not deny its peers the notification.

    The fan-out is synchronous, because the process exits moments later, and
    its observers are strangers to each other. Three failure shapes run here:
    a plain exception, a sys.exit, and a handler whose failure cannot be
    named, because an rpyc netref raises again from inside the error path.
    A bound method sits behind them, on the weak retrieval path.
    """
    import sys
    import weakref

    from src.Signals.Signals import AppQuit
    from src.Signals.SignalManager import SignalManager

    ran = []

    class _QuitObserver:
        """Owner of a weak-stored (bound-method) AppQuit observer."""

        def on_app_quit(self):
            ran.append("bound")

    class _NetrefLikeHandler:
        """Nameable while its simulated connection is up, unnameable once its
        hook has run. A real netref is alive at connect_signal time and dead
        when the fan-out reports that it failed."""

        connected = True

        def __getattr__(self, name):
            if type(self).connected:
                raise AttributeError(name)
            raise EOFError("connection closed")

        def __call__(self):
            ran.append("netref")
            type(self).connected = False
            raise RuntimeError("hook failed as its connection dropped")

    def first_handler():
        ran.append("first")

    def raising_handler():
        ran.append("raiser")
        raise RuntimeError("simulated plugin quit hook failure")

    def exiting_handler():
        ran.append("exiting")
        sys.exit("simulated plugin quit hook calling sys.exit()")

    def last_handler():
        ran.append("last")

    # Strongly held for the duration. The registry's reference to its method
    # is weak, so dropping this would drop the subscription.
    observer = _QuitObserver()
    # The observer really does take the weak path.
    weakref.WeakMethod(observer.on_app_quit)

    signal_manager = SignalManager()
    for handler in (first_handler, raising_handler, exiting_handler,
                    _NetrefLikeHandler(), observer.on_app_quit, last_handler):
        signal_manager.connect_signal(AppQuit, handler)

    result = drive_on_quit(signal_manager)

    assert ran == ["first", "raiser", "exiting", "netref", "bound", "last"], (
        f"every AppQuit handler must run whatever the one before it did -- "
        f"raise, sys.exit(), or fail unnameably -- in connect order and "
        f"whether stored weakly or strongly, got {ran!r}"
    )
    assert result.stop_boot_rescan.calls, (
        "the driven on_quit never got past the AppQuit fan-out -- a failing "
        "handler aborted the teardown instead of being contained (the watchdog "
        "is armed before the fan-out, so it cannot answer this)"
    )
    print("  PASS: a raising AppQuit handler does not deny its peers or the teardown")


def check_quit_drains_store_cache_index() -> None:
    """on_quit must flush the deferred store index, behind the watchdog.

    Without the flush, the deferred read-clock renewals of the last store
    browse are lost on every quit, because os._exit(0) skips the atexit hook
    and the debounce timer is a daemon. A flush before the watchdog is armed
    parks the quit forever on a wedged filesystem.
    """
    watchdog = Recorder()
    probe = _StoreCacheProbe(watchdog)
    result = drive_on_quit(Obj(trigger_signal_sync=Recorder()),
                           store_cache=probe, watchdog=watchdog)
    stub = result.stub

    assert probe.flushes, (
        "on_quit must call store_cache.flush_index(): it is the only live "
        "drain of the deferred index in the real app (os._exit(0) skips the "
        "atexit hook, the debounce timer is a daemon), so without it every "
        "quit inside the debounce window loses the last browse's last-use "
        "clock renewals"
    )
    assert probe.flushes == [True], (
        f"the index flush must run AFTER timer_wheel.schedule(6, force_quit) "
        f"arms the watchdog -- it is an atomic_write_json (two fsyncs, no "
        f"timeout of their own), so on a wedged filesystem an earlier flush "
        f"hangs the quit with nothing left to end the process. Watchdog-armed "
        f"per flush: {probe.flushes!r}"
    )
    assert watchdog.calls and watchdog.calls[0][0][1] is stub.force_quit, (
        f"the watchdog this check asserts against must be the force_quit one, "
        f"got {watchdog.calls!r}"
    )
    print("  PASS: on_quit drains the store cache index, behind the watchdog")


def check_force_quit_terminates_backends() -> None:
    saved_pm = getattr(gl, "plugin_manager", None)
    terminate = Recorder()
    gl.plugin_manager = Obj(terminate_all_backends=terminate)
    code = None
    try:
        with no_real_exit():
            try:
                App.force_quit(Obj())
            except _ForcedExit as e:
                code = e.code

        assert terminate.calls == [((), {})], (
            f"force_quit must terminate the plugin backends before os._exit "
            f"-- otherwise a teardown wedged past the 6s watchdog orphans "
            f"them exactly like an unhandled SIGTERM did (got "
            f"{terminate.calls!r})"
        )
        assert code == 1, f"force_quit must still exit(1), got {code!r}"

        # A failing termination must not cost the exit.
        def _wedged():
            raise RuntimeError("backend registry is wedged")

        gl.plugin_manager = Obj(terminate_all_backends=_wedged)
        code = None
        with no_real_exit():
            try:
                App.force_quit(Obj())
            except _ForcedExit as e:
                code = e.code
        assert code == 1, (
            f"a failing terminate_all_backends must be swallowed so "
            f"force_quit still exits, got {code!r}"
        )
    finally:
        gl.plugin_manager = saved_pm
    print("  PASS: force_quit terminates backends, then exits regardless")


def main() -> None:
    # Line-buffered output. Several checks below fail by dying from the
    # signal, and run_all.py captures stdout through a pipe, so a
    # block-buffered scenario would report an exit code with no output.
    sys.stdout.reconfigure(line_buffering=True)
    fixtures.start_watchdog(60, label="scenario_sigterm_quit")
    recorder = check_sigint_stays_python_handler()
    check_sigint_defers_teardown(recorder)
    check_sigint_escalates_on_wedged_loop(recorder)
    check_signal_reaches_on_quit(recorder, signal.SIGINT, "SIGINT")
    report_signal_path(signal.SIGTERM, "SIGTERM")
    report_signal_path(signal.SIGHUP, "SIGHUP")
    check_unix_signal_keeps_source_armed()
    # Each signal is delivered twice. The second delivery proves the first
    # dispatch left the source and GLib's sigaction in place. Otherwise
    # SIG_DFL is back and this kill takes the interpreter down, which
    # run_all.py reports as a failure.
    for signum, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGHUP, "SIGHUP")):
        check_signal_reaches_on_quit(recorder, signum, name)
        check_signal_reaches_on_quit(recorder, signum, f"{name} (second delivery)")
    check_quit_is_idempotent()
    check_quit_tolerates_missing_main_win()
    check_appquit_handlers_isolated()
    check_quit_drains_store_cache_index()
    check_force_quit_terminates_backends()
    check_unix_signal_add_degrades()
    # Last, because it replaces GLib's sigaction for TERM and HUP.
    check_degraded_fallback_reaches_quit(recorder)
    print("PASS: scenario_sigterm_quit")


if __name__ == "__main__":
    main()
