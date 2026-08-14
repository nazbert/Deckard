"""The port from the engine to the UI.

The render engine (src/backend/DeckManagement) must not know that a GTK window
exists. That means no widget imports, no gl.app.main_win reads, and no GTK
state read from the media thread. Everything the engine tells a UI goes
through the small synchronous interface below, which the attached UI
implements.

The base class is the null implementation. With no UI attached every method
does nothing and push_input_image returns False, which is the headless
behaviour, so the engine dirty-marks and the UI recomposites on map.

The pull direction stays outside this port. On map, KeyGrid and
ScreenBar.load_from_changes call controller.get_input(...).get_current_image()
themselves. A UI that reads pure engine state is legal in-process, and it has
the shape a later "recomposite request" IPC message takes.

This module imports typing only, so any module can import it without a cycle.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.InputIdentifier import InputIdentifier


class UIPort:
    """Engine-side view of the attached UI, if a UI is attached.

    Threading contract. Every method accepts a call from any thread (the media
    thread, the USB monitor, tick and action threads, the GLib main loop) and
    must return without a block on the GTK loop. An implementation marshals a
    widget mutation with GLib.idle_add and must not call run_on_main, because
    a wedged main loop must not stall the media writer. push_input_image shows
    the pattern, which converts on the calling thread and idles the paint.
    """

    # Render mirror on the hot path. The media thread calls it up to keys x
    # fps a second.

    def push_input_image(self, controller: "DeckController",
                         identifier: "InputIdentifier",
                         image: "Image.Image") -> bool:
        """Mirror a freshly composed input image into the UI.

        True means accepted or pending. The conversion can run on this thread,
        and the implementation idles and throttles the paint. False means the
        UI does not show it (no window, unmapped, grid mid-rebuild) and the
        caller dirty-marks. This never raises. An internal exception or a
        refusal returns False.

        The implementation writes the dirty marker itself when it drops an
        accepted frame later (an unmap mid-throttle, a widget orphaned by a
        window rebuild). ui_image_changes_while_hidden stays engine-side for
        that late failure.
        """
        return False

    # Per-deck sync. The caller does not wait; the adapter coalesces.

    def on_page_changed(self, controller: "DeckController") -> None:
        """A page finished loading on this deck; the sidebar may need to
        re-render for the new page's actions."""

    def on_input_visuals_changed(self, controller: "DeckController",
                                 identifier: "InputIdentifier",
                                 state: int, aspect: str) -> None:
        """One input's labels, layout or background changed.

        aspect is "labels", "layout" or "background". state is the input state
        that the change belongs to.
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

        An implementation already on the main loop must rebuild synchronously.
        The caller reloads the page straight afterwards, and an idled rebuild
        lets those repaints reach the grid from before the rotation.
        """

    def query_input_widget(self, controller: "DeckController",
                           identifier: "InputIdentifier") -> "object | None":
        """Deprecated plugin-compat shim behind ControllerKey.get_own_ui_key.

        In-process only. It hands out a live widget, so it is the one method
        here that cannot cross a process boundary.
        """
        return None

    def query_deck_widget(self, controller: "DeckController",
                          part: str) -> "object | None":
        """Deprecated plugin-compat shim behind
        DeckController.get_own_deck_stack_child and get_own_key_grid.

        part is "deck_stack_child" or "key_grid". In-process only, with the
        same limit as query_input_widget.
        """
        return None

    # App level. The USB monitor, the boot rescan and the flatpak poll call
    # these.

    def on_deck_added(self, controller: "DeckController") -> None:
        """A deck was registered and needs a UI page."""

    def on_deck_removed(self, controller: "DeckController") -> None:
        """A deck was unregistered.

        The implementation must queue the UI detach before it returns. The
        caller starts the slow close thread straight afterwards, and a fast
        unplug and replug must not race a late detach against a fresh add.
        """

    def refresh_deck_availability(self) -> None:
        """Re-evaluate the "no decks connected" error screen."""

    def on_page_list_changed(self) -> None:
        """The set of pages changed; refresh any page selector."""

    def notify_plugin_problem(self, plugin_id: str, kind: str) -> None:
        """Show a plugin problem to the user. kind is "outdated" or
        "missing"."""


# The process-wide null port. install(None) restores this one instance, so a
# test can assert identity.
_NULL_PORT = UIPort()

_port: UIPort = _NULL_PORT


def get() -> UIPort:
    """The currently installed port. Never None."""
    return _port


def install(port: "UIPort | None") -> None:
    """Install port as the process-wide engine-to-UI port. None restores the
    null port on window teardown, on quit, and in tests."""
    global _port
    _port = _NULL_PORT if port is None else port
