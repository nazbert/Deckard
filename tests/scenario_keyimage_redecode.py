"""InputImage must not re-decode from disk on every composite.

A source smaller than the ask can never satisfy the check, so the native size
is memoized. A swapped-out image is dropped, not closed, for live readers.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import os
import threading
import types

from PIL import Image

import globals as gl
from fixtures import start_watchdog

import src.backend.DeckManagement.Subclasses.KeyImage as keyimage_mod
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage


class StubInput:
    """Just enough ControllerInput for InputImage.

    Saturation, an active state with a composed layout, and the tile size.
    """

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


def leg_concurrent_swap() -> int:
    """Two threads on one InputImage, a compositor and a resizer.

    The compositor reads pixels off the reference get_raw_image() hands it,
    as the resize of add_image_to_background does, while the resizer forces
    genuine re-decode swaps. An in-flight composite must always complete.
    """
    big_path = os.path.join(gl.DATA_PATH, "concurrent_src.png")
    # A large source, so every re-decode yields a fresh, still-open image the
    # compositor can be caught reading.
    Image.new("RGBA", (512, 512), (40, 90, 160, 255)).save(big_path)

    stub = StubInput(layout_size=1.0)
    with Image.open(big_path) as im:
        key_image = InputImage(stub, im.convert("RGBA").resize((64, 64)),
                               path=big_path)

    errors: list[str] = []
    stop = threading.Event()

    def reseed_for_next_swap():
        # Re-arm the swap path. Shrink the retained copy and forget the
        # memoized native size, so the next get_raw_image() re-decodes and
        # swaps again. The clamp would otherwise settle after one decode.
        key_image.image = key_image.image.resize((64, 64))
        key_image._source_native_size = None

    def compositor():
        try:
            while not stop.is_set():
                img = key_image.get_raw_image()
                if img is None:
                    continue
                # Touch the pixels the same way the real composite does. On a
                # closed image this raises ValueError.
                img.tobytes()
                img.resize((32, 32))
        except Exception as e:  # noqa: BLE001 -- the point is to catch it
            errors.append(f"compositor: {type(e).__name__}: {e}")

    def resizer():
        try:
            for _ in range(400):
                if stop.is_set():
                    break
                stub._layout.size = 6.0        # ask for more than the 64px copy
                key_image.get_raw_image()      # triggers the re-decode swap
                stub._layout.size = 1.0
                reseed_for_next_swap()
        except Exception as e:  # noqa: BLE001
            errors.append(f"resizer: {type(e).__name__}: {e}")
        finally:
            stop.set()

    threads = [threading.Thread(target=compositor, name="compositor"),
               threading.Thread(target=resizer, name="resizer")]
    for t in threads:
        t.start()
    # The resizer sets stop after its 400 swaps. Bound the join defensively.
    for t in threads:
        t.join(timeout=20)
    stop.set()
    for t in threads:
        t.join(timeout=5)

    alive = [t.name for t in threads if t.is_alive()]
    if alive:
        print(f"FAIL(3): threads did not finish (deadlock/hang?): {alive}")
        return 1
    closed_use = [e for e in errors if "closed image" in e]
    if closed_use:
        print(f"FAIL(3): a composite operated on a closed image under the "
              f"concurrent swap: {closed_use[0]}")
        return 1
    if errors:
        print(f"FAIL(3): unexpected error under the concurrent swap: {errors[0]}")
        return 1
    print("PASS: concurrent composite + re-decode swap never touches a closed image")
    return 0


def main() -> int:
    start_watchdog(30, "keyimage_redecode")

    # A 64x64 source is smaller than one 72 px tile, so any layout size of 1 or
    # more asks for more than the source can ever deliver.
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

    # 2. Hand out a reference, force a swap, then use the reference.
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

    # 3. The real cross-thread hazard. Checks 1 and 2 are single-threaded.
    return leg_concurrent_swap()


if __name__ == "__main__":
    raise SystemExit(main())
