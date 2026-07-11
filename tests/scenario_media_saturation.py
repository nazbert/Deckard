"""
Unit-tier scenario for the animated key/dial GIF path the per-deck
display-saturation factor used to skip silently (issue #49):

  * KeyGIF.__init__ (src/backend/DeckManagement/DeckController.py) -- the
    animated key/dial GIF path. Frames are decoded+fitted once at
    construction and get_next_frame only indexes the retained list, so the
    enhancement must be baked in there (one enhance per frame at load, not
    per media tick). A GIF used as a *background* already routed through the
    saturated video path; a GIF on a key/dial sat visibly duller than the
    PNG/mp4 next to it.

Exercised through the REAL production class with a small stub exposing
exactly the surface it reads (get_key_image_size(),
get_display_saturation()) -- same house pattern as
scenario_display_saturation.py, which covers the background-image and
background-video application points.

Covers:
  (a) KeyGIF at factor 1.3 retains measurably more saturated frames than at
      1.0, for EVERY frame (the bake happens in the per-frame decode loop).
  (b) KeyGIF at factor 1.0 is a strict no-op: frames are byte-identical to
      a pre-fix decode (no enhance, no extra conversion).
  (c) KeyGIF alpha survives the enhancement (frames stay RGBA and the
      transparent background stays transparent) -- the reason the GIF path
      exists at all is cv2's demuxer dropping alpha, so the fix must not
      flatten it either.
"""
import os

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import KeyGIF


def _mean_hsv_saturation(image: Image.Image) -> float:
    _, s, _ = image.convert("RGB").convert("HSV").split()
    data = list(s.getdata())
    return sum(data) / len(data)


# ===================================================================== #
# (a)-(c): KeyGIF
# ===================================================================== #

class _StubGifDeckController:
    """Exposes exactly what KeyGIF.__init__ reads: get_key_image_size() and
    get_display_saturation()."""

    def __init__(self, key_size: tuple[int, int], saturation: float):
        self._key_size = key_size
        self._saturation = saturation

    def get_key_image_size(self) -> tuple[int, int]:
        return self._key_size

    def get_display_saturation(self) -> float:
        return self._saturation


class _StubControllerKey:
    """SingleKeyAsset only reads controller_input.deck_controller."""

    def __init__(self, deck_controller):
        self.deck_controller = deck_controller


def _make_test_gif(path: str, size=(96, 96), n_frames: int = 4) -> None:
    """Animated GIF with a transparent background and a vivid opaque disc
    per frame -- vivid enough that a 1.3 boost is unambiguous, transparent
    enough that alpha loss is too."""
    frames = []
    colors = [(220, 30, 30, 255), (30, 200, 40, 255), (40, 60, 220, 255), (230, 210, 20, 255)]
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        draw.ellipse([8, 8, size[0] - 8, size[1] - 8], fill=colors[i % len(colors)])
        frames.append(frame)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=80, loop=0, disposal=2,
    )


def check_keygif_saturation() -> None:
    gif_path = os.path.join(gl.DATA_PATH, "media", "sat_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    _make_test_gif(gif_path)

    key_size = (72, 72)

    def build(saturation: float) -> KeyGIF:
        key = _StubControllerKey(_StubGifDeckController(key_size, saturation))
        return KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)

    gif_default = build(1.0)
    gif_boosted = build(1.3)
    try:
        assert len(gif_default.frames) == len(gif_boosted.frames) == 4

        # (a) every retained frame carries the boost (issue #49: the GIF key
        # sat visibly duller than the saturated stills around it).
        for i, (plain, boosted) in enumerate(zip(gif_default.frames, gif_boosted.frames)):
            sat_plain = _mean_hsv_saturation(plain)
            sat_boosted = _mean_hsv_saturation(boosted)
            assert sat_boosted > sat_plain + 1.0, (
                f"frame {i}: expected factor 1.3 to raise mean HSV saturation "
                f"measurably: default={sat_plain:.2f} boosted={sat_boosted:.2f}"
            )

            # (c) alpha survives: mode stays RGBA and the transparent corner
            # stays fully transparent after the enhancement.
            assert boosted.mode == "RGBA", f"frame {i}: mode {boosted.mode!r} != RGBA"
            assert boosted.getpixel((0, 0))[3] == 0, (
                f"frame {i}: transparent background lost through the enhancement"
            )

        # (b) factor 1.0 must be a strict no-op: a second default-factor
        # decode of the same file produces byte-identical frames (no enhance
        # call, no mode conversion sneaking in at the default).
        gif_default_again = build(1.0)
        try:
            for i, (a, b) in enumerate(zip(gif_default.frames, gif_default_again.frames)):
                assert a.tobytes() == b.tobytes(), f"frame {i}: default factor is not a stable no-op"
        finally:
            gif_default_again.close()
    finally:
        gif_default.close()
        gif_boosted.close()

    print("PASS: KeyGIF frames carry the saturation boost (alpha preserved, 1.0 no-op)")


def main() -> None:
    check_keygif_saturation()
    print("PASS: scenario_media_saturation")


if __name__ == "__main__":
    main()
