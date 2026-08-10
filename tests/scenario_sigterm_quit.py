"""
Wiring scenario: SIGTERM/SIGHUP must run App.on_quit's
teardown instead of killing the process outright.

Plugin backends are spawned with start_new_session=True, so they lead their
own session/process group and no killpg aimed at the app's group ever reaches
them. The only thing that kills them is on_quit's terminate_all_backends().
With no TERM handler, a session-manager logout or a plain `kill -TERM` took
the default disposition -- instant death, teardown never ran, backends
orphaned.

A full-app scenario is impractical here: constructing App needs a live
Gdk.Display, decks and the whole main.py bootstrap, and this harness is
deliberately headless. So these checks drive the real methods UNBOUND on plain
stub objects (the scenario_appshell_lows idiom) and cover the previously
missing link -- signal -> on_quit. The backend-killing half of the chain is
already covered by scenario_plugin_backend_teardown.py; the end-to-end
(`kill -TERM` against the running app with a live backend) stays manual.

  1. register_signal_handlers keeps SIGINT on a *Python-level* handler.
     Migrating it to a GLib unix-signal source would be invisible to
     signal.getsignal(), so Gio.Application.run's register_sigint_fallback
     would see "default handler" and install its own over GLib's sigaction,
     routing Ctrl+C to app.quit() and bypassing on_quit entirely.
 1b. That handler only *queues* on_quit onto the main loop. A Python-level
     handler runs between bytecodes on the main thread and can interrupt any
     statement in the app, so running the teardown there would destroy the
     window and drive plugin quit hooks on top of an unrelated stack frame;
     the queued form lands in the same dispatch context the TERM/HUP sources
     already use. Checked both directly (handler returns without on_quit
     having run; the next main-loop dispatch runs it exactly once) and
     end-to-end with a real SIGINT raised at ourselves.
 1c. Deferring is only as good as the loop, so a Ctrl+C arriving while an
     earlier one has sat undispatched past SIGINT_ESCALATE_AFTER_S forces the
     quit from handler context. Otherwise a wedged loop swallows every signal
     -- TERM and HUP are loop sources too -- leaving SIGKILL, which orphans
     the backends and skips the force_quit watchdog. Gated on elapsed time
     rather than press count (a double-tap or key repeat on a merely busy app
     must not os._exit past the fan-out and close_all()), gated on the
     quit-started latch (a press during a running teardown must not cut it
     short), and it restores SIG_DFL first so a force_quit that itself wedges
     stays killable.
  2. A real SIGTERM raised at ourselves reaches on_quit exactly once via the
     GLib main loop (pre-fix this killed the interpreter -> non-zero exit),
     and it is still armed for a *second* delivery. A unix-signal source
     whose callback returns falsy is destroyed and takes GLib's sigaction
     with it (SIG_DFL is restored), so routing the source straight at on_quit
     -- which returns None the moment its re-entry latch trips -- would let a
     repeated `kill -TERM` disarm the handler and hand the next one to the
     default disposition, killing the app mid-teardown with the backends
     still running. Hence App._on_unix_signal returning SOURCE_CONTINUE.
  3. Same for SIGHUP (terminal close, a common way the app dies).
  4. on_quit is idempotent: a second entry (a repeated TERM/HUP dispatch, a
     queued Ctrl+C, the tray or the Gio "quit" action landing mid-teardown) is
     a no-op instead of re-destroying the window, re-triggering AppQuit and
     arming a second force_quit watchdog.
 4b. The AppQuit fan-out isolates its handlers: one raising quit hook (a
     third-party plugin) is logged and the fan-out continues, instead of
     aborting the teardown before close_all() and terminate_all_backends().
     Weak-stored (bound-method) and strong-stored observers alike.
  5. on_quit tolerates a quit that lands before on_activate built the window;
     the old unguarded self.main_win.destroy() raised AttributeError and
     aborted teardown *before* terminate_all_backends.
  6. force_quit terminates the backends before os._exit(1), so even a wedged
     teardown that the 6s watchdog cuts short doesn't orphan them.
  7. unix_signal_add() degrades (returns False) instead of raising when the
     install itself fails. It runs inside App.__init__, so an escaping
     exception there costs the whole startup.
  8. That degraded path -- GLib 2.80+ moved the Unix API into the separate
     GLibUnix-2.0 namespace, and a runtime shipping neither spelling gets a
     plain signal.signal handler -- still routes TERM/HUP to the teardown.
     Forced explicitly, since this machine's GLib does have the source
     (see the INFO lines the run prints).
  9. on_quit drains StoreCache's deferred index -- and does it
     only AFTER the force_quit watchdog is armed. This is the sole live
     drain in the real app: on_quit ends in os._exit(0), which skips the
     module's atexit hook, and the debounce timer is a daemon. It has to sit
     behind the watchdog because the flush is an atomic_write_json (two
     fsyncs, no timeout of their own): on a wedged filesystem, running it
     earlier hangs the quit with nothing armed to end the process.
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
    """Stands in for os._exit so a check can observe the exit instead of
    dying with the process. BaseException on purpose: an `except Exception`
    in the code under test must not be able to swallow it."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@contextlib.contextmanager
def no_real_exit():
    """Turns os._exit into a raise for the duration. src.app calls it through
    the same module object, so patching here covers on_quit and force_quit.
    Without this a check that accidentally ran a real teardown to completion
    would os._exit(0) and the scenario would report a false PASS."""
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
    it is past the section under test -- everything below it (close_all, the
    controller loop, the thread joins) needs a real app to stand on."""


class QuitRecorder:
    """Stands in for App as the handler's `self`: records every on_quit
    invocation with the args the route passed. The routes differ -- () from a
    GLib unix-signal source or the idle a Ctrl+C queues, (action, param) from
    the Gio "quit" action, and (signum, frame) when a runtime without
    unix-signal sources leaves TERM/HUP on signal.signal -- which is why the
    real on_quit is declared as on_quit(self, *args)."""

    # The real wrappers the signals are registered against, under test unbound
    # on this stub exactly like the other App methods here.
    _on_unix_signal = App._on_unix_signal
    _on_sigint = App._on_sigint

    def __init__(self):
        self.calls = []
        self.loop = None
        self.force_quits = 0
        # The two pieces of App state _on_sigint reads. This stub's on_quit
        # deliberately does NOT latch _quit_started the way the real one does
        # (it has to stay re-runnable across checks), so every check that
        # delivers a SIGINT clears _sigint_first_at itself -- otherwise a
        # later check's first press would count as a follow-up to an interrupt
        # stamped seconds earlier and take the escalation.
        self._quit_started = False
        self._sigint_first_at = None

    def force_quit(self):
        self.force_quits += 1

    def on_quit(self, *args):
        self.calls.append(args)
        if self.loop is not None:
            self.loop.quit()
        # Mirrors the real on_quit's return value: None -- SOURCE_REMOVE if it
        # were the source callback itself, which is why it isn't:
        # _on_unix_signal wraps it and returns SOURCE_CONTINUE so the source
        # (and GLib's sigaction) survive the latch's early return.
        return None


def pump_main_context(max_iterations: int = 25) -> None:
    """Dispatch pending sources on the default main context, bounded so a
    forever-rescheduling source can't hang the scenario."""
    ctx = GLib.MainContext.default()
    for _ in range(max_iterations):
        if not ctx.pending():
            break
        ctx.iteration(False)


def check_sigint_stays_a_python_handler() -> QuitRecorder:
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


def check_sigint_defers_the_teardown_to_the_main_loop(recorder: QuitRecorder) -> None:
    """The SIGINT handler must queue on_quit, not run it.

    A Python-level handler runs between bytecodes on the main thread, so it
    can interrupt any statement in the app -- calling the teardown from there
    destroys the window and drives plugin quit hooks on top of a stack frame
    that was doing something else. The handler therefore hands on_quit to the
    main loop, the same dispatch context the TERM/HUP unix-signal sources
    already run it in.
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


def check_sigint_escalates_only_on_a_wedged_loop(recorder: QuitRecorder) -> None:
    """A Ctrl+C left undispatched must force the quit -- but only that one.

    Deferring the teardown to the main loop means a Ctrl+C is only as good as
    that loop: on a wedged one the idle never dispatches, and since TERM/HUP
    are loop sources too, no signal short of SIGKILL could end the process --
    which orphans the plugin backends (own session, so no killpg reaches them)
    and skips the force_quit watchdog, armed only inside on_quit. Hence the
    escape hatch, and hence the two things that gate it:

      * elapsed time, not press count. A live app busy in Python for a moment
        (on_activate loads pages and resizes images on the main thread)
        answers late, not never -- and a double-tap is ~150ms apart, key
        repeat ~33ms. Escalating on those would os._exit(1) a healthy app
        with no AppQuit fan-out and no close_all(), leaving a deck open for
        the next startup to fail on.
      * the quit-started latch. Presses during a teardown that IS running
        must stay no-ops rather than cutting the ordered shutdown short.

    The escalation also hands SIGINT back to SIG_DFL on its way out: both the
    handler and force_quit log, log sinks take locks, and a wedge inside one
    would swallow the escalation itself. A further press must then kill the
    process outright rather than re-enter the same deadlock.
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

    # An immediate second press: a double-tap, or the first repeat of a held
    # key. The app may simply be busy; it has had no chance to dispatch yet.
    recorder._on_sigint(signal.SIGINT, None)
    assert recorder.force_quits == before_quits, (
        "a double-tapped Ctrl+C must NOT force the quit -- a busy-but-live app "
        "answers a moment later, and force_quit here skips the AppQuit fan-out "
        "and close_all(), leaving a deck open for the next startup"
    )

    # Same press, but the quit it follows has gone unanswered past the
    # threshold: that is a loop which is not dispatching at all.
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
    # Put the handler back for the checks that follow -- from here SIGINT
    # would otherwise kill this scenario outright.
    signal.signal(signal.SIGINT, recorder._on_sigint)
    assert len(recorder.calls) == before_calls, (
        f"the escalation must not run on_quit in signal-handler context, got "
        f"{recorder.calls[before_calls:]!r}"
    )

    # ... and once a teardown is in flight, no press escalates, however long
    # it has been waiting.
    recorder._quit_started = True
    recorder._sigint_first_at = time.monotonic() - 60
    recorder._on_sigint(signal.SIGINT, None)
    assert recorder.force_quits == before_quits + 1, (
        "a Ctrl+C during a running teardown must not force-quit on top of it "
        "-- that cuts the ordered shutdown short with os._exit(1)"
    )
    recorder._quit_started = False

    # Drain the idles the deferred presses queued so they can't land in a
    # later check.
    pump_main_context()
    recorder.calls[before_calls:] = []
    recorder._sigint_first_at = None
    print("  PASS: Ctrl+C escalates only on a loop that stopped dispatching")


def report_signal_path(signum: int, name: str) -> None:
    """Print which of the two mechanisms register_signal_handlers landed on.

    GLib's sigaction is invisible to signal.getsignal() -- that is the very
    reason SIGINT has to stay a Python-level handler (check 1) -- so a SIG_DFL
    reading here means the GLib unix-signal source is what is armed. Anything
    else means unix_signal_add() degraded to signal.signal on this runtime
    (pre-2.80 GLib without the old spelling, or no GLibUnix typelib): the
    checks below still hold, but they are covering the fallback rather than
    the source. Informational, not an assertion -- degrading is a supported
    outcome, silently mistaking one path for the other is not.
    """
    handler = signal.getsignal(signum)
    if handler == signal.SIG_DFL:
        print(f"  INFO: {name} armed via a GLib unix-signal source")
    else:
        print(f"  INFO: {name} DEGRADED to a Python-level handler ({handler!r}) "
              f"-- the GLib unix-signal source path is NOT covered here")


def check_unix_signal_callback_keeps_source_armed() -> None:
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
    # A SIGINT delivery has to look like a first press: this stub's on_quit
    # never latches _quit_started, so a stamp carried over from an earlier
    # check would age past the threshold and take the escalation instead of
    # the deferred path under test.
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


def check_unix_signal_add_degrades_instead_of_raising() -> None:
    """A symbol that resolves but blows up on call must still degrade.

    unix_signal_add() is called from App.__init__ via
    register_signal_handlers, so anything it lets escape aborts startup
    outright -- a much worse outcome than the Python-level handler it is
    supposed to fall back to. Resolving GLib.unix_signal_add /
    GLibUnix.signal_add is no guarantee the call works: a signum
    g_unix_signal_add refuses, a GLib built without UNIX signal support, or
    an argument mismatch between the two spellings all raise at call time.
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


def check_degraded_fallback_still_reaches_on_quit(recorder: QuitRecorder) -> None:
    """The no-GLib-unix-signal-source path must still run the teardown.

    Must run AFTER the GLib-source checks: signal.signal() overwrites GLib's
    sigaction for these signums, so once this has run the source path is no
    longer reachable in this process.
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
                # Pre-fix (no guard) the fall-through dies further down the
                # teardown -- no main_win, no gl.signal_manager in the
                # harness. The recorder below is the verdict either way.
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

    # No main_win: quit before on_activate. The window teardown is a real
    # method now (App._destroy_main_window), so bind the real one to
    # the stub rather than stubbing it out -- its missing-attribute branch is
    # exactly what this check exercises.
    # force_quit and the timer wheel are stubbed because the force_quit
    # watchdog is armed on the way to the AppQuit fan-out, ahead of the
    # sentinel this check stops at -- a real schedule() here would leave a live
    # 6s timer running for the rest of the scenario.
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
        self.flushes = []  # one entry per flush: was the watchdog armed yet?

    def flush_index(self) -> None:
        self.flushes.append(bool(self._watchdog.calls))


class _RaisingLoggers:
    """gl.loggers stand-in that cuts a driven on_quit short at the log-sink
    loop -- everything below it (close_all, the controller loop, the thread
    joins) needs a real app to stand on."""

    def values(self):
        raise _ProbeDone()


def drive_on_quit(signal_manager, store_cache=None, watchdog=None) -> Obj:
    """Run the real App.on_quit unbound on a stub, down to the log-sink loop.

    The idiom the checks above use, with the whole teardown environment
    on_quit touches before that cut stubbed out. Returns the stub, the
    Recorder standing in for timer_wheel.schedule (pass that Recorder in when
    another stub has to read it while the teardown is still running) and the
    one standing in for stop_boot_rescan -- the first step after the AppQuit
    fan-out, so it dates how far the teardown got past it.
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


def check_appquit_handlers_are_isolated_from_each_other() -> None:
    """A raising AppQuit handler must not deny its peers the notification.

    The fan-out is synchronous (the process os._exit()s moments later, so
    queued handlers would never run) and its observers are strangers to each
    other: a third-party plugin raising in its quit hook used to abort the
    whole trigger_signal loop, so every handler connected after it -- and, in
    the caller, everything from close_all() to terminate_all_backends() --
    was skipped. Driven through the real on_quit against a real SignalManager
    so both halves are covered: the isolation itself, and the quit path
    actually using the isolating call.

    Three failure shapes, because they leave a handler by different routes: a
    plain exception, a sys.exit() (SystemExit is a BaseException and unwinds
    an `except Exception` fan-out just as fatally), and a handler whose
    failure cannot even be NAMED -- an rpyc netref into a plugin backend
    raises EOFError on attribute access once its connection is gone, so
    describing it for the log line raises a second time from inside the error
    path.

    A bound method sits behind them on purpose. Every AppQuit observer in the
    app is one, and CallbackRegistry stores bound methods WEAKLY -- a
    different retrieval path through snapshot() than the plain functions
    around it, and the one the real observers take.
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
        """Nameable while its (simulated) connection is up, unnameable once
        its hook has run -- the shutdown ordering of a real netref, which is
        alive at connect_signal time and dead by the time the fan-out tries to
        report that it failed."""

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

    # Strongly held for the duration: the registry's reference to its method
    # is weak, so letting this go would legitimately drop the subscription.
    observer = _QuitObserver()
    # Fixture sanity: this really does take the weak path.
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


def check_quit_drains_the_store_cache_index() -> None:
    """on_quit must flush the deferred store index, behind the watchdog.

    Drives the real on_quit far enough to cover the store-cache drain, then
    cuts it short at the log-sink loop with a sentinel. Two failure modes are
    pinned:

      * no flush at all -- the deferred read-clock renewals of the last store
        browse are lost on every quit, because os._exit(0) skips the atexit
        hook and the debounce timer is a daemon. Nothing else in the process
        drains them, so deleting the block leaves every other test green.
      * flush before timer_wheel.schedule(6, force_quit) -- atomic_write_json
        fsyncs twice with no timeout, so a wedged filesystem parks the quit
        there forever with no watchdog behind it.
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

        # ... and a failing termination must not cost us the exit.
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
    # Line-buffered: several checks below fail by *dying from the signal*
    # (SIG_DFL restored), and run_all.py captures stdout through a pipe -- a
    # block-buffered scenario would report "exit 143, no output".
    sys.stdout.reconfigure(line_buffering=True)
    fixtures.start_watchdog(60, label="scenario_sigterm_quit")
    recorder = check_sigint_stays_a_python_handler()
    check_sigint_defers_the_teardown_to_the_main_loop(recorder)
    check_sigint_escalates_only_on_a_wedged_loop(recorder)
    check_signal_reaches_on_quit(recorder, signal.SIGINT, "SIGINT")
    report_signal_path(signal.SIGTERM, "SIGTERM")
    report_signal_path(signal.SIGHUP, "SIGHUP")
    check_unix_signal_callback_keeps_source_armed()
    # Each signal is delivered twice on purpose. The second delivery proves
    # end-to-end that the first dispatch left the source (and GLib's
    # sigaction) in place: if it did not, SIG_DFL is back and this kill takes
    # the interpreter down instead -- which run_all.py reports as a FAIL, so
    # the check cannot silently pass.
    for signum, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGHUP, "SIGHUP")):
        check_signal_reaches_on_quit(recorder, signum, name)
        check_signal_reaches_on_quit(recorder, signum, f"{name} (second delivery)")
    check_quit_is_idempotent()
    check_quit_tolerates_missing_main_win()
    check_appquit_handlers_are_isolated_from_each_other()
    check_quit_drains_the_store_cache_index()
    check_force_quit_terminates_backends()
    check_unix_signal_add_degrades_instead_of_raising()
    # Last: it replaces GLib's sigaction for TERM/HUP with a Python handler.
    check_degraded_fallback_still_reaches_on_quit(recorder)
    print("PASS: scenario_sigterm_quit")


if __name__ == "__main__":
    main()
