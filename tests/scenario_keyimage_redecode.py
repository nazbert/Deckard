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
  3. Under the REAL cross-thread race -- one thread compositing (reading
     pixels off) get_raw_image()'s reference while another drives re-decode
     swaps on the same InputImage -- no composite ever operates on a closed
     image. This is the media-thread-composite vs UI-sync-re-decode hazard
     the fix targets (get_current_image at DeckController.py:3782/4024 on the
     media thread vs update_all_inputs' preview sync at :1113 on another
     thread, on a background-video page). Checks 1/2 prove the policy
     single-threaded; this proves it holds under contention. Pre-fix:
     'Operation on closed image' from the compositor mid-read.
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


def leg_concurrent_swap() -> int:
    """Two threads on ONE InputImage: a compositor that reads pixels off the
    reference get_raw_image() hands it (as add_image_to_background's resize
    does), and a resizer that keeps forcing genuine re-decode swaps. Pre-fix
    the resizer's swap closed the image the compositor was mid-read on
    ('Operation on closed image'); post-fix the swapped-out image is dropped,
    not closed, so an in-flight composite always completes."""
    big_path = os.path.join(gl.DATA_PATH, "concurrent_src.png")
    # A large source so every re-decode yields a fresh, still-open image the
    # compositor can be caught reading.
    Image.new("RGBA", (512, 512), (40, 90, 160, 255)).save(big_path)

    stub = StubInput(layout_size=1.0)
    with Image.open(big_path) as im:
        key_image = InputImage(stub, im.convert("RGBA").resize((64, 64)),
                               path=big_path)

    errors: list[str] = []
    stop = threading.Event()

    def reseed_for_next_swap():
        # Re-arm the swap path: shrink the retained copy and forget the
        # memoized native size so the next get_raw_image() re-decodes and
        # swaps again (the clamp would otherwise settle after one decode).
        key_image.image = key_image.image.resize((64, 64))
        key_image._source_native_size = None

    def compositor():
        try:
            while not stop.is_set():
                img = key_image.get_raw_image()
                if img is None:
                    continue
                # Touch the pixels the same way the real composite does; on a
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
    # The resizer sets stop after its 400 swaps; bound the join defensively.
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

    # 3) the real cross-thread hazard (checks 1/2 are single-threaded).
    return leg_concurrent_swap()


if __name__ == "__main__":
    raise SystemExit(main())
