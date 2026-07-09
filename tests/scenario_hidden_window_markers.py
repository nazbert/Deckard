"""
Integration scenario (docs/memory-footprint-impl-plan.md P5.4):
ui_image_changes_while_hidden stores dirty MARKERS while the main window is
hidden, not a full composited PIL image per input rewritten ~20-30x/s
against a video background.

fixtures.make_headless_controller's integration tier never sets gl.app (see
fixtures.py's own module docstring), so every real DeckController built
through it is permanently in the "window hidden/unmapped" state as far as
ControllerKey.set_ui_key_image/ControllerTouchScreen.set_ui_image are
concerned -- get_own_key_grid()/the screenbar recursive_hasattr guard both
fail closed. That makes the backend half of P5.4 directly exercisable here,
driving the REAL DeckController/ControllerKey/ControllerTouchScreen code
(not a reimplementation of it).

The GTK-side replay (KeyGrid.load_from_changes / ScreenBar.load_from_changes
actually recompositing and pushing pixels into live widgets on map) is NOT
covered here -- it needs a real GTK widget tree the harness deliberately
never builds (gl.app is never set). See the manual QA steps at the bottom of
this file's module docstring in the PR description.

Covers:
  (a) while "hidden", set_ui_key_image/set_ui_image store True (a marker),
      never a PIL Image, for both Input.Key and Input.Touchscreen
      identifiers -- across repeated ticks (the 20-30x/s churn case), not
      just the first write.
  (b) ControllerKey/ControllerTouchScreen.get_current_image() -- the
      accessor the map-time recompose is meant to use -- is a pure,
      side-effect-free composite: calling it repeatedly with nothing
      changed reproduces byte-identical pixels, so recompositing on map
      shows the same frame the device already has, not something stale or
      different.
  (c) simulating the consume-on-map contract (pop the marker after reading
      it, exactly as KeyGrid/ScreenBar's load_from_changes do) leaves no
      residue in the dict -- for both Key and Touchscreen identifiers
      (design-doc bug 48's half: a Touchscreen marker must be consumable on
      map too, not just Key ones).
"""
import time

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

        # --- (a) initial page load marks every key + the touchscreen dirty,
        # with a marker, never a PIL image. ---
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

        # --- (a, continued) repeated ticks (simulating a video background
        # rewriting every input ~20-30x/s while hidden) must never leave a
        # PIL image behind -- every value observed is always the marker. ---
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

        # --- (b) get_current_image() is a pure, side-effect-free composite:
        # calling it again with nothing changed must reproduce the exact
        # same pixels -- the map-time recompose shows the current frame, not
        # something stale or different. ---
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

        # --- (c) the consume-on-map contract: read + pop leaves no residue,
        # for BOTH Key and Touchscreen identifiers (bug 48's half -- a
        # Touchscreen marker must be just as consumable as a Key one). ---
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

        # Popping twice (KeyGrid's fallback path racing ScreenBar's own
        # consumption, or vice versa) must be safe -- both sites guard with
        # try/except KeyError, never a raw dict[key] access.
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
