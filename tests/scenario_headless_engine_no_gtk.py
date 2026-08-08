"""
Scenario (issue #141 step (c)): the render engine's import closure must be
WIDGET-FREE.

A real DeckController -- page loads, key/dial input, media ticks, teardown --
must run without ever importing `gi.repository.Gtk/Adw/Gdk/GdkPixbuf/Pango`,
`src.windows.*`, `GtkHelper.GtkHelper` or `GtkHelper.GenerativeUI.*`.

Scope honesty: this proves the `make_headless_controller` ENGINE closure only.
The running app with plugins still loads Gtk/Adw at module scope
(PluginBase.py, ActionHolder.py -- deliberately untouched by #141), so what
this pins is the seam and headless testability, NOT an RSS reduction; that
lands with the daemon/client split.

"No gi at all" is deliberately NOT the target and never will be: dasbus (the
DBus API), the signal-manager idle trampoline, notify, single-instance, and
the lockscreen/window-grabber integrations all need GLib/Gio, and none of them
loads the widget stack. GLib and Gio are therefore asserted PRESENT below, so
the sweep can't pass vacuously.

Mechanism: a sys.meta_path finder installed at index 0 BEFORE anything else is
imported, so a violation fails at the offending import with that import's own
traceback -- far better diagnostics than a post-hoc sys.modules sweep. The
sweep runs anyway as a belt-and-braces check for anything that slipped in
before the tripwire.
"""
import sys

FORBIDDEN_EXACT = frozenset({
    "gi.repository.Gtk",
    "gi.repository.Adw",
    "gi.repository.Gdk",
    "gi.repository.GdkPixbuf",
    "gi.repository.Pango",
    "GtkHelper.GtkHelper",
    "src.windows",
    "GtkHelper.GenerativeUI",
})
FORBIDDEN_PREFIXES = ("src.windows.", "GtkHelper.GenerativeUI.")

# Expected residue: gi namespaces that carry no widget stack. Asserted PRESENT
# so a sweep over an engine that somehow imported nothing can't pass silently.
REQUIRED_PRESENT = ("gi.repository.GLib", "gi.repository.Gio")


def _is_forbidden(name: str) -> bool:
    return name in FORBIDDEN_EXACT or name.startswith(FORBIDDEN_PREFIXES)


class _GuiImportTripwire:
    """A meta-path finder that refuses widget-stack imports.

    gi's own DynamicImporter is a meta-path finder too, so `from gi.repository
    import Gtk` routes through here first -- inserting this at index 0 means it
    wins.
    """

    def find_spec(self, fullname, path=None, target=None):
        if _is_forbidden(fullname):
            raise ImportError(
                f"forbidden GUI import in the headless engine: {fullname}"
            )
        return None


_TRIPWIRE = _GuiImportTripwire()
sys.meta_path.insert(0, _TRIPWIRE)

# Anything below this line runs under the tripwire.
import fixtures  # noqa: E402  (import first: sets up the isolated data dir)
import globals as gl  # noqa: E402

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402

WATCHDOG_SECONDS = 60


def check_tripwire_is_armed() -> None:
    """The tripwire must actually fire -- otherwise everything below is
    vacuous. Uses a name nothing else imports so no real module is affected."""
    try:
        __import__("src.windows.ui_adapter")
    except ImportError as e:
        assert "forbidden GUI import" in str(e), (
            f"an unrelated ImportError masked the tripwire check: {e}"
        )
    else:
        raise AssertionError(
            "the meta-path tripwire did not fire for src.windows.ui_adapter -- "
            "every assertion in this scenario would be vacuous"
        )
    assert "src.windows.ui_adapter" not in sys.modules, (
        "the refused module was still registered in sys.modules"
    )
    print("PASS: the meta-path tripwire refuses widget-stack imports")


def check_engine_runs_headless() -> None:
    """Drive the real engine: two visually distinct pages, a page switch, key
    and dial input, media ticks, teardown."""
    import os

    red = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "engine_red.png"), color=(220, 20, 20))
    blue = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "engine_blue.png"), color=(20, 20, 220))
    page_a_path = fixtures.seed_page_with_background("EngineA", red)
    page_b_path = fixtures.seed_page_with_background("EngineB", blue)

    controller = fixtures.make_headless_controller(serial="headless-nogtk-1")
    try:
        deck = fixtures.raw_deck(controller)

        page_a = gl.page_manager.get_page(page_a_path, controller)
        controller.load_page(page_a, allow_reload=True)
        assert fixtures.wait_until(
            lambda: len(deck.ops_by_name("set_key_image")) > 0, timeout=10), (
            "page A never reached the device"
        )
        a_hashes = {e[4] for e in deck.ops_by_name("set_key_image")}

        # Input while page A is up: the real event callbacks, hold timers and
        # action dispatch all run.
        deck.fire_key_event(0, True)
        deck.fire_key_event(0, False)
        if controller.inputs.get(Input.Dial):
            from StreamDeck.Devices.StreamDeck import DialEventType

            deck.fire_dial_event(0, DialEventType.PUSH, True)
            deck.fire_dial_event(0, DialEventType.PUSH, False)

        before_switch = deck.current_seq()
        page_b = gl.page_manager.get_page(page_b_path, controller)
        controller.load_page(page_b, allow_reload=True)
        assert fixtures.wait_until(
            lambda: any(e[2] == "set_key_image" for e in deck.ops_after(before_switch)),
            timeout=10), "page B never reached the device"
        b_hashes = {e[4] for e in deck.ops_after(before_switch) if e[2] == "set_key_image"}

        assert a_hashes, "fixture sanity: page A produced no key writes"
        assert b_hashes, "fixture sanity: page B produced no key writes"
        assert b_hashes - a_hashes, (
            "the page switch produced no NEW key content -- the two pages are "
            "supposed to be visually distinct, so this run proved nothing"
        )

        # The engine still dirty-marks with no UI attached: the null port is
        # the whole reason this works headless.
        assert controller.ui_image_changes_while_hidden, (
            "no dirty markers accumulated -- the null port should refuse every "
            "push and the engine should mark instead"
        )
        print("PASS: real controller loaded two pages, took input, painted, headless")
    finally:
        fixtures.teardown(controller)


def check_no_widget_modules_loaded() -> None:
    """Belt and braces: catches anything imported before the tripwire went in
    (nothing should be, but the assertion is cheap and the failure mode it
    guards -- a silent widget import at the very top of the process -- is not
    otherwise observable)."""
    loaded = sorted(m for m in sys.modules if _is_forbidden(m))
    assert not loaded, f"widget-stack modules loaded in the engine closure: {loaded}"

    missing = [m for m in REQUIRED_PRESENT if m not in sys.modules]
    assert not missing, (
        f"expected gi residue {missing} was absent -- the sweep above may be "
        f"passing vacuously"
    )
    gi_loaded = sorted(m for m in sys.modules if m.startswith("gi.repository."))
    print(f"PASS: no widget-stack modules; gi residue is {gi_loaded}")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_headless_engine_no_gtk")

    check_tripwire_is_armed()
    check_engine_runs_headless()
    check_no_widget_modules_loaded()

    print("PASS: scenario_headless_engine_no_gtk")


if __name__ == "__main__":
    main()
