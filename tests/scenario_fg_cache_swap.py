"""LayoutManager._fg_cache must not serve a stale resized foreground.

InputImage._ensure_fits_composed() swaps its image in place, so the asset
object stays identical while its pixels change. The layout key must notice.
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
    """Stands in for the InputImage cache_token.

    Its backing image can be swapped in place, exactly as
    _ensure_fits_composed() does. It is only ever compared by identity.
    """

    def __init__(self, image: Image.Image):
        self.image = image


def _make_layout_manager() -> LayoutManager:
    # add_image_to_background only reaches get_composed_layout(). Give the
    # action_layout every field, so inject_defaults never touches the
    # controller_input identifier. controller_input is otherwise unused here,
    # because the resized foreground depends on the asset and the layout alone.
    controller_input = types.SimpleNamespace(identifier=None)
    lm = LayoutManager(controller_input)
    lm.action_layout = ImageLayout(valign=0, halign=0, fill_mode="stretch", size=1.0)
    return lm


def _dominant(img: Image.Image) -> tuple:
    """The single solid colour of a flat image, read at the centre pixel."""
    return img.convert("RGBA").getpixel((img.width // 2, img.height // 2))


def main() -> int:
    start_watchdog(30, "fg_cache_swap")

    lm = _make_layout_manager()
    background = Image.new("RGBA", (72, 72), (0, 0, 0, 0))

    # Both source images are the same size, so the composed pixel size, and with
    # it the unhardened layout key, is identical across the swap. The only
    # difference the cache can key on is the image itself.
    red_src = Image.new("RGBA", (144, 144), RED)
    green_src = Image.new("RGBA", (144, 144), GREEN)

    asset = _FakeAsset(red_src)

    # 1. The first composite is red and populates _fg_cache for this pair.
    out1 = lm.add_image_to_background(asset.image, background, cache_token=asset)
    if _dominant(out1) != RED:
        print(f"FAIL(setup): first composite is not RED: {_dominant(out1)}")
        return 1

    # 2. Swap the backing image of the asset in place, keeping the same asset
    #    object, the same layout and a new source of the same size. That is what
    #    an _ensure_fits_composed() re-decode does, without the size growth.
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
