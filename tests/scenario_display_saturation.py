"""Unit-tier scenario for the per-deck display-saturation feature.

Drives BackgroundImage and BackgroundVideoCache at their real enhancement
points. Factor 1.0 is a strict no-op, and the cache filename carries .satNNN.
"""
import os

import fixtures  # noqa: F401  (sets up an isolated data dir before any `src`/`globals` import)

import cv2
import numpy as np
from PIL import Image

import globals as gl
from src.backend.DeckManagement.DeckController import BackgroundImage
from src.backend.DeckManagement.Subclasses.background_video_cache import BackgroundVideoCache


# BackgroundImage

class _StubImageDeckController:
    """Exposes exactly what BackgroundImage.__init__ reads."""

    def __init__(self, saturation: float):
        self._saturation = saturation

    def get_display_saturation(self) -> float:
        return self._saturation


def _make_colorful_image(size=(64, 64)) -> Image.Image:
    # Vivid horizontal color bands. A low-chroma photo could hide a modest
    # saturation change in rounding; these bands make it unambiguous.
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    bands = [(220, 30, 30), (30, 200, 40), (40, 60, 220), (230, 210, 20)]
    band_h = size[1] // len(bands)
    for i, color in enumerate(bands):
        arr[i * band_h:(i + 1) * band_h, :, :] = color
    return Image.fromarray(arr, mode="RGB")


def _mean_hsv_saturation(image: Image.Image) -> float:
    _, s, _ = image.convert("RGB").convert("HSV").split()
    data = list(s.getdata())
    return sum(data) / len(data)


def check_background_image() -> None:
    source = _make_colorful_image()

    bg_default = BackgroundImage(_StubImageDeckController(1.0), source)
    bg_boosted = BackgroundImage(_StubImageDeckController(1.3), source.copy())

    # Factor 1.0 must be a strict no-op. No ImageEnhance call, no mode
    # conversion and no copy, so the same object comes back out.
    assert bg_default.image is source, (
        "factor 1.0 must skip enhancement entirely: BackgroundImage.image "
        "should be the original object, not a copy or a converted image"
    )

    # Factor 1.3 must measurably raise mean HSV saturation against the
    # untouched default-factor image.
    sat_default = _mean_hsv_saturation(bg_default.image)
    sat_boosted = _mean_hsv_saturation(bg_boosted.image)
    assert sat_boosted > sat_default + 1.0, (
        f"expected factor 1.3 to raise mean HSV saturation measurably: "
        f"default={sat_default:.2f} boosted={sat_boosted:.2f}"
    )

    print(f"PASS: BackgroundImage saturation (default={sat_default:.2f}, boosted={sat_boosted:.2f})")


# BackgroundVideoCache naming and sweeper hash-stem compatibility

class _StubDeck:
    def key_layout(self):
        return (1, 2)

    def key_count(self):
        return 2

    def key_image_format(self):
        return {"size": (32, 32)}

    def is_touch(self):
        return False


class _StubVideoDeckController:
    """Exposes what BackgroundVideoCache.__init__ and _generate_alpha_frame read.

    Those are get_display_saturation(), .deck, .key_spacing and
    generate_alpha_key().
    """

    def __init__(self, saturation: float):
        self._saturation = saturation
        self.deck = _StubDeck()
        self.key_spacing = (4, 4)

    def get_display_saturation(self) -> float:
        return self._saturation

    def generate_alpha_key(self):
        return Image.new("RGBA", (32, 32), (0, 0, 0, 0))


def _make_test_video(path: str, n_frames: int = 5, size=(64, 64), color=(30, 80, 200)) -> None:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    frame[:, :] = color
    for _ in range(n_frames):
        writer.write(frame)
    writer.release()


def check_background_video_cache() -> None:
    # A real StubSettingsManager is needed, because BackgroundVideoCache.__init__
    # reads performance.cache-videos through get_app_settings().
    fixtures.install_stub_globals()

    video_path = os.path.join(gl.DATA_PATH, "sat_test_source.mp4")
    _make_test_video(video_path)

    cache_default = BackgroundVideoCache(
        video_path, deck_controller=_StubVideoDeckController(1.0), extend_touchscreen=False
    )
    cache_boosted = BackgroundVideoCache(
        video_path, deck_controller=_StubVideoDeckController(1.3), extend_touchscreen=False
    )
    try:
        md5 = cache_default.video_md5
        assert md5 == cache_boosted.video_md5, "both caches are built from the same source file"

        default_name = os.path.basename(cache_default.cache_path)
        boosted_name = os.path.basename(cache_boosted.cache_path)

        assert default_name == f"{md5}.mp4", (
            f"factor 1.0 must keep today's plain cache filename, got {default_name!r}"
        )
        assert boosted_name == f"{md5}.sat130.mp4", (
            f"factor 1.3 must carry the two-decimal-encoded suffix, got {boosted_name!r}"
        )

        # sweep_stale_video_caches computes entry_hash = entry.split(".")[0] on
        # every cache directory entry before it dispatches on file type, to check
        # it against the md5s still referenced by deck and page settings. Run
        # that inline expression unmodified and require both real filenames to
        # resolve back to the source md5.
        assert default_name.split(".")[0] == md5
        assert boosted_name.split(".")[0] == md5

        # Sanity. The build path runs end to end and produces a measurably more
        # saturated frame, so the suffix is not the only thing that changed and
        # the enhancement really is baked into the cached canvas.
        default_tile = cache_default.get_tiles(0)[0]
        boosted_tile = cache_boosted.get_tiles(0)[0]
        sat_default = _mean_hsv_saturation(default_tile)
        sat_boosted = _mean_hsv_saturation(boosted_tile)
        assert sat_boosted > sat_default + 1.0, (
            f"expected the cached canvas to carry the saturation boost: "
            f"default={sat_default:.2f} boosted={sat_boosted:.2f}"
        )
    finally:
        cache_default.close()
        cache_boosted.close()

    print(f"PASS: BackgroundVideoCache naming ({default_name!r} / {boosted_name!r}) + sweeper hash-stem compatibility")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_display_saturation")
    check_background_image()
    check_background_video_cache()
    print("PASS: scenario_display_saturation")


if __name__ == "__main__":
    main()
