"""Typed accessors for the process-wide services that live on globals.

The gl module is a namespace of late-initialised slots, and well over six
hundred places read the four hottest of them, which are the locale manager,
the App, the settings manager and the page manager backend. Each of those
reads is an invisible dependency edge. gl.lm names nothing a reader follows,
nothing a rename tracks, and nothing a test substitutes without a reach into
another module's namespace. This module gives those four reads a name each.

What an accessor buys over the raw read

It is checked. Most raw reads sit in unannotated defs, whose bodies mypy skips.
An accessor is one annotated function, checked once, and its callers inherit a
concrete type.

It puts the None guard in one place. main_window() holds the hand-rolled
"if gl.app is not None and hasattr(gl.app, "main_win")" dance once, and the
require_* pair turns an AttributeError on NoneType into a named boot-phase
error that names the construction step which has not run.

It is a seam. The body can change, to an injection, a per-test double or a
lazily built service, and no caller changes.

It is greppable. "from src.backend.services import tr" is an import edge that
the language server resolves and a rename follows.

What it is not

It is no service locator, no registry and no container. Nothing registers here
and nothing is constructed here. Every function reads the same slot the raw
expression reads, on every call, so a slot rebound underneath it, which the
test harness does, still applies. The slots stay where they are, and only the
protocol gets a name.

Honest optionals

app() and page_manager() return "| None", because running code really does
find those slots absent, before Main.__init__ publishes the App, and inside
the None checks of the D-Bus API and of the deck controller teardown. The
require_* variants serve a site whose None branch cannot run after boot. A
require_* at a site whose None branch is live deletes a real guard, so the
pair keeps that choice explicit.

Imports

globals at runtime and nothing else first-party. Every type imports under
TYPE_CHECKING, and "from __future__ import annotations" makes every annotation
a string. So any layer can import this, including the render engine's
widget-free closure, GtkHelper and the windows, with no cycle and no toolkit.
The deployment floor runs Python 3.13, which evaluates a parameter and return
annotation at def time, and the future import prevents that.
scenario_floor_import executes this module body on that interpreter to keep it
true.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import globals as gl

if TYPE_CHECKING:
    from locales.LocaleManager import LocaleManager
    from src.app import App
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
    from src.backend.SettingsManager import AppSettings, SettingsManager
    from src.windows.mainWindow.mainWindow import MainWindow


# Translations

def tr(key: str, fallback: str | None = None) -> str:
    """The translation for key in the active language.

    This forwards to gl.lm.get, whose resolution order differs from what the
    parameter name suggests. It reads the active language, then the fallback
    locale, which defaults to the key itself, and only a value still None
    after that reaches fallback. So fallback does not answer an unknown key,
    and tr("missing", "FB") returns "missing" and not "FB". The result is
    HTML-escaped for GTK markup.

    No fallback and a fallback of None mean the same thing to the locale
    manager, so this hands the call over in two shapes rather than forward a
    None that the parameter is not typed to take.

    key is typed str and this guards it in no way. get resolves a None key to
    None, and that None comes back out through the "-> str". mypy rejects it
    at every annotated call site, so it can arrive from an unchecked one
    alone. The Store badge rows record that shape in a comment and return
    early rather than depend on it.

    Raises RuntimeError before main.create_global_objects() builds the locale
    manager. The raw read raises there too, with an AttributeError on None
    that names neither the slot nor the phase, and this is the same crash with
    the cause written on it.
    """
    # gl.lm carries a concrete annotation, because the slot is late-init, so
    # this widening keeps the pre-boot branch alive. Code reaches that branch,
    # and a None check against a non-optional type narrows to an uninhabited
    # type, whose body mypy skips.
    lm: LocaleManager | None = gl.lm
    if lm is None:
        raise RuntimeError(
            f"translation requested for {key!r} before the locale manager "
            f"exists -- gl.lm is built by main.create_global_objects()"
        )
    if fallback is None:
        return lm.get(key)
    return lm.get(key, fallback)


# The application and its window

def app() -> App | None:
    """The running App, or None before Main.__init__ publishes it.

    This is the honest read, so a caller that runs during boot sees the
    absence. Use require_app() where the None cannot happen.
    """
    return gl.app


def require_app() -> App:
    """The running App, never None.

    For a site whose None branch cannot run once the app is up, such as a
    window handler, a plugin callback or an action. It raises RuntimeError
    rather than hand back a None that crashes one dereference later, in a
    place whose message names no boot phase.

    Adopt it only where that is true. A site whose None branch is live loses a
    guard. The require_* pair moves a guard into one place and removes none.
    """
    running = gl.app
    if running is None:
        raise RuntimeError(
            "the App does not exist yet -- gl.app is published in "
            "Main.__init__, before app.run(). Work that must wait for the "
            "running app belongs on src.backend.startup_queue instead."
        )
    return running


def main_window() -> MainWindow | None:
    """The main window, or None while there is not one.

    This holds the two-step guard that its callers hand-roll, because the
    window goes missing in two ways and only one of them gives a None. gl.app
    itself is absent until it publishes, and nothing binds main_win at all,
    because no class body declares it, until App.on_activate constructs it. A
    plain gl.app.main_win therefore raises AttributeError between those two
    points, so this reads the attribute with getattr rather than test it for
    None.

    It does not return None once the window is gone.
    App._destroy_main_window destroys the widget and leaves the attribute
    bound, and nothing else clears it, so after that this hands back a
    destroyed and truthy window. Code reaches that state, because the AppQuit
    fan-out that runs the plugin teardown hooks comes after the destroy. A
    caller on the quit path must not repaint or present through what it gets
    here. A cleared slot in the teardown would make the absence honest.
    """
    running = gl.app
    if running is None:
        return None
    window: MainWindow | None = getattr(running, "main_win", None)
    return window


def require_main_window() -> MainWindow:
    """The main window, never None.

    This is what the raw gl.app.main_win dereference means at almost every
    one of its sites, such as a window handler, a dialog, a page or action
    editor, and a plugin's UI callback. None of them carries a None branch,
    and neither other accessor fits. main_window() makes each one invent a
    branch it never had, and require_app().main_win types as Any, because
    App.on_activate carries no annotation and nothing declares the attribute.
    This one hands back a concrete window.

    An absence here means that nothing built the window yet, before
    App.on_activate, because the App itself has not published or because the
    attribute is unbound. It does not cover the quit path. The attribute stays
    bound past the destroy (see main_window()), so a caller during teardown
    gets a destroyed window rather than an error, and must not paint through
    it.
    """
    window = main_window()
    if window is None:
        raise RuntimeError(
            "the main window does not exist yet -- gl.app.main_win is bound "
            "by App.on_activate, and until it runs there is either no App or "
            "no window attribute on it."
        )
    return window


# Settings

def settings() -> SettingsManager:
    """The settings manager.

    A passthrough with a concrete type. The slot is late-init, and nothing
    that runs observes it absent, so a caller inherits a real type rather than
    a union to narrow. Before main.create_global_objects() it hands back the
    None that the raw read gives.
    """
    return gl.settings_manager


def app_settings() -> AppSettings:
    """A typed view onto the app settings.

    AppSettings wraps the shared settings dict and copies nothing, so the
    view is cheap and a write reaches the settings. Build one per use rather
    than hold it.

    This dereferences the settings manager, so before
    main.create_global_objects() builds it this raises the raw AttributeError
    on None. tr() is the one accessor that trades that for a named error,
    because hundreds of places read its slot.
    """
    return gl.settings_manager.app()


def deck_settings(serial_number: str) -> dict:
    """This deck's settings, as the settings manager hands them out.

    Production returns a fresh deep copy per call, so a mutation of the result
    is safe and persists nothing. Pair it with save_deck_settings, as the raw
    call site does.

    This dereferences the settings manager, so before boot it raises the same
    AttributeError on None that the raw call site raises.
    """
    return gl.settings_manager.get_deck_settings(serial_number)


# Pages

def page_manager() -> PageManagerBackend | None:
    """The page manager backend, or None when there is not one.

    Honestly optional. A call can reach the D-Bus API before
    main.create_global_objects() builds this, and deck controller teardown
    runs after it is gone. Code runs on both None branches.
    """
    return gl.page_manager


def require_page_manager() -> PageManagerBackend:
    """The page manager backend, never None.

    For a site reached with the pages loaded alone, such as page editing,
    action configuration and the deck UI. Adopt it only where nothing reaches
    the None branch. At a site that reaches it, the guard stays.
    """
    manager = gl.page_manager
    if manager is None:
        raise RuntimeError(
            "the page manager backend does not exist yet -- gl.page_manager "
            "is built by main.create_global_objects(), after the settings "
            "manager it takes as an argument."
        )
    return manager
