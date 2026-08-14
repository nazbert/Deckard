"""
Regression test for the per-touchscreen background video path.

A video assigned as the SD+ touchscreen background must play. A real
DeckController over a fake SD+ drives it.
"""

# The state holds an InputVideo over a strip-sized shared frame cache, the
# media tick re-composites the strip while it is set, and the dual-hash dedup
# gates the device writes.
import os
import time

import fixtures
import globals as gl
from loguru import logger as log

from src.backend.DeckManagement.InputIdentifier import Input


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_touchscreen_video_bg")

    error_logs: list[str] = []
    log.add(lambda m: error_logs.append(str(m)), level="ERROR",
            filter=lambda r: "background" in r["message"].lower())

    video_path = fixtures.make_test_mp4(os.path.join(fixtures.DATA_DIR, "assets", "ts_bg.mp4"))
    image_path = fixtures.make_test_png(
        os.path.join(fixtures.DATA_DIR, "assets", "ts_bg.png"),
        size=(200, 100), color=(0, 200, 30),
    )
    corrupt_path = os.path.join(fixtures.DATA_DIR, "assets", "corrupt.mp4")
    with open(corrupt_path, "wb") as f:
        f.write(b"not really a video")

    controller = fixtures.make_headless_controller(serial="ts-video-1")
    try:
        page = controller.active_page
        ident = Input.Touchscreen("sd-plus")
        touch = controller.get_input(ident)
        state = touch.get_active_state()
        deck = fixtures.raw_deck(controller)

        # 1. A video assignment composites a frame of the video.
        page.set_background_image(identifier=ident, state=0, path=video_path, update=True)
        img = None
        for _ in range(3):
            img = state.get_current_image()
        px = img.convert("RGB").getpixel((img.width // 2, img.height // 2))
        assert abs(px[0] - 128) <= 40 and abs(px[1] - 64) <= 40, (
            f"video frame not painted: center pixel {px}, expected R~128 G~64"
        )

        # 2. Playback. The media tick alone must keep pushing new strip
        # frames, with distinct payload hashes. An identical frame is
        # dedup-skipped, and a static background writes about once.
        seq_before = deck.current_seq()
        got = fixtures.wait_until(
            lambda: len({op[4] for op in deck.ops_after(seq_before)
                         if op[2] == "set_touchscreen_image"}) >= 3,
            timeout=5.0,
        )
        distinct = {op[4] for op in deck.ops_after(seq_before)
                    if op[2] == "set_touchscreen_image"}
        assert got, (
            f"expected >=3 distinct tick-driven strip writes within 5s, got "
            f"{len(distinct)} -- the background video is not playing"
        )

        # 3. A decodable video produces no error log.
        assert len(error_logs) == 0, f"unexpected background errors: {error_logs}"

        # 3b. The loop and fps page settings reach the playing video. The
        # sidebar rows persist through these setters.
        assert page.get_background_loop(identifier=ident, state=0) is True
        assert page.get_background_fps(identifier=ident, state=0) == 30
        assert state.background_video.fps == 30
        page.set_background_fps(identifier=ident, state=0, fps=15, update=True)
        page.set_background_loop(identifier=ident, state=0, loop=False, update=True)
        state.get_current_image()
        assert state.background_video.fps == 15, (
            f"configured fps must reach the playing video, got {state.background_video.fps}"
        )
        assert state.background_video.loop is False
        page.set_background_loop(identifier=ident, state=0, loop=True, update=True)

        # 3c. The fps setting is a render cap rather than a playback rate.
        # The video runs 30 frames at a native 15fps, with the blue channel
        # at frame times eight. Capped at 5, natural-speed playback still
        # covers most of the cycle in 1.8s, which a playback rate would not.
        page.set_background_fps(identifier=ident, state=0, fps=5, update=True)
        assert fixtures.wait_until(
            lambda: state.background_video is not None
            and state.background_video.video_cache is not None
            and state.background_video.video_cache.is_cache_complete(),
            timeout=10.0,
        ), "strip frame cache never completed"
        blues = []
        deadline = time.time() + 1.8
        while time.time() < deadline:
            frame_img = state.get_current_image()
            blues.append(frame_img.convert("RGB").getpixel((frame_img.width // 2, frame_img.height // 2))[2])
            time.sleep(0.1)
        span = max(blues) - min(blues)
        assert span >= 120, (
            f"playback speed appears tied to the fps cap: blue span {span} over "
            f"1.8s at cap=5 (natural 15fps should traverse most of 0..232)"
        )
        # The cap must hold even though this loop drives composites directly,
        # standing in for a deck background video that re-triggers the strip.
        # The quantized picker hands out at most cap times 1.8 distinct
        # frames, where an uncapped 15fps sampled at 10Hz would give about 18.
        distinct = len(set(blues))
        assert distinct <= 14, (
            f"fps cap not applied at the picker: {distinct} distinct frames "
            f"observed over 1.8s at cap=5 (expected ~9)"
        )
        page.set_background_fps(identifier=ident, state=0, fps=30, update=True)

        # 4. A switch to a plain image still renders and detaches the video.
        page.set_background_image(identifier=ident, state=0, path=image_path, update=True)
        for _ in range(3):
            img = state.get_current_image()
        px = img.convert("RGB").getpixel((img.width // 2, img.height // 2))
        expected = (0, 200, 30)
        assert all(abs(a - b) <= 10 for a, b in zip(px, expected)), (
            f"image background regressed: center pixel {px}, expected ~{expected}"
        )
        assert state.background_video is None, (
            "video cache reader must be released when the background is no longer a video"
        )

        # 5. A corrupt file logs at most once, not once per composite.
        error_logs.clear()
        page.set_background_image(identifier=ident, state=0, path=corrupt_path, update=True)
        for _ in range(10):
            state.get_current_image()
        time.sleep(0.2)
        assert len(error_logs) <= 1, (
            f"corrupt background must log at most once, got {len(error_logs)}"
        )

        # 6. The sidebar preview helper, which builds no widget.
        from src.backend.MediaManager import MediaManager
        gl.media_manager = MediaManager()
        from src.windows.mainWindow.elements.Sidebar.elements.BackgroundEditor import build_preview_pixbuf
        assert build_preview_pixbuf(video_path) is not None, (
            "video paths must resolve to a thumbnail pixbuf for the preview"
        )
        assert build_preview_pixbuf(image_path) is None, (
            "image paths must return None (set_filename renders them directly)"
        )
        assert build_preview_pixbuf(None) is None

        print("scenario_touchscreen_video_bg: PASS")
    finally:
        fixtures.teardown(controller)


if __name__ == "__main__":
    main()
