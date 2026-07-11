"""
Regression test for the per-touchscreen background video path.

A video assigned as the SD+ touchscreen background must PLAY: the state holds
an InputVideo over a strip-sized shared frame cache (mp4_tile_cache), the
media tick re-composites the strip while it is set
(ControllerTouchScreen.on_media_player_tick + MediaPlayerThread's tick
predicate), and the existing dual-hash dedup gates the device writes.

Asserts, over a REAL DeckController (live MediaPlayerThread) on a fake SD+:
  1. assigning a .mp4 as the touchscreen background raises nowhere and the
     composite contains a frame of the video (pixel probe),
  2. PLAYBACK: with no other animated content, the media tick alone pushes
     multiple DISTINCT strip writes to the device (journal payload hashes),
  3. a decodable video produces zero background error logs,
  4. plain image backgrounds still render (no regression) and the video's
     cache reader is released on the switch away from a video,
  5. a corrupt "video" logs at most once across many composites,
  6. the sidebar preview helper resolves videos to a thumbnail pixbuf and
     images to None (Gtk.Picture.set_filename handles those directly).
"""
import os
import time

import fixtures
import globals as gl
from loguru import logger as log

from src.backend.DeckManagement.InputIdentifier import Input


def make_test_mp4(path: str, size=(200, 100), n_frames=30) -> str:
    import cv2
    import numpy as np
    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 15, size)
    for i in range(n_frames):
        # frame i is solid BGR(i*8, 64, 128) == RGB(128, 64, i*8):
        # red/green fixed across the video, blue varies per frame.
        frame = np.full((size[1], size[0], 3), (i * 8 % 255, 64, 128), dtype=np.uint8)
        writer.write(frame)
    writer.release()
    assert os.path.getsize(path) > 0
    return path


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_touchscreen_video_bg")

    error_logs: list[str] = []
    log.add(lambda m: error_logs.append(str(m)), level="ERROR",
            filter=lambda r: "background" in r["message"].lower())

    video_path = make_test_mp4(os.path.join(fixtures.DATA_DIR, "assets", "ts_bg.mp4"))
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

        # 1: video assignment composites a frame of the video
        page.set_background_image(identifier=ident, state=0, path=video_path, update=True)
        img = None
        for _ in range(3):
            img = state.get_current_image()
        px = img.convert("RGB").getpixel((img.width // 2, img.height // 2))
        assert abs(px[0] - 128) <= 40 and abs(px[1] - 64) <= 40, (
            f"video frame not painted: center pixel {px}, expected R~128 G~64"
        )

        # 2: playback -- the media tick alone must keep pushing NEW strip
        # frames to the device (distinct payload hashes; identical frames
        # would be dedup-skipped, a static background would write ~once).
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

        # 3: no error logs for a decodable video
        assert len(error_logs) == 0, f"unexpected background errors: {error_logs}"

        # 3b: loop/fps page settings reach the playing video (sidebar rows
        # persist through these setters; defaults are loop=True, fps=30)
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

        # 3c: the fps setting is a RENDER cap, not a playback rate. The test
        # video is 30 frames at native 15fps (one loop every 2s), blue channel
        # == frame*8. With fps capped at 5, natural-speed playback still
        # traverses most of the cycle within ~1.8s (blue span >= 120); if fps
        # were the playback rate, 1.8s at 5fps would cover only ~9 frames
        # (span ~72).
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
        # The cap must hold even though THIS loop drives composites directly
        # (standing in for a deck bg video / dial animation re-triggering the
        # strip): the quantized picker may hand out at most ~cap*1.8 distinct
        # frames. Uncapped native 15fps sampled at 10Hz would give ~18.
        distinct = len(set(blues))
        assert distinct <= 14, (
            f"fps cap not applied at the picker: {distinct} distinct frames "
            f"observed over 1.8s at cap=5 (expected ~9)"
        )
        page.set_background_fps(identifier=ident, state=0, fps=30, update=True)

        # 4: switching to a plain image still renders and detaches the video
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

        # 5: a corrupt file logs at most once, not once per composite
        error_logs.clear()
        page.set_background_image(identifier=ident, state=0, path=corrupt_path, update=True)
        for _ in range(10):
            state.get_current_image()
        time.sleep(0.2)
        assert len(error_logs) <= 1, (
            f"corrupt background must log at most once, got {len(error_logs)}"
        )

        # 6: the sidebar preview helper (no widgets involved -- safe headless)
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
