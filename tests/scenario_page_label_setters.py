"""
Every Page.set_label_* styling setter reaches the UI port, then repaints.

Each of the eight setters forwards to on_input_visuals_changed with the aspect
"labels", then runs its trailing update_input. The port call must come first,
because a raise in the forwarder would abort the repaint.
"""

# Each setter must also work with no UI attached.
import fixtures

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input

LABEL_POSITION = "center"

# (setter name, value) for every Page.set_label_* that calls the forwarder.
SETTERS = [
    ("set_label_font_family", "Fake Sans"),
    ("set_label_font_size", 17),
    ("set_label_font_weight", 700),
    ("set_label_font_color", [10, 20, 30, 255]),
    ("set_label_outline_width", 2),
    ("set_label_outline_color", [40, 50, 60, 255]),
    ("set_label_font_style", "italic"),
    ("set_label_alignment", "left"),
]


class RecordingPort(ui_port.UIPort):
    """Records the visuals-changed calls and nothing else."""

    def __init__(self, journal):
        self.journal = journal

    def on_input_visuals_changed(self, controller, identifier, state, aspect):
        self.journal.append(("port", controller, identifier, state, aspect))


def check_font_family_every_controller(page, identifier, state) -> None:
    """The family must land on every controller the page covers and survive a
    state switch. The setter writes KeyLabel.font_name; a write to font_family
    grows a stray attribute instead of raising."""
    second = fixtures.make_headless_controller(serial="label-setters-2")
    try:
        # get_controller_input_states iterates every controller with no page
        # filter. That unscoped broadcast is existing behavior, not a
        # page-scoped guarantee this scenario pins.
        covered = page.get_controller_input_states(identifier, state)
        serials = [s.controller_input.deck_controller.serial_number() for s in covered]
        assert "label-setters-2" in serials, (
            "the second deck's input state is not in the set the setter "
            f"writes to (got {serials}) -- this leg would prove nothing"
        )

        page.set_label_font_family(identifier, state, LABEL_POSITION, "Family Two")

        for input_state in covered:
            serial = input_state.controller_input.deck_controller.serial_number()
            label = input_state.label_manager.page_labels[LABEL_POSITION]
            assert label.font_name == "Family Two", (
                f"deck {serial} kept font_name {label.font_name!r} -- the "
                "family write went somewhere KeyLabel does not read"
            )

        for input_state in covered:
            serial = input_state.controller_input.deck_controller.serial_number()
            label = input_state.label_manager.page_labels[LABEL_POSITION]
            assert not hasattr(label, "font_family"), (
                f"deck {serial}'s KeyLabel grew a stray 'font_family' "
                "attribute -- the setter is writing the wrong field name"
            )

        # A family set while state 1 is inactive must be what state 1 renders
        # once it becomes active.
        c_input = second.get_input(identifier)
        c_input.add_new_state(switch=False)
        assert 1 in c_input.states, "could not create a second input state"
        assert c_input.state == 0, "add_new_state(switch=False) switched anyway"

        page.set_label_font_family(identifier, 1, LABEL_POSITION, "Family Switched")
        c_input.set_state(1)
        label = c_input.states[1].label_manager.page_labels[LABEL_POSITION]
        assert label.font_name == "Family Switched", (
            f"after switching to state 1 the label reports {label.font_name!r} "
            "-- the family set while the state was inactive was dropped"
        )

        print("PASS: font-family reaches every controller and survives a state switch")
    finally:
        fixtures.teardown(second)


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_page_label_setters")
    controller = fixtures.make_headless_controller(serial="label-setters-1")
    journal = []
    ui_port.install(RecordingPort(journal))
    try:
        page = controller.active_page
        assert page is not None, "headless controller loaded no page"
        identifier = Input.Key("0x0")
        state = 0

        # The label manager must exist. Otherwise the setters skip their
        # guarded block and this scenario passes vacuously.
        assert page.get_label_manager(identifier, state) is not None, (
            "no LabelManager for 0x0 state 0 -- the setters' guarded block "
            "would be skipped and this test would prove nothing"
        )

        # Instrument the repaint so ordering is observable. Instance
        # attribute shadows the bound method the setters call.
        real_update_input = page.update_input

        def recording_update_input(ident, st, wake=True):
            journal.append(("update_input", ident, st))
            return real_update_input(ident, st, wake)

        page.update_input = recording_update_input

        for name, value in SETTERS:
            journal.clear()
            setter = getattr(page, name)
            # A missing LabelManager.update_label_editor raises
            # AttributeError here.
            setter(identifier, state, LABEL_POSITION, value)

            kinds = [entry[0] for entry in journal]
            assert "port" in kinds, (
                f"{name} never reached the UI port -- "
                "LabelManager.update_label_editor is missing or not forwarding"
            )
            assert "update_input" in kinds, (
                f"{name} skipped its update_input repaint (the forwarder "
                "raised and aborted the setter)"
            )
            assert kinds.index("port") < kinds.index("update_input"), (
                f"{name} repainted before notifying the UI -- the forwarder "
                "must run first, exactly where update_label_editor did"
            )

            _, got_controller, got_identifier, got_state, aspect = journal[
                kinds.index("port")
            ]
            assert got_controller is controller, f"{name} reported the wrong controller"
            assert got_identifier == identifier, (
                f"{name} reported {got_identifier} instead of {identifier}"
            )
            assert got_state == state, f"{name} reported state {got_state}, expected {state}"
            assert aspect == "labels", (
                f"{name} reported aspect {aspect!r}, expected 'labels'"
            )

        # The value landed. The setters are not no-ops under the recording
        # port.
        assert page.get_label_font_size(identifier, state, LABEL_POSITION) == 17

        # With a null port the same setters must not raise, and the repaint
        # must still happen.
        ui_port.install(None)
        journal.clear()
        page.set_label_font_size(identifier, state, LABEL_POSITION, 21)
        assert [entry[0] for entry in journal] == ["update_input"], (
            "with no UI attached the setter must still repaint (and must not "
            "reach a port that no longer exists)"
        )

        print(f"PASS: {len(SETTERS)} label setters reach the port then repaint")

        check_font_family_every_controller(page, identifier, state)
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)

    print("PASS: scenario_page_label_setters")


if __name__ == "__main__":
    main()
