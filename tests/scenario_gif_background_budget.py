"""A GIF background over GIF_BG_BUDGET_MB must not decode into RAM.

The provider raises pre-decode and the background falls back to the cv2 mp4
path with one logged warning. Re-setting the same path keeps that fallback.
"""
import os

import fixtures
import globals as gl
from fixtures import start_watchdog, teardown, wait_until

from loguru import logger as log
from PIL import Image, ImageDraw


def _make_many_frame_gif(path: str, n_frames: int, size=(8, 8)) -> str:
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        draw.point((i % size[0], (i // size[0]) % size[1]), fill=(200, 40, 60, 255))
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=30, loop=0, disposal=2,
    )
    return path


def main() -> None:
    start_watchdog(90, label="scenario_gif_background_budget")

    controller = fixtures.make_headless_controller(serial="gif-budget-1")
    try:
        from src.backend.DeckManagement.DeckController import GIF_BG_BUDGET_MB

        # The frame count derives from the real canvas geometry, which mirrors
        # the provider estimate, so the fixture is over budget on any deck
        # layout the harness runs with and no hardcoded count can rot.
        key_rows, key_cols = controller.deck.key_layout()
        key_w, key_h = controller.deck.key_image_format()['size']
        spacing_x, spacing_y = controller.key_spacing
        canvas_w = key_w * key_cols + spacing_x * (key_cols - 1)
        canvas_h = key_h * key_rows + spacing_y * (key_rows - 1)
        budget_bytes = GIF_BG_BUDGET_MB * 1024 * 1024
        n_frames = budget_bytes // (canvas_w * canvas_h * 4) + 25

        gif_path = _make_many_frame_gif(
            os.path.join(gl.DATA_PATH, "media", "over_budget.gif"), n_frames)

        warnings: list[str] = []
        sink_id = log.add(lambda m: warnings.append(str(m)), level="WARNING")
        try:
            controller.background.set_from_path(gif_path)

            video = controller.background.video
            assert type(video).__name__ == "BackgroundVideo", (
                f"an over-budget GIF must fall back to the cv2 path "
                f"(BackgroundVideo), got {type(video).__name__}"
            )

            over_budget_warnings = [w for w in warnings if "over budget" in w]
            assert len(over_budget_warnings) == 1, (
                f"exactly one over-budget warning expected, got "
                f"{len(over_budget_warnings)}: {over_budget_warnings}"
            )

            # Playback still runs on the fallback. The media tick publishes
            # tiles, and alpha placeholders while the cv2 cache builds count,
            # because the contract is a live playback machinery with no crash.
            assert wait_until(
                lambda: len(controller.background.tiles) == controller.deck.key_count()
                and all(t is not None for t in controller.background.tiles),
                timeout=5,
            ), "the cv2 fallback never published a full tile set"

            # The keep-check. The same path again keeps the loaded fallback,
            # with no second decode attempt and no second warning.
            controller.background.set_from_path(gif_path)
            assert controller.background.video is video, (
                "re-setting the same over-budget GIF must keep the loaded "
                "cv2 fallback (keep-check), not rebuild"
            )
            over_budget_warnings = [w for w in warnings if "over budget" in w]
            assert len(over_budget_warnings) == 1, (
                f"the keep-check must prevent a second over-budget warning, "
                f"got {len(over_budget_warnings)}"
            )
        finally:
            log.remove(sink_id)

        print("PASS: scenario_gif_background_budget")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
