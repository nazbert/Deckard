"""The import closure of the render engine must be widget-free.

A real DeckController must run page loads, input, media ticks and teardown
without importing Gtk, Adw, Gdk, GdkPixbuf, Pango, src.windows or GtkHelper.
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

# Expected residue, gi namespaces that carry no widget stack. Asserted present,
# so a sweep over an engine that imported nothing cannot pass silently.
REQUIRED_PRESENT = ("gi.repository.GLib", "gi.repository.Gio")

# The complete set the engine closure may pull in. Engine code names GLib and
# Gio through SignalManager, api, notify and HelperMethods.open_web. GObject
# and GModule come with gi itself, and Xdp is the flatpak probe of DeckManager,
# which is libportal and absent on Mac, hence a subset check. Anything outside
# this set is a new dependency that has to be argued for.
ALLOWED_GI_RESIDUE = frozenset({
    "gi.repository.GLib",
    "gi.repository.GModule",
    "gi.repository.GObject",
    "gi.repository.Gio",
    "gi.repository.Xdp",
    # PyGObject 3.56 and later split the Unix-only parts of GLib and Gio into
    # their own namespaces and load them transitively with Gio. Still no widget
    # stack, so the same argument as GLib and Gio themselves holds.
    "gi.repository.GLibUnix",
    "gi.repository.GioUnix",
})


def _is_forbidden(name: str) -> bool:
    return name in FORBIDDEN_EXACT or name.startswith(FORBIDDEN_PREFIXES)


class _GuiImportTripwire:
    """A meta-path finder that refuses widget-stack imports.

    The DynamicImporter of gi is a meta-path finder too, so a gi.repository
    import routes through here first once this sits at index 0.
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

# The harness runs a StubDeckManager, because the real one starts a USBMonitor
# and an Xdp portal probe, so without this import the real module and its
# closure would go unproven. Importing the module alone starts nothing.
import src.backend.DeckManagement.DeckManager  # noqa: E402,F401

WATCHDOG_SECONDS = 60


def check_tripwire_is_armed() -> None:
    """The tripwire must fire, or everything below is vacuous.

    It uses a name nothing else imports, so no real module is affected.
    """
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
    """Drive the real engine end to end under the tripwire.

    Two visually distinct pages, a page switch, key and dial input, media ticks
    and teardown. Page B is an action page, so ActionCore construction,
    initialize_actions, on_ready and the local GenerativeUI import are covered.
    """
    import os

    red = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "engine_red.png"), color=(220, 20, 20))
    green = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "engine_green.png"), color=(20, 220, 20))
    page_a_path = fixtures.seed_page_with_background("EngineA", red)

    # Before the controller. load_default_page() runs at the end of
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

        # Input while page A is up, so the real event callbacks, hold timers
        # and action dispatch all run.
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

        # The action ran. Construction, initialize_actions and on_ready all sit
        # inside the engine closure and all ran under the tripwire.
        assert fixtures.wait_until(lambda: _any_action_ready(page_b), timeout=10), (
            "page B's action never reached on_ready -- ActionCore init / "
            "initialize_actions / on_ready would be outside this scenario's "
            "widget-free proof"
        )

        # Its paint reached the device, which proves the action ran for real
        # rather than merely being constructed.
        deck.fire_key_event(0, True)
        deck.fire_key_event(0, False)

        assert a_hashes, "fixture sanity: page A produced no key writes"
        assert b_hashes, "fixture sanity: page B produced no key writes"
        assert b_hashes - a_hashes, (
            "the page switch produced no NEW key content -- the two pages are "
            "supposed to be visually distinct, so this run proved nothing"
        )

        # The engine still dirty-marks with no UI attached, which is what the
        # null port buys.
        assert controller.ui_image_changes_while_hidden, (
            "no dirty markers accumulated -- the null port should refuse every "
            "push and the engine should mark instead"
        )
        print("PASS: real controller loaded a background page and an ACTION page, "
              "took input, painted, headless")
    finally:
        fixtures.teardown(controller)


def check_no_widget_modules_loaded() -> None:
    """Catch anything imported before the tripwire went in.

    Nothing should be, and a silent widget import at the very top of the
    process is not otherwise observable.
    """
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
