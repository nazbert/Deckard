"""
Scenario (the "late-failure marker channel"): a frame that
push_input_image ACCEPTED but the UI later dropped must still be recorded in
`controller.ui_image_changes_while_hidden`.

`push_input_image` answers True as soon as a frame is in the input's mirror
slot -- the engine therefore does NOT dirty-mark it. But the window can unmap
(or a rebuild orphan the widget) before that paint runs, and by then there is
no engine call left to return False through. Both drop sites route to
`ui_adapter.mark_dirty` instead:

  * `GtkUIAdapter._drain_mirror` -- the slot's main-loop drain, when the
    window unmapped before it ran, the widget is gone, or the paint raises;
  * `KeyButton.paint_mirror_frame` -- the mapped-guard that refuses to paint
    a button unmapped between queuing and running the idle, and its paint
    except.

Without the marker, `KeyGrid/ScreenBar.load_from_changes` has nothing to
replay on remap and the preview stays stale until something else repaints it
-- the exact "the window only updates when re-opened" symptom.

Headless tier: no widget is constructed. The KeyButton half calls the REAL
`paint_mirror_frame`/`_mark_dropped` against a stand-in `self`, which is what
makes it a test of that code rather than of a reimplementation. An ACCEPTING
port is installed for the whole run so the engine's own media ticks never
write markers -- every marker asserted below is one the ADAPTER wrote.
"""
from types import SimpleNamespace

import fixtures

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input
from src.windows.ui_adapter import GtkUIAdapter, _MirrorSlot, mark_dirty


class AcceptingPort(ui_port.UIPort):
    """Keeps the engine quiet: accepted pushes are never dirty-marked, so the
    marker dict below contains only what the adapter put there."""

    def push_input_image(self, controller, identifier, image) -> bool:
        return True


class _RaisingImage:
    def prepare_mirror_frame(self, image):
        return image

    def paint_mirror_frame(self, payload):
        raise RuntimeError("screenbar widget was disposed")


class _RecordingImage:
    def __init__(self):
        self.painted = []

    def prepare_mirror_frame(self, image):
        return image

    def paint_mirror_frame(self, payload):
        self.painted.append(payload)
        return False


def _child_with_screenbar(image_widget):
    return SimpleNamespace(
        page_settings=SimpleNamespace(
            deck_config=SimpleNamespace(
                screenbar=SimpleNamespace(image=image_widget)
            )
        )
    )


def check_touchscreen_drain_drops(controller, ts_ident) -> None:
    markers = controller.ui_image_changes_while_hidden

    # 1. A successful drain must NOT mark -- otherwise every assertion below
    # would pass for the wrong reason.
    adapter = GtkUIAdapter()
    image_widget = _RecordingImage()
    adapter.bind(controller, _child_with_screenbar(image_widget))
    adapter._window_mapped = True

    markers.clear()
    assert adapter.push_input_image(controller, ts_ident, object()) is True
    assert image_widget.painted == [], (
        "a push painted on the producer's own stack instead of marshalling it"
    )
    assert adapter._drain_mirror(controller, ts_ident) is False
    assert len(image_widget.painted) == 1, "the drain never painted the accepted frame"
    # Immediately again: inside the mirror interval, so it is held in the slot
    # and answered True while a delayed drain is armed for it.
    assert adapter.push_input_image(controller, ts_ident, object()) is True
    assert len(image_widget.painted) == 1, "the interval let a second frame through"
    assert adapter._drain_mirror(controller, ts_ident) is False
    assert len(image_widget.painted) == 2, "the delayed drain never painted the held frame"
    assert not markers, f"a SUCCESSFUL drain dirty-marked: {markers!r}"

    # 2. Unmapped before the drain runs: the accepted frame never lands, so
    # the drop has to be recorded here or it is lost entirely.
    markers.clear()
    adapter.push_input_image(controller, ts_ident, object())
    adapter.push_input_image(controller, ts_ident, object())
    adapter._window_mapped = False
    assert adapter._drain_mirror(controller, ts_ident) is False
    assert markers.get(ts_ident) is True, (
        "a frame accepted into the mirror slot and then dropped by an unmap "
        f"was never dirty-marked (markers: {markers!r}) -- load_from_changes "
        "would have nothing to replay on remap"
    )

    # 3. The paint itself raising (disposed widget) is the same class of drop.
    # The slot is seeded directly so the DRAIN is the thing that fails, not
    # the push (which has its own containment).
    adapter = GtkUIAdapter()
    adapter.bind(controller, _child_with_screenbar(_RaisingImage()))
    adapter._window_mapped = True
    slot = _MirrorSlot()
    slot.offer(object())
    adapter._mirror_slots[(controller, ts_ident)] = slot

    markers.clear()
    assert adapter._drain_mirror(controller, ts_ident) is False
    assert markers.get(ts_ident) is True, (
        "a drain whose paint raised did not dirty-mark -- the frame is lost "
        "with no replay"
    )

    # 4. A drain that finds the widget gone (a rebuild between push and paint)
    # is the last drop shape the slot can produce.
    adapter = GtkUIAdapter()
    adapter.bind(controller, SimpleNamespace())
    adapter._window_mapped = True
    slot = _MirrorSlot()
    slot.offer(object())
    adapter._mirror_slots[(controller, ts_ident)] = slot

    markers.clear()
    assert adapter._drain_mirror(controller, ts_ident) is False
    assert markers.get(ts_ident) is True, (
        "a drain that could no longer resolve its widget did not dirty-mark"
    )
    print("PASS: mirror-drain drops (unmap + raise + missing widget) reach the marker dict")


def check_key_paint_drop(controller, key_ident) -> None:
    from src.windows.mainWindow.elements.KeyGrid import KeyButton

    markers = controller.ui_image_changes_while_hidden

    def make_stand_in(mapped: bool, image):
        stand_in = SimpleNamespace(
            identifier=key_ident,
            key_grid=SimpleNamespace(deck_controller=controller),
            pixbuf=None,
            image=image,
            get_mapped=lambda: mapped,
            # Main-loop-only sidebar mirror; headless it returns immediately,
            # and it is not what this scenario is about.
            set_icon_selector_previews=lambda pixbuf: None,
        )
        stand_in._mark_dropped = lambda: KeyButton._mark_dropped(stand_in)
        return stand_in

    # 1. Mapped: the paint lands and nothing is marked.
    markers.clear()
    painted = []
    ok = make_stand_in(True, SimpleNamespace(set_from_pixbuf=painted.append))
    assert KeyButton.paint_mirror_frame(ok, object()) is False, (
        "a GLib idle callback that returns truthy re-arms forever"
    )
    assert len(painted) == 1, "the mapped button did not paint"
    assert not markers, f"a successful key paint dirty-marked: {markers!r}"

    # 2. Unmapped between queue and run -- the mapped-guard drop.
    markers.clear()
    assert KeyButton.paint_mirror_frame(make_stand_in(False, None), object()) is False
    assert markers.get(key_ident) is True, (
        "KeyButton.paint_mirror_frame dropped a frame on its mapped-guard "
        f"without dirty-marking (markers: {markers!r}) -- push_input_image "
        "already answered True for it, so nothing else will ever record it"
    )

    # 3. The paint itself raising (disposed widget) is the same class of drop.
    markers.clear()

    def boom(_pixbuf):
        raise RuntimeError("widget was disposed")

    KeyButton.paint_mirror_frame(
        make_stand_in(True, SimpleNamespace(set_from_pixbuf=boom)), object())
    assert markers.get(key_ident) is True, (
        "a raising key paint did not dirty-mark"
    )
    print("PASS: KeyButton.paint_mirror_frame drops (unmapped + raise) reach the marker dict")


def check_mark_dirty_survives_a_dead_controller() -> None:
    """mark_dirty runs on the main loop, off the engine's call stack: it must
    never raise into a GLib callback for a controller torn down mid-flight."""

    class _Dead:
        @property
        def ui_image_changes_while_hidden(self):
            raise AttributeError("controller was closed")

    mark_dirty(_Dead(), Input.Key("0x0"))
    print("PASS: mark_dirty contains a torn-down controller")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_ui_dirty_marker_channel")
    ui_port.install(AcceptingPort())
    controller = fixtures.make_headless_controller(serial="dirty-marker-1")
    try:
        key_ident = controller.inputs[Input.Key][0].identifier
        ts_ident = controller.inputs[Input.Touchscreen][0].identifier

        check_touchscreen_drain_drops(controller, ts_ident)
        check_key_paint_drop(controller, key_ident)
        check_mark_dirty_survives_a_dead_controller()
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)

    print("PASS: scenario_ui_dirty_marker_channel")


if __name__ == "__main__":
    main()
