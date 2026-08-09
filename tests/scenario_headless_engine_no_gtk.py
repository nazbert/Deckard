"""
Scenario: the render engine's import closure must be
WIDGET-FREE.

A real DeckController -- page loads (background AND action pages, so
ActionCore construction / initialize_actions / on_ready are covered too),
key/dial input, media ticks, teardown -- must run without ever importing
`gi.repository.Gtk/Adw/Gdk/GdkPixbuf/Pango`, `src.windows.*`,
`GtkHelper.GtkHelper` or `GtkHelper.GenerativeUI.*`. `DeckManager` is
imported here too: the harness substitutes a stub for it, so without an
explicit import its module-level closure would go unproven.

Scope honesty: this proves the `make_headless_controller` ENGINE closure only.
The running app with plugins still loads Gtk/Adw at module scope
(PluginBase.py, ActionHolder.py -- deliberately untouched), so what
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

# The COMPLETE set the engine closure is allowed to pull in. GLib/Gio are the
# ones engine code names (SignalManager, api, notify, HelperMethods.open_web);
# GObject/GModule come with gi itself; Xdp is DeckManager's flatpak probe
# (libportal, not a widget toolkit -- and absent on Mac, hence a subset check
# rather than equality). Anything OUTSIDE this set is a new dependency that
# has to be argued for, which is the point of pinning it.
ALLOWED_GI_RESIDUE = frozenset({
    "gi.repository.GLib",
    "gi.repository.GModule",
    "gi.repository.GObject",
    "gi.repository.Gio",
    "gi.repository.Xdp",
    # PyGObject >= 3.56 splits the Unix-only portions of GLib/Gio into their
    # own namespaces and loads them transitively with Gio. Still no
    # widget stack — same argument as GLib/Gio themselves.
    "gi.repository.GLibUnix",
    "gi.repository.GioUnix",
})


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

# The harness runs a StubDeckManager (the real one starts a USBMonitor and an
# Xdp portal probe), so the real module would otherwise never be imported at
# all and its closure -- also cleaned of gl.app.main_win reads --
# would go unproven. Importing the module alone starts nothing.
import src.backend.DeckManagement.DeckManager  # noqa: E402,F401

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


def _any_action_ready(page) -> bool:
    for by_ident in page.action_objects.values():
        for by_state in by_ident.values():
            for by_index in by_state.values():
                for action in by_index.values():
                    if getattr(action, "on_ready_called", False):
                        return True
    return False


def check_engine_runs_headless() -> None:
    """Drive the real engine: two visually distinct pages (the second carrying
    a real ActionCore subclass), a page switch, key and dial input, media
    ticks, teardown.

    Page B is deliberately an ACTION page: ActionCore construction,
    Page.initialize_actions and on_ready are a large slice of the engine
    closure -- including ActionCore's function-local
    `from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI` -- and a
    background-only page never reaches any of it, leaving it outside the
    tripwire's proof.
    """
    import os

    red = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "engine_red.png"), color=(220, 20, 20))
    green = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "engine_green.png"), color=(20, 220, 20))
    page_a_path = fixtures.seed_page_with_background("EngineA", red)

    # Before the controller: load_default_page() runs at the end of
    # DeckController.__init__ and would hit an unset plugin_manager.
    fixtures.install_stub_plugin_manager(fixtures.make_latch_action_class(), green)

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

        key_ident = controller.inputs[Input.Key][0].identifier.json_identifier
        page_b_path = fixtures.seed_action_page("EngineB", key_ident)

        before_switch = deck.current_seq()
        page_b = gl.page_manager.get_page(page_b_path, controller)
        controller.load_page(page_b, allow_reload=True)
        assert fixtures.wait_until(
            lambda: any(e[2] == "set_key_image" for e in deck.ops_after(before_switch)),
            timeout=10), "page B never reached the device"
        b_hashes = {e[4] for e in deck.ops_after(before_switch) if e[2] == "set_key_image"}

        # The action actually ran: construction + initialize_actions +
        # on_ready, all inside the engine closure, all under the tripwire.
        assert fixtures.wait_until(lambda: _any_action_ready(page_b), timeout=10), (
            "page B's action never reached on_ready -- ActionCore init / "
            "initialize_actions / on_ready would be outside this scenario's "
            "widget-free proof"
        )

        # And its paint reached the device, which is what proves the action
        # ran for real rather than merely being constructed.
        deck.fire_key_event(0, True)
        deck.fire_key_event(0, False)

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
        print("PASS: real controller loaded a background page and an ACTION page, "
              "took input, painted, headless")
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
    unexpected = sorted(set(gi_loaded) - ALLOWED_GI_RESIDUE)
    assert not unexpected, (
        f"the engine closure pulled in unlisted gi namespaces: {unexpected}. "
        "The FORBIDDEN_* lists only name the widget stack; this check is what "
        "makes 'the residue is GLib/GModule/GObject/Gio/Xdp' literally true."
    )
    print(f"PASS: no widget-stack modules; gi residue is {gi_loaded}")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_headless_engine_no_gtk")

    check_tripwire_is_armed()
    check_engine_runs_headless()
    check_no_widget_modules_loaded()

    print("PASS: scenario_headless_engine_no_gtk")


if __name__ == "__main__":
    main()
