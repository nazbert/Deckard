"""
Scenario: LayoutManager._fg_cache must not serve a stale resized foreground
after the source asset's backing image is swapped in place (issue #3 / B-03
follow-up, review round 1 MEDIUM).

add_image_to_background() memoizes the resized foreground keyed on the asset
object (cache_token) + a layout key. InputImage._ensure_fits_composed()
re-decodes and swaps its `image` IN PLACE (B-03): the asset object stays
identical while its pixels change. If the layout key does not distinguish
WHICH source image was resized, a composite after such a swap is served the
stale entry cached from before it.

Today a re-decode only ever GROWS the image, and the layout key already
carries the composed pixel size (driven by layout.size), so a real swap
happens to change the key too -- the bug is latent, not live. This scenario
forces the future-proof condition the coupling does not cover: a swap that
keeps the SAME layout (a same-geometry re-decode, e.g. a saturation/mtime
refresh). Under the pre-hardening key that is an unconditional stale serve;
under the hardened key (id(image)/image.size in fg_key) the swap invalidates
the entry.

Check (real LayoutManager):
  1. Composite asset with a RED source image (layout unchanged), then swap
     the asset's backing image to a GREEN one of the same size and composite
     again. The second composite must show GREEN, not the cached RED.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import types

from PIL import Image

from fixtures import start_watchdog

from src.backend.DeckManagement.DeckController import LayoutManager
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout


RED = (200, 30, 30, 255)
GREEN = (30, 200, 30, 255)


class _FakeAsset:
    """Stands in for the InputImage cache_token: an object whose backing
    `image` can be swapped in place, exactly like _ensure_fits_composed()
    does. It is only ever compared by identity (cache_token is ...), so no
    behaviour is needed."""

    def __init__(self, image: Image.Image):
        self.image = image


def _make_layout_manager() -> LayoutManager:
    # add_image_to_background only reaches get_composed_layout(); give the
    # action_layout every field so inject_defaults never touches the
    # controller_input's identifier. controller_input is otherwise unused
    # here (the resized foreground depends only on asset + layout).
    controller_input = types.SimpleNamespace(identifier=None)
    lm = LayoutManager(controller_input)
    lm.action_layout = ImageLayout(valign=0, halign=0, fill_mode="stretch", size=1.0)
    return lm


def _dominant(img: Image.Image) -> tuple:
    """The single solid colour of a flat image (center pixel)."""
    return img.convert("RGBA").getpixel((img.width // 2, img.height // 2))


def main() -> int:
    start_watchdog(30, "fg_cache_swap")

    lm = _make_layout_manager()
    background = Image.new("RGBA", (72, 72), (0, 0, 0, 0))

    # Both source images are the SAME size so the composed pixel size (and
    # thus the pre-hardening layout key) is identical across the swap -- the
    # only difference the cache can key on is the image itself.
    red_src = Image.new("RGBA", (144, 144), RED)
    green_src = Image.new("RGBA", (144, 144), GREEN)

    asset = _FakeAsset(red_src)

    # 1. First composite: RED. Populates _fg_cache for this asset+layout.
    out1 = lm.add_image_to_background(asset.image, background, cache_token=asset)
    if _dominant(out1) != RED:
        print(f"FAIL(setup): first composite is not RED: {_dominant(out1)}")
        return 1

    # 2. Swap the asset's backing image in place -- the same asset object,
    #    the same layout, a new (same-size) source. This is exactly what an
    #    _ensure_fits_composed() re-decode does, minus the size growth.
    asset.image = green_src
    out2 = lm.add_image_to_background(asset.image, background, cache_token=asset)

    got = _dominant(out2)
    if got != GREEN:
        print(f"FAIL: composite after an in-place image swap served the stale "
              f"cached foreground: got {got}, expected GREEN {GREEN} -- "
              f"_fg_cache keyed only on asset+layout, not the backing image")
        return 1

    print("PASS: _fg_cache invalidates on an in-place source-image swap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
