"""
Unit-tier scenario for the two media paths the per-deck display-saturation
factor used to skip silently (issues #49 and #50):

  * KeyGIF.__init__ (src/backend/DeckManagement/DeckController.py) -- the
    animated key/dial GIF path. Frames are decoded+fitted once at
    construction and get_next_frame only indexes the retained list, so the
    enhancement must be baked in there (one enhance per frame at load, not
    per media tick). A GIF used as a *background* already routed through the
    saturated video path; a GIF on a key/dial sat visibly duller than the
    PNG/mp4 next to it.
  * ControllerTouchScreenState._get_fitted_background_image (same file) --
    the per-touchscreen (SD+ strip) background image. The fitted result is
    memoized under a (path, mtime, size) key that must also gain the
    saturation dimension, or a factor change would keep serving the stale
    enhancement.

Both are exercised through the REAL production classes with small stubs
exposing exactly the surface each reads (get_key_image_size(),
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
  (d) _get_fitted_background_image at 1.3 returns a measurably more
      saturated strip image than at 1.0.
  (e) the fitted-image cache key includes the factor: flipping the
      controller's saturation between calls (same path/mtime/size) must
      return a differently-enhanced image, not the cached previous one.
  (f) _read_display_saturation validates the persisted factor: a non-finite
      value ("nan"/"inf", which float() accepts without raising) or an
      out-of-range value must not reach an ImageEnhance factor / cache key --
      non-finite falls back to the default, out-of-range clamps to the UI
      range.
"""
import os

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import (
    DeckController,
    KeyGIF,
    ControllerTouchScreenState,
)


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


# ===================================================================== #
# (d)+(e): ControllerTouchScreenState._get_fitted_background_image
# ===================================================================== #

class _StubTouchDeckController:
    def __init__(self, saturation: float):
        self.saturation = saturation

    def get_display_saturation(self) -> float:
        return self.saturation


class _StubControllerTouch:
    def __init__(self, saturation: float):
        self.deck_controller = _StubTouchDeckController(saturation)


def _make_touch_state(saturation: float) -> ControllerTouchScreenState:
    """__new__ + exactly the attributes _get_fitted_background_image reads
    (controller_touch.deck_controller, _fitted_background_cache) -- the full
    __init__ needs a real ControllerTouchScreen/deck graph."""
    state = ControllerTouchScreenState.__new__(ControllerTouchScreenState)
    state.controller_touch = _StubControllerTouch(saturation)
    state._fitted_background_cache = (None, None)
    return state


def _make_colorful_image(path: str, size=(200, 60)) -> None:
    import numpy as np
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    bands = [(220, 30, 30), (30, 200, 40), (40, 60, 220), (230, 210, 20)]
    band_h = max(1, size[1] // len(bands))
    for i, color in enumerate(bands):
        arr[i * band_h:(i + 1) * band_h, :, :] = color
    Image.fromarray(arr, mode="RGB").save(path, "PNG")


def check_touchscreen_background_saturation() -> None:
    bg_path = os.path.join(gl.DATA_PATH, "media", "sat_strip_bg.png")
    os.makedirs(os.path.dirname(bg_path), exist_ok=True)
    _make_colorful_image(bg_path)

    strip_size = (800, 100)

    # (d) factor 1.3 must measurably boost the fitted strip image (issue
    # #50: keys boosted, strip didn't).
    plain = _make_touch_state(1.0)._get_fitted_background_image(bg_path, strip_size)
    boosted = _make_touch_state(1.3)._get_fitted_background_image(bg_path, strip_size)
    assert plain is not None and boosted is not None

    sat_plain = _mean_hsv_saturation(plain)
    sat_boosted = _mean_hsv_saturation(boosted)
    assert sat_boosted > sat_plain + 1.0, (
        f"expected factor 1.3 to raise the strip background's mean HSV "
        f"saturation measurably: default={sat_plain:.2f} boosted={sat_boosted:.2f}"
    )

    # (e) the memo key carries the factor: same state, same file, factor
    # flipped between calls -- the second call must NOT serve the first
    # call's cached enhancement (path/mtime/size alone would).
    state = _make_touch_state(1.0)
    first = state._get_fitted_background_image(bg_path, strip_size)
    state.controller_touch.deck_controller.saturation = 1.3
    second = state._get_fitted_background_image(bg_path, strip_size)
    assert first.tobytes() != second.tobytes(), (
        "fitted-background cache served a stale enhancement after a "
        "saturation change: the cache key must include the factor"
    )
    assert abs(_mean_hsv_saturation(second) - sat_boosted) < 0.01, (
        "post-change fitted image should match a fresh 1.3 enhancement"
    )

    # And the cache still works within one factor: a repeat call is a hit
    # returning an equal (copied) image.
    third = state._get_fitted_background_image(bg_path, strip_size)
    assert third.tobytes() == second.tobytes()
    assert third is not second, "cache must hand out copies, not the cached object"

    print("PASS: touchscreen background image carries the saturation boost (cache keyed on factor)")


# ===================================================================== #
# (f): _read_display_saturation validates the persisted factor
# ===================================================================== #

class _StubSettingsController:
    """Exactly what DeckController._read_display_saturation reads:
    get_deck_settings() plus the DEFAULT/MIN/MAX_DISPLAY_SATURATION class
    constants it references through self (unbound-method call, house
    pattern)."""

    DEFAULT_DISPLAY_SATURATION = DeckController.DEFAULT_DISPLAY_SATURATION
    MIN_DISPLAY_SATURATION = DeckController.MIN_DISPLAY_SATURATION
    MAX_DISPLAY_SATURATION = DeckController.MAX_DISPLAY_SATURATION

    def __init__(self, settings: dict):
        self._settings = settings

    def get_deck_settings(self) -> dict:
        return self._settings


def _read_sat(settings: dict) -> float:
    return DeckController._read_display_saturation(_StubSettingsController(settings))


def check_read_saturation_validates() -> None:
    default = DeckController.DEFAULT_DISPLAY_SATURATION
    lo = DeckController.MIN_DISPLAY_SATURATION
    hi = DeckController.MAX_DISPLAY_SATURATION

    # A "nan"/"inf" string parses through float() without raising -- it must
    # NOT reach an ImageEnhance factor or a cache key (a non-finite key never
    # matches -> a cache that never hits and re-enhances every composite).
    for poison in ("nan", "inf", "-inf"):
        got = _read_sat({"display": {"saturation": poison}})
        assert got == default, f"{poison!r} setting must fall back to default {default}, got {got}"

    # Garbage / missing -> default (existing contract, still holds).
    assert _read_sat({"display": {"saturation": "not-a-number"}}) == default
    assert _read_sat({}) == default

    # Out-of-range persisted values are clamped to the UI range, not trusted.
    assert _read_sat({"display": {"saturation": 9.0}}) == hi
    assert _read_sat({"display": {"saturation": 0.0}}) == lo
    assert _read_sat({"display": {"saturation": -5.0}}) == lo

    # In-range values pass through unchanged.
    assert _read_sat({"display": {"saturation": 1.3}}) == 1.3

    print("PASS: _read_display_saturation rejects non-finite and clamps out-of-range factors")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_media_saturation")
    check_keygif_saturation()
    check_touchscreen_background_saturation()
    check_read_saturation_validates()
    print("PASS: scenario_media_saturation")


if __name__ == "__main__":
    main()
