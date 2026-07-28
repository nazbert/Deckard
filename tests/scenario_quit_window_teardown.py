"""App._destroy_main_window must not abort on an unrealized window (#193).

GTK 4.22 segfaults on the dispose path of a window that was never realized.
In background mode (-b) on_activate builds main_win but never presents it, so
`main_win.destroy()` in on_quit killed the process *before*
terminate_all_backends() -- orphaning every plugin backend on every quit.

Red-check: with the guard removed, check 2 does not fail, it takes the whole
interpreter down (SIGSEGV), which run_all.py reports as a failed scenario.
That is the point -- a Python-level try/except cannot catch a native abort,
so the only honest assertion is "the process is still here afterwards".

Needs a display: GTK windows cannot be constructed headless. Skips cleanly
(exit 0, SKIP in the output) when no display is available, since the harness
has no CI lane with one.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import fixtures  # noqa: F401  (isolated data dir + watchdog; must precede app imports)

fixtures.start_watchdog(60, "quit_window_teardown")

if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
    print("SKIP: no display; GTK window construction needs one")
    raise SystemExit(0)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from src.app import App  # noqa: E402


class _Stub:
    """Carries just the attribute the real unbound method reads."""

    def __init__(self, main_win):
        self.main_win = main_win


def check_1_missing_window_is_a_noop():
    class _NoWin:
        pass

    App._destroy_main_window(_NoWin())
    print("PASS 1: no main_win attribute -> no-op")


def _run_in_app(body, app_id):
    """Drive `body(app)` from inside a running GtkApplication.

    The conditions matter: a bare Gtk.ApplicationWindow() with no application
    and no main loop does NOT reproduce #193 (verified -- an earlier version
    of this scenario passed with the guard removed, i.e. it was a false
    green). The abort needs a window bound to a running GtkApplication, which
    is what on_activate actually builds.
    """
    app = Adw.Application(application_id=app_id)
    result = {}

    def on_activate(_a):
        body(app, result)
        app.quit()

    app.connect("activate", on_activate)
    app.run([])
    return result


def check_2_unrealized_window_survives():
    """The regression itself. Pre-fix this SIGSEGVs the whole interpreter,
    which run_all.py reports as a failed scenario -- there is no Python-level
    exception to assert on, so surviving to the print IS the assertion."""
    for cls, label in ((Gtk.ApplicationWindow, "Gtk"), (Adw.ApplicationWindow, "Adw")):
        def body(app, result, cls=cls):
            win = cls(application=app)  # built exactly as on_activate does
            assert not win.get_realized(), "precondition: never presented"
            App._destroy_main_window(_Stub(win))
            result["survived"] = True

        res = _run_in_app(body, f"dev.deckard.scenario.qwt.unrealized{label}")
        assert res.get("survived"), f"{label}: teardown did not complete"
        print(f"PASS 2{'' if label == 'Gtk' else 'b'}: unrealized "
              f"{label}.ApplicationWindow -> survived teardown")


def check_3_realized_window_is_still_destroyed():
    """The guard must not turn the normal (windowed) path into a no-op."""
    app = Adw.Application(application_id="dev.deckard.scenario.quitteardown")
    result = {}

    def on_activate(_a):
        win = Adw.ApplicationWindow(application=app)
        win.connect("destroy", lambda *_: result.__setitem__("destroyed", True))
        win.present()
        result["realized_before"] = win.get_realized()
        App._destroy_main_window(_Stub(win))
        app.quit()

    app.connect("activate", on_activate)
    app.run([])

    assert result.get("realized_before"), "presented window should have realized"
    assert result.get("destroyed"), \
        "a realized window must still be destroyed, not skipped"
    print("PASS 3: realized window -> still destroyed")


def main():
    check_1_missing_window_is_a_noop()
    check_2_unrealized_window_survives()
    check_3_realized_window_is_still_destroyed()
    print("ALL PASS: scenario_quit_window_teardown")


if __name__ == "__main__":
    main()
