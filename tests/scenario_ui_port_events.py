"""
Scenario (issue #141 step (a)): the engine->UI port contract.

Before #141 there was no way to observe what the engine wanted to tell a UI
without building a GTK widget tree -- the only headless-visible trace was the
dirty-marker dict, i.e. the "nothing was shown" path. With the port, a test
can attach a RECORDING implementation and assert the positive direction:
which inputs got mirrored, when the sidebar was asked to re-render, and that
an accepting UI means the engine does NOT dirty-mark.

Covers:
  (a) with an accepting port attached, a page load pushes an image for every
      key identifier AND the touchscreen, and ui_image_changes_while_hidden
      stays EMPTY (the exact inverse of scenario_hidden_window_markers).
  (b) on_page_changed fires with the page's actions already initialized --
      the ordering load_page deliberately arranges (the sidebar's
      ActionManager can only render actions that exist).
  (c) install(None) restores the null port and pushes dirty-mark again.
  (d) every port method is callable from a headless process without raising,
      on the null port AND on a GtkUIAdapter with no window attached. This is
      the class of bug that used to be real: update_state_switcher reached
      gl.app.main_win.sidebar unguarded, and FlatpakDeckDisconnectThread
      called check_for_errors() with no window -- both AttributeError crashes
      before the window existed.
"""
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input

WATCHDOG_SECONDS = 60


class RecordingPort(ui_port.UIPort):
    """Accepts every frame and records everything the engine reports."""

    def __init__(self):
        self.pushes: list = []
        self.page_changes: list = []
        self.visuals: list = []
        self.states_changed: list = []
        self.state_selected: list = []
        self.low_fps: list = []
        self.layout_changes: list = []
        self.decks_added: list = []
        self.decks_removed: list = []
        self.availability_refreshes = 0
        self.page_list_changes = 0
        self.plugin_problems: list = []
        # Set by the test: snapshotted whenever on_page_changed fires.
        self.page_change_probe = lambda: None

    def push_input_image(self, controller, identifier, image) -> bool:
        self.pushes.append(identifier)
        return True

    def on_page_changed(self, controller) -> None:
        self.page_changes.append(self.page_change_probe())

    def on_input_visuals_changed(self, controller, identifier, state, aspect) -> None:
        self.visuals.append((identifier, state, aspect))

    def on_input_states_changed(self, controller, identifier, n_states) -> None:
        self.states_changed.append((identifier, n_states))

    def on_input_state_selected(self, controller, identifier, state) -> None:
        self.state_selected.append((identifier, state))

    def set_low_fps_warning(self, controller, shown) -> None:
        self.low_fps.append(shown)

    def on_deck_layout_changed(self, controller) -> None:
        self.layout_changes.append(controller)

    def on_deck_added(self, controller) -> None:
        self.decks_added.append(controller)

    def on_deck_removed(self, controller) -> None:
        self.decks_removed.append(controller)

    def refresh_deck_availability(self) -> None:
        self.availability_refreshes += 1

    def on_page_list_changed(self) -> None:
        self.page_list_changes += 1

    def notify_plugin_problem(self, plugin_id, kind) -> None:
        self.plugin_problems.append((plugin_id, kind))


def check_accepted_pushes_suppress_markers(port) -> None:
    controller = fixtures.make_headless_controller(serial="ui-port-1")
    try:
        keys = controller.inputs[Input.Key]
        touchscreens = controller.inputs[Input.Touchscreen]
        assert keys, "fixture sanity: expected at least one key input"
        assert touchscreens, "fixture sanity: expected a touchscreen input"

        wanted = {i.identifier for i in keys} | {touchscreens[0].identifier}
        assert fixtures.wait_until(
            lambda: wanted <= set(port.pushes), timeout=10), (
            f"the page load did not mirror every input: missing "
            f"{sorted(str(i) for i in wanted - set(port.pushes))}"
        )

        assert controller.ui_image_changes_while_hidden == {}, (
            "the engine dirty-marked even though every push was ACCEPTED -- "
            f"markers: {controller.ui_image_changes_while_hidden!r}"
        )
        print("PASS: an accepting port receives every input and suppresses dirty markers")

        # (c) Detach: pushes must dirty-mark again, exactly as headless does.
        ui_port.install(None)
        keys[0].update(force=True)
        assert fixtures.wait_until(
            lambda: keys[0].identifier in controller.ui_image_changes_while_hidden,
            timeout=5), (
            "with the null port installed the engine did not fall back to "
            "dirty-marking -- the map-time replay would have nothing to show"
        )
        print("PASS: install(None) restores the null port and the dirty-mark fallback")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)


def check_page_change_follows_action_init() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "ui_port_icon.png"), color=(0, 180, 0))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)

    port = RecordingPort()
    ui_port.install(port)
    controller = fixtures.make_headless_controller(serial="ui-port-2")
    try:
        key_ident = controller.inputs[Input.Key][0].identifier.json_identifier
        action_page = gl.page_manager.get_page(
            fixtures.seed_action_page("UiPortActions", key_ident), controller)

        def actions_ready() -> bool:
            for by_ident in action_page.action_objects.values():
                for by_state in by_ident.values():
                    for by_index in by_state.values():
                        for action in by_index.values():
                            if getattr(action, "on_ready_called", False):
                                return True
            return False

        port.page_change_probe = actions_ready
        port.page_changes.clear()
        controller.load_page(action_page, allow_reload=True)

        assert fixtures.wait_until(lambda: any(port.page_changes), timeout=10), (
            f"on_page_changed never fired with the page's actions initialized "
            f"(observed probes: {port.page_changes!r}) -- the sidebar would "
            f"render a page whose action objects don't exist yet"
        )
        print("PASS: on_page_changed reaches the UI with the new page's actions initialized")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)


def check_every_port_method_is_headless_safe() -> None:
    """No window, no gl.app: every method must be a quiet no-op."""
    from src.windows.ui_adapter import GtkUIAdapter

    identifier = Input.Key("0x0")
    controller = object()

    def exercise(port, label: str) -> None:
        try:
            assert port.push_input_image(controller, identifier, object()) is False, (
                f"{label}: push must be refused with no UI attached"
            )
            port.on_page_changed(controller)
            port.on_input_visuals_changed(controller, identifier, 0, "labels")
            port.on_input_visuals_changed(controller, identifier, 0, "layout")
            port.on_input_visuals_changed(controller, identifier, 0, "background")
            port.on_input_states_changed(controller, identifier, 3)
            port.on_input_state_selected(controller, identifier, 1)
            port.set_low_fps_warning(controller, True)
            port.on_deck_layout_changed(controller)
            assert port.query_input_widget(controller, identifier) is None
            assert port.query_deck_widget(controller, "key_grid") is None
            assert port.query_deck_widget(controller, "deck_stack_child") is None
            port.on_deck_added(controller)
            port.on_deck_removed(controller)
            port.refresh_deck_availability()
            port.on_page_list_changed()
            port.notify_plugin_problem("com_example_Plugin", "outdated")
            port.notify_plugin_problem("com_example_Plugin", "missing")
        except Exception as e:
            raise AssertionError(f"{label}: port method raised headless: {e!r}")

    exercise(ui_port.UIPort(), "null port")
    exercise(GtkUIAdapter(), "unattached GtkUIAdapter")

    # on_deck_layout_changed ran inline on the main thread, which is the path
    # that imports KeyGrid lazily -- proving the ui_adapter <-> KeyGrid import
    # cycle is actually broken.
    import sys
    assert "src.windows.mainWindow.elements.KeyGrid" in sys.modules, (
        "the rotation path never reached its lazy KeyGrid import"
    )
    print("PASS: every port method is callable headless on both the null port and the adapter")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_ui_port_events")

    port = RecordingPort()
    ui_port.install(port)
    check_accepted_pushes_suppress_markers(port)
    check_page_change_follows_action_init()
    check_every_port_method_is_headless_safe()

    print("PASS: scenario_ui_port_events")


if __name__ == "__main__":
    main()
