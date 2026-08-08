"""
The engine -> UI port (#141).

The render engine (``src/backend/DeckManagement/``) must not know that a GTK
window exists: no widget imports, no ``gl.app.main_win`` reads, no GTK state
queried from the media thread. Everything it wants to tell a UI goes through
the small synchronous interface below; whatever UI is attached implements it.

The base class **is** the null implementation: with no UI attached every
method no-ops and ``push_input_image`` returns False, which is exactly the
headless behaviour the engine already had (it dirty-marks and the UI
recomposites on map).

The pull direction is deliberately NOT part of this port: on map,
``KeyGrid``/``ScreenBar.load_from_changes`` call
``controller.get_input(...).get_current_image()`` themselves. The UI reading
pure engine state is legal in-process and is precisely the shape a future
"recomposite request" IPC message takes.

This module imports nothing but ``typing`` on purpose, so any module may
import it without risking a cycle.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

    from src.backend.DeckManagement.DeckController import DeckController
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier


class UIPort:
    """Engine-side view of 'whatever UI is attached, if any'.

    Threading contract: EVERY method is callable from any thread (media
    thread, USB monitor, tick/action threads, GLib main loop) and MUST return
    without blocking on the GTK loop. Implementations marshal widget
    mutations with GLib.idle_add; run_on_main is banned inside implementations
    (a wedged main loop must never stall the media writer). The #181
    discipline -- convert on the calling thread, idle only the paint -- is the
    template for push_input_image.
    """

    # -- render mirror (hot path: media thread, up to ~keys x fps calls/s) --

    def push_input_image(self, controller: "DeckController",
                         identifier: "InputIdentifier",
                         image: "Image.Image") -> bool:
        """Mirror a freshly composed input image into the UI.

        True = accepted-or-pending (conversion may run on this thread; the
        paint is idled/throttled). False = not shown (no window, unmapped,
        grid mid-rebuild): the caller dirty-marks. Never raises -- ANY
        internal exception or refusal returns False.

        Late-failure channel: if an accepted frame is later dropped (unmap
        mid-throttle, widget orphaned by a window rebuild), the IMPLEMENTATION
        writes the dirty marker itself -- ``ui_image_changes_while_hidden``
        stays engine-side for exactly this reason.
        """
        return False

    # -- per-deck sync (fire-and-forget; coalescing is the adapter's job) --

    def on_page_changed(self, controller: "DeckController") -> None:
        """A page finished loading on this deck; the sidebar may need to
        re-render for the new page's actions."""

    def on_input_visuals_changed(self, controller: "DeckController",
                                 identifier: "InputIdentifier",
                                 state: int, aspect: str) -> None:
        """One input's labels/layout/background changed.

        `aspect` is one of "labels", "layout", "background"; `state` is the
        input state the change belongs to.
        """

    def on_input_states_changed(self, controller: "DeckController",
                                identifier: "InputIdentifier",
                                n_states: int) -> None:
        """The number of states on an input changed."""

    def on_input_state_selected(self, controller: "DeckController",
                                identifier: "InputIdentifier",
                                state: int) -> None:
        """The active state of an input changed."""

    def set_low_fps_warning(self, controller: "DeckController",
                            shown: bool) -> None:
        """Show/hide this deck's low-FPS banner."""

    def on_deck_layout_changed(self, controller: "DeckController") -> None:
        """The deck's key layout changed (rotation).

        Implementations MUST rebuild synchronously when already on the main
        loop: the caller reloads the page immediately afterwards, and an idled
        rebuild would let those repaints hit the pre-rotation grid.
        """

    def query_input_widget(self, controller: "DeckController",
                           identifier: "InputIdentifier") -> "object | None":
        """Deprecated plugin-compat shim -- backs ControllerKey.get_own_ui_key.

        In-process only: it hands out a live widget, so it is the one method
        here that cannot survive a process boundary.
        """
        return None

    def query_deck_widget(self, controller: "DeckController",
                          part: str) -> "object | None":
        """Deprecated plugin-compat shim -- backs
        DeckController.get_own_deck_stack_child/get_own_key_grid.

        `part` is "deck_stack_child" or "key_grid". In-process only, same
        caveat as query_input_widget.
        """
        return None

    # -- app level (USB monitor / boot rescan / flatpak poll threads) --

    def on_deck_added(self, controller: "DeckController") -> None:
        """A deck was registered and needs a UI page."""

    def on_deck_removed(self, controller: "DeckController") -> None:
        """A deck was unregistered.

        MUST synchronously queue the UI detach before returning (plan P1.3):
        the caller spawns the slow close thread immediately afterwards, and a
        fast unplug/replug must not race a late detach against a fresh add.
        """

    def refresh_deck_availability(self) -> None:
        """Re-evaluate the "no decks connected" error screen."""

    def on_page_list_changed(self) -> None:
        """The set of pages changed; refresh any page selector."""

    def notify_plugin_problem(self, plugin_id: str, kind: str) -> None:
        """Surface a plugin problem to the user. `kind` is "outdated" or
        "missing"."""


# The process-wide null port. A single shared instance (rather than a fresh
# one per install(None)) so tests can assert identity.
_NULL_PORT = UIPort()

_port: UIPort = _NULL_PORT


def get() -> UIPort:
    """The currently installed port. Never None."""
    return _port


def install(port: "UIPort | None") -> None:
    """Install `port` as the process-wide engine->UI port; None restores the
    null port (window teardown, quit, tests)."""
    global _port
    _port = _NULL_PORT if port is None else port
