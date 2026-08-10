"""
Pins the typed gl accessors (src/backend/services.py) -- the named reads that
stand in for `gl.lm.get`, `gl.app`, `gl.app.main_win`, `gl.settings_manager`
and `gl.page_manager`.

Nothing here is clever, and that is the point: an accessor is a one-line
forward, so the way it breaks is by forwarding slightly less than the raw
expression did -- an argument dropped, a slot cached, a None branch quietly
"improved" into a crash. Every guard below is aimed at one of those.

Guards:
  1. tr() forwards to gl.lm.get in both shapes. With no fallback and with one,
     the result matches the raw call byte for byte AND matches the value the
     locale data says it should be -- equality with gl.lm.get alone would be
     near-vacuous, since tr calls it. The fallback guard is the sharp one: a
     dropped `fallback` argument still passes an equality-only check on every
     ordinary key.
  2. tr() before the locale manager exists raises a RuntimeError that names the
     slot and the construction step, instead of an AttributeError on None.
     This is the accessors' one intentional behaviour change, and it is on a
     path that already crashed.
  3. main_window() is None for BOTH ways the window can be missing: gl.app is
     None, and gl.app exists without a `main_win` attribute bound yet (the raw
     `gl.app.main_win` raises AttributeError there -- there is no class-body
     declaration, on_activate binds it). It returns the window once bound, and
     it keeps returning it after teardown, because App._destroy_main_window
     destroys the widget and leaves the attribute bound -- the documented
     asymmetry, pinned here so the docstring cannot drift back to claiming a
     post-quit None.
  4. require_main_window() raises a named error for both of those absences and
     hands back the concrete window otherwise -- the conversion the raw
     dereferences want, since none of them has a None branch to keep.
  5. app() is the honest raw read, identity-preserving, None included;
     require_app() returns the same object post-publish and raises a
     boot-phase RuntimeError before it.
  6. settings(), app_settings() and deck_settings() pass straight through to
     the settings manager -- the same object, the same shared dict, no copy of
     its own.
  7. page_manager() stays honestly Optional; require_page_manager() is the
     opt-in that turns absence into a named error.
  8. Every accessor re-reads its gl slot per call, so rebinding a slot (which
     is exactly what the harness and future tests do) is honoured.
  9. The module's runtime imports are `globals` plus stdlib -- the claim that
     lets any layer, engine closure included, import it without a cycle.

No GTK: the accessors know nothing about the toolkit, and neither does this.
"""
import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import ast
import os
import sys

import globals as gl  # noqa: E402

from locales.LocaleManager import LocaleManager  # noqa: E402
from src.backend import services  # noqa: E402
from src.backend.SettingsManager import AppSettings  # noqa: E402

WATCHDOG_SECONDS = 30

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(_REPO_ROOT, "src", "backend", "services.py")

# Two locales, one row per shape the reader below exercises. The delimiter and
# the header layout are the real file's (locales/locales.csv).
LOCALE_CSV = (
    "key;de_DE;en_US\n"
    "go-back;Zurück;Go back\n"
    "amp-key;A & B;A & B\n"
    "german-only;Nur Deutsch;\n"
)


def build_locale_manager() -> LocaleManager:
    """A real LocaleManager over a two-locale CSV written into the harness's
    temp data dir -- the point is to compare tr() against the production
    get(), so a stub with a recording get() would prove nothing."""
    path = os.path.join(gl.DATA_PATH, "services_accessors_locales.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(LOCALE_CSV)
    lm = LocaleManager(csv_path=path)
    lm.set_language("de_DE")
    lm.set_fallback_language("en_US")
    # No CSV row can produce a key whose fallback-locale value is None -- the
    # reader stores strings -- so the `fallback` parameter is unreachable
    # through real locale data. It is still part of get()'s signature and of
    # every tr() call site that passes one, so the only way to keep the
    # threading honest is to build the entry the reader cannot.
    lm.locale_data["fallback-only"] = {"de_DE": None, "en_US": None}
    return lm


class FakeApp:
    """Stands in for gl.app. Deliberately WITHOUT `main_win`: that attribute is
    bound by App.on_activate and by nothing earlier, which is the case guard 3
    is about."""


class FakeWindow:
    pass


class FakePageManager:
    pass


def check_tr_forwards_both_shapes() -> None:
    lm = build_locale_manager()
    gl.lm = lm

    # A hit in the active language.
    assert services.tr("go-back") == "Zurück", services.tr("go-back")
    assert services.tr("go-back") == lm.get("go-back")

    # A miss: get() resolves to the key itself, and does so BEFORE the
    # fallback parameter is consulted -- so a fallback must not change it.
    assert services.tr("no-such-key") == "no-such-key"
    assert services.tr("no-such-key", "FALLBACK") == "no-such-key"
    assert services.tr("no-such-key", "FALLBACK") == lm.get("no-such-key", "FALLBACK")

    # An empty value in the active language falls through to the fallback
    # locale, which is empty too here: the empty string comes back, not the key.
    assert services.tr("german-only") == "Nur Deutsch"
    lm.set_language("en_US")
    assert services.tr("german-only") == "" == lm.get("german-only")
    lm.set_language("de_DE")

    # The fallback parameter, on the only path that reaches it. Dropping the
    # argument inside tr() is invisible to every assertion above and shows up
    # right here.
    assert services.tr("fallback-only", "FALLBACK") == "FALLBACK"
    assert services.tr("fallback-only", "FALLBACK") == lm.get("fallback-only", "FALLBACK")
    assert services.tr("fallback-only") == "fallback-only"
    assert services.tr("fallback-only") == lm.get("fallback-only")

    # get() escapes for GTK markup; tr() must not add or remove a layer.
    assert services.tr("amp-key") == "A &amp; B" == lm.get("amp-key")

    # Per-call slot read: a rebound gl.lm is the one that answers.
    other = build_locale_manager()
    other.locale_data["go-back"] = {"de_DE": "Anders", "en_US": "Other"}
    gl.lm = other
    assert services.tr("go-back") == "Anders", "tr() cached gl.lm instead of reading it"
    gl.lm = lm

    print("PASS: tr() forwards to gl.lm.get in both shapes, fallback included")


def check_tr_before_the_locale_manager_exists() -> None:
    gl.lm = None  # the real pre-boot value
    try:
        services.tr("go-back")
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("tr() before gl.lm exists must raise RuntimeError")
    assert "gl.lm" in message, message
    assert "create_global_objects" in message, message
    assert "go-back" in message, "the message must name the key that was asked for"

    print("PASS: tr() pre-boot raises a named RuntimeError, not AttributeError on None")


def check_app_and_require_app() -> None:
    gl.app = None
    assert services.app() is None

    try:
        services.require_app()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("require_app() must raise while gl.app is None")
    assert "gl.app" in message, message

    running = FakeApp()
    gl.app = running  # stand-in for the real App
    assert services.app() is running, "app() must be the raw read, identity included"
    assert services.require_app() is running

    print("PASS: app() reads honestly, require_app() names the boot phase")


def check_main_window_covers_both_absences() -> None:
    gl.app = None
    assert services.main_window() is None, "no app means no window"

    running = FakeApp()
    gl.app = running
    assert not hasattr(running, "main_win"), "the fixture's premise: main_win is unbound"
    assert services.main_window() is None, (
        "an app without main_win must read as no window -- the raw "
        "gl.app.main_win raises AttributeError here"
    )

    window = FakeWindow()
    running.main_win = window
    assert services.main_window() is window

    # Teardown does NOT null the attribute -- App._destroy_main_window destroys
    # the widget and leaves it bound, and nothing else clears it -- so a
    # destroyed window still reads as present. Pinned because the accessor's
    # docstring warns callers about exactly this, and a future slot-clearing
    # fix must land as a deliberate change to both.
    window.destroyed = True
    assert services.main_window() is window, (
        "a destroyed-but-bound main_win still comes back: nothing unbinds it"
    )

    # A slot rebound to None is seen on the next call, not cached.
    running.main_win = None
    assert services.main_window() is None

    print("PASS: main_window() covers both absences and does not invent a post-quit None")


def check_require_main_window() -> None:
    gl.app = None
    try:
        services.require_main_window()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("require_main_window() must raise while gl.app is None")
    assert "gl.app.main_win" in message, message

    running = FakeApp()
    gl.app = running
    try:
        services.require_main_window()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError(
            "require_main_window() must raise while main_win is unbound -- the "
            "window between the App being published and on_activate building it"
        )
    assert "on_activate" in message, message

    window = FakeWindow()
    running.main_win = window
    got = services.require_main_window()
    assert got is window, "require_main_window() must hand back the window itself"
    assert got is not None, "the whole point of the require_* form"

    print("PASS: require_main_window() names both absences and returns the window")


def check_settings_accessors_pass_through() -> None:
    manager = fixtures.StubSettingsManager(app_settings={"general": {"hold-time": 0.75}})
    gl.settings_manager = manager

    assert services.settings() is manager, "settings() must be the manager itself"

    view = services.app_settings()
    assert isinstance(view, AppSettings)
    assert view.data is manager.get_app_settings(), (
        "app_settings() must wrap the SHARED settings dict, not a copy"
    )
    assert view.hold_time == 0.75

    deck = services.deck_settings("DECK-SERIAL")
    assert deck == manager.get_deck_settings("DECK-SERIAL")
    deck["written-through"] = True
    assert manager.get_deck_settings("DECK-SERIAL").get("written-through") is True, (
        "deck_settings() must hand back what the manager hands back, unwrapped"
    )

    # Per-call slot read.
    replacement = fixtures.StubSettingsManager(app_settings={"general": {"hold-time": 0.25}})
    gl.settings_manager = replacement
    assert services.settings() is replacement
    assert services.app_settings().hold_time == 0.25

    print("PASS: settings()/app_settings()/deck_settings() pass through unwrapped")


def check_page_manager_pair() -> None:
    gl.page_manager = None
    assert services.page_manager() is None, "page_manager() stays honestly Optional"

    try:
        services.require_page_manager()
    except RuntimeError as e:
        message = str(e)
    else:
        raise AssertionError("require_page_manager() must raise while the slot is None")
    assert "gl.page_manager" in message, message

    manager = FakePageManager()
    gl.page_manager = manager
    assert services.page_manager() is manager
    assert services.require_page_manager() is manager

    print("PASS: page_manager() is Optional, require_page_manager() names the absence")


def check_runtime_imports_are_globals_only() -> None:
    """The module's whole portability claim: any layer can import it. That
    holds only while its runtime imports are globals plus stdlib -- a
    first-party import here is a cycle risk and, for the engine closure, a
    toolkit risk."""
    tree = ast.parse(open(MODULE_PATH, encoding="utf-8").read(), MODULE_PATH)

    type_checking_bodies: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            named = (test.id if isinstance(test, ast.Name)
                     else test.attr if isinstance(test, ast.Attribute) else None)
            if named == "TYPE_CHECKING":
                for stmt in node.body:
                    for child in ast.walk(stmt):
                        type_checking_bodies.add(id(child))

    roots: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in type_checking_bodies:
            continue
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import can only reach first-party code and has no
                # root to resolve: record it verbatim so it FAILS below.
                roots.add("." * node.level + (node.module or ""))
            elif node.module:
                roots.add(node.module.split(".")[0])

    assert roots, f"no runtime imports found in {MODULE_PATH}: this check would pass vacuously"
    first_party = {r for r in roots if r not in sys.stdlib_module_names and r != "globals"}
    assert not first_party, (
        f"services.py must import nothing first-party but globals -- every "
        f"layer imports it: {sorted(first_party)}"
    )

    print("PASS: runtime imports are globals + stdlib")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_services_accessors")

    saved = (gl.lm, gl.app, gl.settings_manager, gl.page_manager)
    try:
        check_tr_forwards_both_shapes()
        check_tr_before_the_locale_manager_exists()
        check_app_and_require_app()
        check_main_window_covers_both_absences()
        check_require_main_window()
        check_settings_accessors_pass_through()
        check_page_manager_pair()
        check_runtime_imports_are_globals_only()
    finally:
        gl.lm, gl.app, gl.settings_manager, gl.page_manager = saved

    print("ALL PASS: scenario_services_accessors")


if __name__ == "__main__":
    main()
