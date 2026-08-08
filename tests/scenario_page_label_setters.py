"""
Regression test (#141 review): every `Page.set_label_*` styling setter must
reach the UI port and still repaint the input afterwards.

The engine->UI port refactor deleted `LabelManager.update_label_editor`, but
`PageManagement/Page.py` still calls it from eight label setters
(set_label_font_family / _font_size / _font_weight / _font_color /
_outline_width / _outline_color / _font_style / _alignment). Without the
forwarder every one of those raised AttributeError *before* its trailing
`self.update_input(identifier, state)` -- so changing a label's font in the
UI silently stopped repainting the key, and the label editor never
refreshed.

What this pins, end-to-end on a real headless DeckController:
  1. no exception escapes any of the eight setters,
  2. each one reaches the port as on_input_visuals_changed(..., "labels")
     with the right controller/identifier/state,
  3. the port call happens BEFORE the trailing update_input repaint (order
     matters: an AttributeError in the forwarder aborts the repaint), and
  4. the setter still works with no UI attached (null port).

Delete `LabelManager.update_label_editor` again and this fails on the first
setter.
"""
import fixtures

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input

LABEL_POSITION = "center"

# (setter name, value) -- every Page.set_label_* that calls the forwarder.
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

        # The label manager must actually exist, or the setters would skip
        # their `if label_manager is not None:` block and this whole scenario
        # would pass vacuously.
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
            # Pre-fix this raised AttributeError: 'LabelManager' object has
            # no attribute 'update_label_editor'.
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

        # The value actually landed (the setters are not no-ops under the
        # recording port).
        assert page.get_label_font_size(identifier, state, LABEL_POSITION) == 17

        # 4. Null port (headless / pre-window): same setters, no UI, no raise,
        # repaint still happens.
        ui_port.install(None)
        journal.clear()
        page.set_label_font_size(identifier, state, LABEL_POSITION, 21)
        assert [entry[0] for entry in journal] == ["update_input"], (
            "with no UI attached the setter must still repaint (and must not "
            "reach a port that no longer exists)"
        )

        print(f"PASS: {len(SETTERS)} label setters reach the port then repaint")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)

    print("PASS: scenario_page_label_setters")


if __name__ == "__main__":
    main()
