"""
Wiring scenario for issue #169: SIGTERM/SIGHUP must run App.on_quit's
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
  4. on_quit is idempotent: a second entry (Ctrl+C landing mid-teardown --
     Python signal handlers run between bytecodes on the main thread) is a
     no-op instead of re-destroying the window, re-triggering AppQuit and
     arming a second force_quit watchdog.
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
"""
import fixtures  # noqa: F401  (must be first: isolates the data dir)

import contextlib
import os
import signal
import sys
import threading

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


class QuitRecorder:
    """Stands in for App as the handler's `self`: records every on_quit
    invocation with the args the route passed. The three routes differ --
    (signum, frame) from signal.signal, () from a GLib unix-signal source,
    (action, param) from the Gio "quit" action -- which is why the real
    on_quit is declared as on_quit(self, *args)."""

    # The real wrapper the TERM/HUP sources are registered against, under
    # test unbound on this stub exactly like the other App methods here.
    _on_unix_signal = App._on_unix_signal

    def __init__(self):
        self.calls = []
        self.loop = None

    def on_quit(self, *args):
        self.calls.append(args)
        if self.loop is not None:
            self.loop.quit()
        # Mirrors the real on_quit's return value: None -- SOURCE_REMOVE if it
        # were the source callback itself, which is why it isn't:
        # _on_unix_signal wraps it and returns SOURCE_CONTINUE so the source
        # (and GLib's sigaction) survive the latch's early return.
        return None


def check_sigint_stays_a_python_handler() -> QuitRecorder:
    recorder = QuitRecorder()
    App.register_signal_handlers(recorder)

    assert signal.getsignal(signal.SIGINT) == recorder.on_quit, (
        f"SIGINT must stay wired through signal.signal to on_quit, got "
        f"{signal.getsignal(signal.SIGINT)!r}. A GLib unix-signal source is "
        f"invisible to signal.getsignal(), so moving SIGINT there would let "
        f"Gio.Application.run's register_sigint_fallback install its own "
        f"handler on top and route Ctrl+C to app.quit(), skipping on_quit's "
        f"whole teardown."
    )
    print("  PASS: SIGINT still routed to on_quit via signal.signal")
    return recorder


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
    # method now (App._destroy_main_window, #193), so bind the real one to
    # the stub rather than stubbing it out -- its missing-attribute branch is
    # exactly what this check exercises.
    stub = Obj(_quit_started=False)
    stub._destroy_main_window = lambda: App._destroy_main_window(stub)
    saved_dbus = app_mod.stop_dbus_service
    saved_sm = getattr(gl, "signal_manager", None)
    app_mod.stop_dbus_service = Recorder()
    gl.signal_manager = Obj(trigger_signal=_trigger_signal)
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
        gl.signal_manager = saved_sm

    assert reached, (
        "on_quit must get past the window teardown and on to the AppQuit "
        "signal when there is no main_win"
    )
    assert stub._quit_started is True, (
        "on_quit must latch _quit_started on the way in, not on the way out"
    )
    print("  PASS: on_quit survives a quit that precedes the main window")


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
    check_force_quit_terminates_backends()
    check_unix_signal_add_degrades_instead_of_raising()
    # Last: it replaces GLib's sigaction for TERM/HUP with a Python handler.
    check_degraded_fallback_still_reaches_on_quit(recorder)
    print("PASS: scenario_sigterm_quit")


if __name__ == "__main__":
    main()
