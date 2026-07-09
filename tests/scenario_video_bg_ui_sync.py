"""
Regression test: update_all_inputs must still sync the IN-APP key previews when
there is a background video.

Before the fix, update_all_inputs() early-returned after the dials whenever
`background.video` was set ("so as not to affect the video"), skipping every
key. That protects the DEVICE video (the per-frame media loop paints the deck),
but it also skipped set_ui_key_image, so opaque keys -- whose per-frame render
the video loop ALSO skips -- were never repainted in the app. The in-app grid
then diverged from the deck: a mix of stale and black key previews on
video-background pages, while the device itself was correct.

Headless tier: gl.app is never set, so set_ui_key_image stores a dirty MARKER in
ui_image_changes_while_hidden per key -- observable proof the UI push happened
(the real GTK-side replay needs a live widget tree the harness never builds; see
scenario_hidden_window_markers).
"""
import fixtures
from src.backend.DeckManagement.InputIdentifier import Input


class _FakeBGVideo:
    """Truthy stand-in for a decoded background video (the real object is a
    BackgroundVideo). Only .close() is ever touched here, by teardown."""

    def close(self):
        pass


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_video_bg_ui_sync")
    controller = fixtures.make_headless_controller(serial="uisync-1")
    try:
        tasks = controller.ui_image_changes_while_hidden
        key_ids = {i.identifier for i in controller.inputs[Input.Key]}
        assert key_ids, "fixture sanity: expected key inputs"

        # Stand in a background video so update_all_inputs takes the
        # "don't disturb the video" branch.
        controller.background.video = _FakeBGVideo()

        tasks.clear()
        controller.update_all_inputs()

        marked = {k for k in tasks if k in key_ids}
        assert marked == key_ids, (
            f"update_all_inputs with a background video synced only "
            f"{len(marked)}/{len(key_ids)} keys' in-app previews -- the skipped "
            f"keys would stay stale/black in the app while the deck is correct "
            f"(missing: {sorted(str(k) for k in key_ids - marked)[:8]})"
        )
        print(f"PASS: all {len(key_ids)} keys' in-app previews synced despite the background video")
    finally:
        controller.background.video = None
        fixtures.teardown(controller)

    print("PASS: scenario_video_bg_ui_sync")


if __name__ == "__main__":
    main()
