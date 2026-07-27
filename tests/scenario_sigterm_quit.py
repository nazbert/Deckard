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
     GLib main loop (pre-fix this killed the interpreter -> non-zero exit).
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
"""
import fixtures  # noqa: F401  (must be first: isolates the data dir)

import contextlib
import os
import signal

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

    def _fake_exit(code):
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

    def __init__(self):
        self.calls = []
        self.loop = None

    def on_quit(self, *args):
        self.calls.append(args)
        if self.loop is not None:
            self.loop.quit()
        # Mirrors the real on_quit's return value: None. As a GLib source
        # callback that means SOURCE_REMOVE -- moot in production (on_quit
        # ends in os._exit and never returns). Here it means GLib drops the
        # source and restores SIG_DFL for that signum once its last source
        # goes, so each signal below is raised exactly once on purpose: a
        # second one would kill this interpreter.
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
    print(f"  PASS: {name} dispatched to on_quit through the GLib main loop")


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

    stub = Obj(_quit_started=False)  # no main_win: quit before on_activate
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
    fixtures.start_watchdog(60, label="scenario_sigterm_quit")
    recorder = check_sigint_stays_a_python_handler()
    check_signal_reaches_on_quit(recorder, signal.SIGTERM, "SIGTERM")
    check_signal_reaches_on_quit(recorder, signal.SIGHUP, "SIGHUP")
    check_quit_is_idempotent()
    check_quit_tolerates_missing_main_win()
    check_force_quit_terminates_backends()
    print("PASS: scenario_sigterm_quit")


if __name__ == "__main__":
    main()
