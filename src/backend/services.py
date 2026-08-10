"""
Typed accessors for the process-wide services that live on ``globals``.

The `gl` module is a namespace of late-initialised slots, and the four hottest
of them -- the locale manager, the App, the settings manager and the page
manager backend -- are read from well over six hundred places. Every one of
those reads is an invisible dependency edge: `gl.lm` names nothing a reader can
follow, nothing a rename can track, and nothing a test can substitute without
reaching into another module's namespace. This module gives those four reads a
name each.

WHAT AN ACCESSOR BUYS OVER THE RAW READ

* It is CHECKED. Most raw reads sit in unannotated defs, whose bodies mypy does
  not look at at all; an accessor is one annotated function, checked once,
  whose callers inherit a concrete type.
* It puts the None-guard in ONE place. `main_window()` is the hand-rolled
  ``if gl.app is not None and hasattr(gl.app, "main_win")`` dance, written
  once; the ``require_*`` pair turns "AttributeError on NoneType" into a named
  boot-phase error that says which construction step has not run yet.
* It is a SEAM. The body can change -- injection, a per-test double, a lazily
  built service -- without touching a caller.
* It is GREPPABLE. ``from src.backend.services import tr`` is an import edge
  the language server resolves and a rename follows.

WHAT IT DELIBERATELY IS NOT

Not a service locator, not a registry, not a container. Nothing is registered
here and nothing is constructed here: every function is a read of the same slot
the raw expression reads, performed on every call so that a slot rebound
underneath it (which is exactly what the test harness does) is honoured. The
slots stay where they are; only the protocol gets a name.

HONEST OPTIONALS

``app()`` and ``page_manager()`` return ``| None`` because those slots really
are absent to running code -- before ``Main.__init__`` publishes the App, and
during the DBus API's and the deck controller teardown's None-checks
respectively. The ``require_*`` variants are for the sites whose None branch
cannot be reached post-boot; adopting one at a site whose None branch IS live
would delete a real guard, so the pair exists to keep that choice explicit
rather than implicit.

IMPORTS

``globals`` at runtime and nothing else first-party; every type is imported
under ``TYPE_CHECKING`` and every annotation is a string by way of
``from __future__ import annotations``. So any layer may import this -- the
render engine's widget-free closure, GtkHelper, the windows -- with no cycle
and no toolkit dragged along. The deployment floor (Python 3.13) evaluates
parameter and return annotations at def time, which the future-import is what
prevents; scenario_floor_import executes this module body on that interpreter
to keep it true.
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


# ====================================================================== #
# Translations                                                           #
# ====================================================================== #

def tr(key: str, fallback: str | None = None) -> str:
    """The translation for `key` in the active language.

    Straight ``gl.lm.get``, whose resolution order is worth stating because it
    is not the order the parameter name suggests: the active language, then the
    fallback locale DEFAULTING TO THE KEY ITSELF, and only a value that is
    still None after that reaches `fallback`. So `fallback` does not answer an
    unknown key -- ``tr("missing", "FB")`` is ``"missing"``, not ``"FB"``. The
    result is HTML-escaped for GTK markup.

    Passing no fallback and passing ``None`` mean the same thing to the locale
    manager, which is why this hands the call over in two shapes rather than
    forwarding a None the parameter is not typed to take.

    `key` is typed ``str`` and is not defended: ``get`` resolves a None key to
    None, and that None comes straight back out through the ``-> str``. mypy
    rejects it at every annotated call site, so it can only arrive from an
    unchecked one -- the Store badge rows record that shape in a comment and
    short-circuit rather than lean on it.

    Raises RuntimeError when called before ``main.create_global_objects()``
    builds the locale manager -- the raw read raises there too (an
    AttributeError on None naming neither the slot nor the phase), and this is
    the same crash with the cause written on it.
    """
    # gl.lm is annotated concretely (late-init), so widening here is what keeps
    # the pre-boot branch a branch: it is genuinely reachable, and a None-check
    # against a non-Optional narrows to an uninhabited type whose body mypy
    # then skips entirely.
    lm: LocaleManager | None = gl.lm
    if lm is None:
        raise RuntimeError(
            f"translation requested for {key!r} before the locale manager "
            f"exists -- gl.lm is built by main.create_global_objects()"
        )
    if fallback is None:
        return lm.get(key)
    return lm.get(key, fallback)


# ====================================================================== #
# The application and its window                                         #
# ====================================================================== #

def app() -> App | None:
    """The running App, or None before ``Main.__init__`` publishes it.

    The honest read: callers that genuinely run during boot get to see the
    absence. Use ``require_app()`` where the None cannot happen.
    """
    return gl.app


def require_app() -> App:
    """The running App, never None.

    For the sites whose None branch is unreachable once the app is up -- a
    window handler, a plugin callback, an action. Raises RuntimeError rather
    than handing back a None that would crash one dereference later somewhere
    with no boot phase in the message.

    Adopt it only where that unreachability is real: converting a site whose
    None branch is live deletes a guard. The ``require_*`` pair moves the
    guard into one place, it never removes one.
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

    Subsumes the two-step guard its callers hand-roll, because there are two
    distinct ways for the window to be missing and only one of them is a None:
    ``gl.app`` itself is absent until it is published, and ``main_win`` is not
    BOUND at all -- no class-body declaration -- until ``App.on_activate``
    constructs it. So a plain ``gl.app.main_win`` raises AttributeError in the
    window between those two points, which is why this reads the attribute
    defensively rather than testing it for None.

    NOT None once the window is gone, though. ``App._destroy_main_window``
    destroys the widget and leaves the attribute bound, and nothing else
    clears it, so after that point this hands back a destroyed-but-truthy
    window -- reachably, because the AppQuit fan-out that runs plugin teardown
    hooks comes after the destroy. Callers on the quit path must not repaint
    or present through what they get here. Clearing the slot in the teardown
    is the fix that would make the absence honest.
    """
    running = gl.app
    if running is None:
        return None
    window: MainWindow | None = getattr(running, "main_win", None)
    return window


def require_main_window() -> MainWindow:
    """The main window, never None.

    This is what the raw ``gl.app.main_win`` dereference actually means at the
    overwhelming majority of its sites -- a window handler, a dialog, a page or
    action editor, a plugin's UI callback. None of them has a None branch, and
    neither of the other two accessors fits: ``main_window()`` would make each
    one invent a branch it never had, and ``require_app().main_win`` types as
    Any, because ``App.on_activate`` is unannotated and nothing declares the
    attribute. This one hands back a concrete window.

    Absence here means the window has not been built yet -- before
    ``App.on_activate``, whether because the App itself is not published or
    because the attribute is not bound. Absence does NOT cover the quit path:
    the attribute stays bound past the destroy (see ``main_window()``), so a
    caller running during teardown gets a destroyed window rather than an
    error, and must not paint through it.
    """
    window = main_window()
    if window is None:
        raise RuntimeError(
            "the main window does not exist yet -- gl.app.main_win is bound "
            "by App.on_activate, and until it runs there is either no App or "
            "no window attribute on it."
        )
    return window


# ====================================================================== #
# Settings                                                               #
# ====================================================================== #

def settings() -> SettingsManager:
    """The settings manager.

    A passthrough, and typed concretely on purpose: the slot is late-init but
    is never observed absent by anything that runs, so a caller inherits a real
    type instead of a union it would have to narrow. Called before
    ``main.create_global_objects()`` it hands back the None the raw read does.
    """
    return gl.settings_manager


def app_settings() -> AppSettings:
    """A typed view onto the app settings.

    ``AppSettings`` wraps the shared settings dict without copying it, so the
    view is cheap and writes through; build one per use rather than holding it.

    Dereferences the settings manager, so before
    ``main.create_global_objects()`` builds it this raises the raw, unnamed
    AttributeError on None -- ``tr()`` is the one accessor that trades that for
    a named error, because its slot is read from hundreds of places.
    """
    return gl.settings_manager.app()


def deck_settings(serial_number: str) -> dict:
    """This deck's settings, as the settings manager hands them out.

    A fresh deep copy per call in production, so mutating the result is safe
    and is also NOT persisted -- pair it with ``save_deck_settings`` exactly as
    the raw call site does.

    Dereferences the settings manager, so pre-boot it raises the same unnamed
    AttributeError on None that the raw call site does.
    """
    return gl.settings_manager.get_deck_settings(serial_number)


# ====================================================================== #
# Pages                                                                  #
# ====================================================================== #

def page_manager() -> PageManagerBackend | None:
    """The page manager backend, or None when there is not one.

    Honestly Optional: the DBus API can be reached before
    ``main.create_global_objects()`` builds it, and deck controller teardown
    runs after it is gone. Both of those None branches are live code.
    """
    return gl.page_manager


def require_page_manager() -> PageManagerBackend:
    """The page manager backend, never None.

    For the sites reached only with pages loaded -- page editing, action
    configuration, the deck UI. Adopt it only where the None branch is
    genuinely unreachable: at a site that has one, the guard has to stay.
    """
    manager = gl.page_manager
    if manager is None:
        raise RuntimeError(
            "the page manager backend does not exist yet -- gl.page_manager "
            "is built by main.create_global_objects(), after the settings "
            "manager it takes as an argument."
        )
    return manager
