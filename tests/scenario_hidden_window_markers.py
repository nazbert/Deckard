"""ui_image_changes_while_hidden must store dirty markers, not composites.

The null UI port makes push_input_image return False, so every input is in
the nothing-is-showing state and the marker path is directly exercisable.
"""

import fixtures
from PIL import Image

from src.backend.DeckManagement.InputIdentifier import Input

WATCHDOG_SECONDS = 30


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_hidden_window_markers")

    controller = fixtures.make_headless_controller(serial="p54-1")
    try:
        key_inputs = controller.inputs[Input.Key]
        touchscreen_inputs = controller.inputs[Input.Touchscreen]
        assert key_inputs, "fixture sanity: expected at least one key input"
        assert touchscreen_inputs, "fixture sanity: expected a touchscreen input (FakeDeck.is_touch() is always True)"

        tasks = controller.ui_image_changes_while_hidden

        # The initial page load marks every key and the touchscreen dirty, with
        # a marker and never a PIL image.
        def all_inputs_marked():
            return (
                all(tasks.get(i.identifier) is not None for i in key_inputs)
                and tasks.get(touchscreen_inputs[0].identifier) is not None
            )

        assert fixtures.wait_until(all_inputs_marked, timeout=5), (
            "not all inputs were marked dirty after the initial page load"
        )

        for i in key_inputs:
            value = tasks[i.identifier]
            assert value is True, f"expected a True marker for {i.identifier}, got {value!r}"
            assert not isinstance(value, Image.Image), (
                f"{i.identifier}'s dirty entry must be a marker, not a stashed PIL image"
            )

        touchscreen_identifier = touchscreen_inputs[0].identifier
        ts_value = tasks[touchscreen_identifier]
        assert ts_value is True, f"expected a True marker for the touchscreen, got {ts_value!r}"
        assert not isinstance(ts_value, Image.Image)

        print("PASS: initial hidden-window paint stores markers, not PIL images")

        # Repeated ticks, which model a video background rewriting every input
        # 20 to 30 times a second while hidden, must never leave a PIL image
        # behind. Every value observed is always the marker.
        sample_key = key_inputs[0]
        for _ in range(10):
            sample_key.update(force=True)
            value = tasks.get(sample_key.identifier)
            assert value is True, f"expected the marker to stay True across repeated hidden ticks, got {value!r}"
            assert not isinstance(value, Image.Image)

        touchscreen_inputs[0].update()
        ts_value = tasks.get(touchscreen_identifier)
        assert ts_value is True
        assert not isinstance(ts_value, Image.Image)

        print("PASS: repeated hidden-window ticks never store a PIL image")

        # get_current_image() is a pure composite with no side effects, so
        # calling it again with nothing changed must reproduce the same pixels.
        # The map-time recompose then shows the current frame rather than
        # something stale.
        img1 = sample_key.get_current_image()
        hash1 = hash(img1.tobytes())
        img1.close()
        img2 = sample_key.get_current_image()
        hash2 = hash(img2.tobytes())
        img2.close()
        assert hash1 == hash2, "get_current_image() must be deterministic across repeated calls with no state change"

        ts_img1 = touchscreen_inputs[0].get_current_image()
        ts_hash1 = hash(ts_img1.tobytes())
        ts_img1.close()
        ts_img2 = touchscreen_inputs[0].get_current_image()
        ts_hash2 = hash(ts_img2.tobytes())
        ts_img2.close()
        assert ts_hash1 == ts_hash2, "touchscreen get_current_image() must be deterministic too"

        print("PASS: get_current_image() is a deterministic, side-effect-free recompose accessor")

        # The consume-on-map contract. A read and a pop leave no residue, for
        # both Key and Touchscreen identifiers, so a Touchscreen marker is just
        # as consumable as a Key one.
        for i in key_inputs:
            assert i.identifier in tasks
            recomposed = i.get_current_image()
            assert recomposed is not None
            recomposed.close()
            tasks.pop(i.identifier)
            assert i.identifier not in tasks

        assert touchscreen_identifier in tasks
        recomposed = touchscreen_inputs[0].get_current_image()
        assert recomposed is not None
        recomposed.close()
        tasks.pop(touchscreen_identifier)
        assert touchscreen_identifier not in tasks

        # Popping twice, when the KeyGrid fallback path races the ScreenBar
        # consumption, must be safe. Both sites guard with try and except
        # KeyError, never a raw dict subscript.
        try:
            tasks.pop(touchscreen_identifier)
            raised = False
        except KeyError:
            raised = True
        assert raised, "fixture sanity: popping an already-popped key should raise KeyError (guarded at the call sites)"

        print("PASS: dirty markers are fully consumable on map for both Key and Touchscreen identifiers")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_hidden_window_markers")


if __name__ == "__main__":
    main()
