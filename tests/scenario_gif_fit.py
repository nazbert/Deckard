"""
Unit-tier scenario (docs/memory-footprint-impl-plan.md P2.3): KeyGIF must
fit every decoded frame to at most 2x the key tile size at construction,
instead of retaining full source-resolution RGBA frames forever (design doc
§3.2: "41-200MB per GIF key").

Drives the REAL KeyGIF.__init__ against a synthetic on-disk GIF (unlike
scenario_gif_timeline.py, which bypasses __init__ entirely to unit-test the
wall-clock frame-picking arithmetic in isolation).

Covers:
  (a) a GIF much larger than the tile size ends up with every retained
      frame at or below 2x the tile size in both dimensions.
  (b) alpha survives the decode+fit round trip (the reason this stays a PIL
      frame list instead of routing through Mp4FrameCache -- cv2's GIF
      demuxer drops alpha; see the design doc's alpha-probe note).
  (c) a GIF already smaller than the 2x budget keeps its source dimensions
      (shrink-only, same policy as P2.4's static images: upscaling a small
      GIF to the budget would multiply its retained memory -- 40px -> 144px
      is 13x per frame -- for zero display benefit, since compositing
      scales per-tick anyway).
  (d) the source GIF file handle is closed once decoding finishes (no
      dangling fd kept alive behind the fitted frames) -- checked via
      close()'s cleanup and by confirming `self.gif` is no longer retained
      as an attribute (mem-plan P2.3: "close the source PIL handle").
"""
import os

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import KeyGIF


class _StubDeckController:
    """Exposes exactly what KeyGIF.__init__ reads: get_key_image_size()."""

    def __init__(self, key_size: tuple[int, int]):
        self._key_size = key_size

    def get_key_image_size(self) -> tuple[int, int]:
        return self._key_size


class _StubControllerKey:
    """SingleKeyAsset only reads controller_input.deck_controller."""

    def __init__(self, key_size: tuple[int, int]):
        self.deck_controller = _StubDeckController(key_size)


def _make_test_gif(path: str, size=(320, 320), n_frames: int = 6) -> None:
    """A small animated GIF with a fully transparent background and an
    opaque colored disc that shifts each frame -- large enough (well above
    2x any of this test's tile sizes) to exercise the fit, and with real
    alpha so the alpha-preservation assertion has something to check."""
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 20 + i * 5
        draw.ellipse([x0, 40, x0 + 180, 220], fill=(220, 30, 30, 255))
        frames.append(frame)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=80, loop=0, disposal=2,
    )


def check_large_gif_is_fit() -> None:
    gif_path = os.path.join(gl.DATA_PATH, "media", "large_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    _make_test_gif(gif_path, size=(320, 320), n_frames=6)

    tile_size = (72, 72)
    budget = (tile_size[0] * 2, tile_size[1] * 2)  # KeyGIF.MAX budget == 2x tile

    key = _StubControllerKey(tile_size)
    gif = KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)

    try:
        assert len(gif.frames) == 6, f"expected 6 decoded frames, got {len(gif.frames)}"

        for i, frame in enumerate(gif.frames):
            assert frame.width <= budget[0] and frame.height <= budget[1], (
                f"frame {i}: {frame.size} exceeds the 2x-tile budget {budget}"
            )
            # The source (320x320) is well above the budget, so the fit must
            # have actually done something, not left it at source res.
            assert frame.width < 320 and frame.height < 320, (
                f"frame {i}: {frame.size} was not downsized from the 320x320 source"
            )
            assert frame.mode == "RGBA", f"frame {i}: expected RGBA, got {frame.mode}"

        # (b) alpha preserved: the disc is opaque, the surrounding
        # background is fully transparent -- both must still be present
        # after decode+fit, on every frame.
        for i, frame in enumerate(gif.frames):
            alphas = frame.getchannel("A").getextrema()
            assert alphas[0] == 0, f"frame {i}: fully-transparent background did not survive fitting (min alpha {alphas[0]})"
            assert alphas[1] == 255, f"frame {i}: opaque disc did not survive fitting (max alpha {alphas[1]})"

        # (d) the source file handle must not be retained behind the fitted
        # frames -- KeyGIF no longer keeps a `self.gif` attribute at all
        # once construction finishes.
        assert not hasattr(gif, "gif"), "KeyGIF must not retain the source PIL handle after decoding"

        print(f"PASS: large GIF (320x320) fit to <= {budget} per frame, alpha preserved, source handle released")
    finally:
        gif.close()


def check_small_gif_keeps_source_size() -> None:
    gif_path = os.path.join(gl.DATA_PATH, "media", "small_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    _make_test_gif(gif_path, size=(40, 40), n_frames=3)

    tile_size = (72, 72)  # budget = 144x144, well above the 40x40 source
    budget = (tile_size[0] * 2, tile_size[1] * 2)
    key = _StubControllerKey(tile_size)
    gif = KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)

    try:
        for i, frame in enumerate(gif.frames):
            assert frame.size == (40, 40), (
                f"frame {i}: a smaller-than-budget source must keep its own "
                f"dimensions (shrink-only fit), got {frame.size}"
            )
        print(f"PASS: small GIF (40x40) kept at source size (budget {budget} not forced)")
    finally:
        gif.close()


def main() -> None:
    check_large_gif_is_fit()
    check_small_gif_keeps_source_size()
    print("PASS: scenario_gif_fit")


if __name__ == "__main__":
    main()
