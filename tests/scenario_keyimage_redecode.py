"""
Scenario: InputImage must not re-decode from disk on every composite for
sub-tile sources, and must not close the image under concurrent readers
(issue #3 / B-03).

get_raw_image() -> _ensure_fits_composed() re-decoded when the composed
layout needed more resolution than the retained copy -- but when the SOURCE
is smaller than the ask (any 64px icon on 72px tiles at size>1), the check
could never be satisfied: every call re-ran Image.open+convert+enhance from
disk, and the swap closed the previously returned image out from under the
media thread ('Operation on closed image').

Checks (real InputImage against a stub input):
  1. Repeated get_raw_image() with an unsatisfiable ask decodes from disk at
     most once (the memoized native-size clamp); pre-fix: once per call.
  2. A reference handed out by get_raw_image() stays usable after a
     re-decode swap; pre-fix: ValueError on any operation.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os
import types

from PIL import Image

import globals as gl
from fixtures import start_watchdog

import src.backend.DeckManagement.Subclasses.KeyImage as keyimage_mod
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage


class StubInput:
    """Just enough ControllerInput for InputImage: saturation, active state
    with a composed layout, and the tile size."""

    def __init__(self, layout_size: float):
        self.deck_controller = types.SimpleNamespace(
            get_display_saturation=lambda: 1.0)
        self._layout = types.SimpleNamespace(size=layout_size)
        self._state = types.SimpleNamespace(
            layout_manager=types.SimpleNamespace(
                get_composed_layout=lambda: self._layout))

    def get_active_state(self):
        return self._state

    def get_image_size(self):
        return (72, 72)


def main() -> int:
    start_watchdog(30, "keyimage_redecode")

    # A 64x64 source: smaller than one 72px tile, so any layout size >= 1
    # asks for more than the source can ever deliver.
    src_path = os.path.join(gl.DATA_PATH, "icon64.png")
    Image.new("RGBA", (64, 64), (30, 120, 200, 255)).save(src_path)

    opens = [0]
    real_open = keyimage_mod.Image.open

    def counting_open(path, *a, **k):
        opens[0] += 1
        return real_open(path, *a, **k)

    stub = StubInput(layout_size=2.0)
    with Image.open(src_path) as im:
        key_image = InputImage(stub, im.convert("RGBA"), path=src_path)

    keyimage_mod.Image = types.SimpleNamespace(
        open=counting_open,
        Resampling=Image.Resampling,
        Image=Image.Image,
    )
    try:
        held = key_image.get_raw_image()  # may trigger the first re-decode
        for _ in range(30):
            key_image.get_raw_image()
    finally:
        keyimage_mod.Image = Image

    if opens[0] > 1:
        print(f"FAIL(1): {opens[0]} disk decodes across 31 composites of an "
              f"unsatisfiable source (expected <= 1) -- per-frame disk I/O "
              f"on background-video pages")
        return 1
    print(f"PASS: unsatisfiable source decoded {opens[0]}x across 31 composites")

    # 2) close-under-reader: hand out a reference, force a swap, use the ref.
    stub2 = StubInput(layout_size=1.0)
    big_path = os.path.join(gl.DATA_PATH, "big.png")
    Image.new("RGBA", (600, 600), (200, 30, 30, 255)).save(big_path)
    with Image.open(big_path) as im:
        key_image2 = InputImage(stub2, im.convert("RGBA").resize((80, 80)),
                                path=big_path)

    held = key_image2.get_raw_image()
    stub2._layout.size = 6.0  # now needs more than the 80px retained copy
    key_image2.get_raw_image()  # triggers the re-decode swap
    try:
        held.resize((10, 10))  # any operation on a closed image raises
    except ValueError as e:
        print(f"FAIL(2): swapped-out image was closed under the reader: {e}")
        return 1
    print("PASS: swapped-out image stays usable for in-flight composites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
