"""
Screensaver media must LOOP when the config has no explicit `loop` key
(issue #204).

`load_screensaver` used to apply `config.get("loop", False)` while every
media-layer default (ScreenSaver.loop, Background.set_from_path /
prebuild_from_path, BackgroundVideo/GifBackground) says True, so any
screensaver config written before the loop toggle existed -- or by a
programmatic writer that omitted the key -- played exactly one pass and
then held its last frame on the device for the whole idle window. Field
evidence: the #196 hardware run captured one 1.2s pass followed by
hash-skip-only frozen ticks.

Covers:
  * page-level screensaver config with no `loop` key -> ScreenSaver.loop True;
  * deck-level screensaver config with no `loop` key -> same (the other
    branch of load_screensaver's config selection);
  * an EXPLICIT `"loop": false` still wins -- the default change must not
    make the toggle unclickable;
  * the defaulted value is threaded all the way into the live background
    provider (GifBackground.loop), not just parked on the ScreenSaver.
"""
import json
import os

import fixtures
import globals as gl
from fixtures import teardown, wait_until

from PIL import Image, ImageDraw


def _make_gif(path: str, n_frames: int = 4) -> str:
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        draw.ellipse([8 + i * 4, 12, 36 + i * 4, 40], fill=(200, 40, 60, 255))
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=2,
    )
    return path


def _seed_screensaver_page(page_name: str, media_path: str, loop=None) -> str:
    """Page JSON whose screensaver block omits `loop` entirely unless one is
    passed -- the whole point of the scenario is the ABSENT key."""
    screensaver = {
        "overwrite": True,
        "enable": True,
        "media-path": media_path,
        "time-delay": 60,
        "fps": 30,
        "brightness": 30,
    }
    if loop is not None:
        screensaver["loop"] = loop

    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {}, "dials": {}, "touchscreens": {},
            "settings": {"screensaver": screensaver},
        }, f)
    return path


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_screensaver_loop_default")
    gif = _make_gif(os.path.join(gl.DATA_PATH, "media", "saver_loop.gif"))

    controller = fixtures.make_headless_controller(serial="ss-loop-1")
    try:
        # --- page config, no `loop` key ---------------------------------
        no_key_path = _seed_screensaver_page("SaverNoLoopKey", gif)
        page = gl.page_manager.get_page(no_key_path, controller)
        controller.load_page(page, allow_reload=True)

        assert controller.screen_saver.loop is True, (
            "a screensaver config without a `loop` key must loop -- "
            f"got loop={controller.screen_saver.loop!r} (one pass then a "
            "frozen last frame for the whole idle window)"
        )
        assert controller.screen_saver.media_path == gif, (
            "fixture sanity: the page's screensaver config was not applied"
        )

        # --- the value reaches the live provider ------------------------
        controller.screen_saver.show()
        assert wait_until(lambda: controller.background.video is not None, timeout=5), (
            "the screensaver's GIF background never landed"
        )
        video = controller.background.video
        assert video.loop is True, (
            f"the defaulted loop must be threaded into the background "
            f"provider, got {type(video).__name__}.loop={video.loop!r}"
        )
        controller.screen_saver.hide()
        assert wait_until(lambda: not controller.screen_saver.showing, timeout=5)

        # --- explicit False still wins ----------------------------------
        off_path = _seed_screensaver_page("SaverLoopOff", gif, loop=False)
        off_page = gl.page_manager.get_page(off_path, controller)
        controller.load_page(off_page, allow_reload=True)

        assert controller.screen_saver.loop is False, (
            "an explicit `\"loop\": false` must still disable looping -- "
            "the default must not override a persisted toggle"
        )

        # --- deck config, no `loop` key ---------------------------------
        # The deck branch is chosen when deck settings enable the screensaver
        # and the page does NOT overwrite it, so the page seeded here is a
        # plain one.
        deck_settings = controller.get_deck_settings()
        deck_settings["screensaver"] = {
            "enable": True,
            "media-path": gif,
            "time-delay": 60,
            "fps": 30,
            "brightness": 30,
        }
        gl.settings_manager.save_deck_settings(controller.serial_number(), deck_settings)

        plain_path = fixtures.seed_page("SaverDeckLevel")
        plain_page = gl.page_manager.get_page(plain_path, controller)
        controller.load_page(plain_page, allow_reload=True)

        assert controller.screen_saver.loop is True, (
            "a DECK-level screensaver config without a `loop` key must loop "
            f"too -- got loop={controller.screen_saver.loop!r}"
        )

        print("PASS: scenario_screensaver_loop_default")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
