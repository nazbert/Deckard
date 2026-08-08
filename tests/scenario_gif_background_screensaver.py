"""
Integration-tier scenario (issue #196, phase 2): a GIF screensaver
background exercises the same GifBackground branch as a page background.

ScreenSaver.show()/set_media_path() reach into
Background.prebuild_from_path/set_from_path, so the .gif divert must hold
there too -- this is the regression guard for the ScreenSaver.py reach-ins
(a refactor that moved the branch out of prebuild_from_path would silently
put screensaver GIFs back on the alpha-dropping cv2 path).

Covers:
  * show() with a GIF media path lands a GifBackground on the (swapped)
    screensaver background, tiles published RGBA;
  * a set_media_path() call WHILE showing (the ScreenSaver.py:80 reach-in)
    re-routes through the same branch;
  * hide() restores the page and releases the screensaver's GIF frames
    (the swap closes the provider -- ownership follows the background).
"""
import os

import fixtures
import globals as gl
from fixtures import start_watchdog, teardown, wait_until

from PIL import Image, ImageDraw


def _make_gif(path: str, color, size=(64, 64), n_frames: int = 4) -> str:
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 8 + i * 4
        draw.ellipse([x0, 12, x0 + 28, 40], fill=color)
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=2,
    )
    return path


def main() -> None:
    start_watchdog(60, label="scenario_gif_background_screensaver")
    gif_a = _make_gif(os.path.join(gl.DATA_PATH, "media", "saver_a.gif"), (200, 40, 60, 255))
    gif_b = _make_gif(os.path.join(gl.DATA_PATH, "media", "saver_b.gif"), (40, 200, 60, 255))

    controller = fixtures.make_headless_controller(serial="gif-saver-1")
    try:
        controller.screen_saver.set_media_path(gif_a)
        controller.screen_saver.set_brightness(10)
        controller.screen_saver.show()

        assert controller.screen_saver.showing, "show() must flip showing"
        video = controller.background.video
        assert type(video).__name__ == "GifBackground", (
            f"a GIF screensaver background must land a GifBackground, "
            f"got {type(video).__name__}"
        )
        assert video.video_path == gif_a
        assert video.frames and video.frames[0].mode == "RGBA", (
            "screensaver GIF frames must be RGBA"
        )
        assert wait_until(lambda: controller.background.get_identified_tile(0) is not None, timeout=5), (
            "no identified tile published for the screensaver GIF background"
        )

        # The while-showing reach-in (ScreenSaver.set_media_path ->
        # background.set_from_path) takes the same branch and swaps the
        # provider; the old one is closed by Background.set_video.
        controller.screen_saver.set_media_path(gif_b)
        swapped = controller.background.video
        assert type(swapped).__name__ == "GifBackground" and swapped.video_path == gif_b, (
            "set_media_path while showing must re-route through GifBackground"
        )
        assert not video.frames, (
            "the replaced screensaver GIF must be closed on swap "
            "(frames released with the background)"
        )

        controller.screen_saver.hide()
        assert not controller.screen_saver.showing, "hide() must flip showing off"
        # The default page has no background: the restore swaps the GIF
        # provider out (and closes it) once hide()'s load settles.
        assert wait_until(lambda: controller.background.video is None, timeout=5), (
            "hide() must restore the page's (empty) background"
        )
        assert wait_until(lambda: not swapped.frames, timeout=5), (
            "the screensaver GIF's frames must be released when the page "
            "background is restored"
        )

        print("PASS: scenario_gif_background_screensaver")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
