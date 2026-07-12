"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from src.backend.DeckManagement.Subclasses.SingleKeyAsset import SingleKeyAsset
from PIL import Image, ImageEnhance

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import ControllerInput

class InputImage(SingleKeyAsset):
    # mem-plan P2.4: static media used to retain the source-resolution RGBA
    # forever (design doc §3.2 -- "tens of MB for photos"). The stock UI
    # caps ImageLayout.size at 200% (ImageEditor.py's SizeRow, a
    # SpinButton(0, 200, 1)), so 2x the tile size is the largest composed
    # size a well-behaved layout ever asks for; this constant sizes the
    # one-time fit-at-load below (fit budget = 2x THAT, i.e. 4x tile, so a
    # 200% layout never has to re-decode). It is NOT enforced as a hard
    # cap: plugins call set_action_layout()/set_media(size=...) directly
    # with an unvalidated float and can do so after this constructor
    # already ran -- see _ensure_fits_composed(), which re-decodes from
    # `path` if a later composed layout asks for more than what got kept.
    MAX_LAYOUT_SCALE = 2.0

    def __init__(self, controller_input: "ControllerInput", image: Image.Image, path: str = None):
        """
        Initialize the class with the given controller key, image, fill mode, size, vertical alignment, and horizontal alignment.

        Parameters:
            controller_key (ControllerKey): The key of the controller.
            image (Image.Image): The image to be displayed.
            path (str, optional): The source file `image` was decoded from,
                if any. None for plugin-supplied in-memory images and SVG
                thumbnails (no cheap way to re-decode those at a higher
                resolution). Kept so a later composed layout that needs more
                resolution than the fitted copy retains can re-decode from
                source instead of upscaling a blurry copy.
            fill_mode (str, optional): The mode for filling the image. Defaults to "cover".
            size (float, optional): The size of the image. Defaults to 1.
            valign (float, optional): The vertical alignment of the image. Defaults to 0. Ranges from -1 to 1.
            halign (float, optional): The horizontal alignment of the image. Defaults to 0. Ranges from -1 to 1.
        """
        super().__init__(controller_input)
        image = image.convert("RGBA")

        # One-time load-point enhancement: this constructor runs once per
        # page/state (re)load, well before per-frame label compositing, and
        # covers both key and dial static media (both go through this same
        # class). At the default factor the enhance call is skipped, so the
        # stored image is identical to today's. Applied to the raw media
        # layer only -- the caller composites labels on top of get_raw_image()
        # afterwards, so text is never re-tinted. Stored (not just applied)
        # because _ensure_fits_composed() may need to reapply it to a fresh
        # decode later.
        self._saturation = self.deck_controller.get_display_saturation()
        if abs(self._saturation - 1.0) > 0.001:
            image = ImageEnhance.Color(image).enhance(self._saturation)

        self.path = path
        # Native size of the source file, captured on the first re-decode in
        # _ensure_fits_composed(). None until then (the constructor's `image`
        # may already be a fitted copy, so its size is not the source's).
        self._source_native_size = None
        self.image = self._fit_to_budget(image)

        if self.image is None:
            self.image = self.controller_input.get_empty_background()

    def _budget_size(self) -> "tuple[int, int] | None":
        """The largest resolution this class will retain without a later
        on-demand re-decode. None means "no visual target" (e.g. a dial
        input_image on a non-touch deck, get_image_size() == (0, 0)) --
        fitting to a near-zero budget there would be a real regression, not
        a memory win, so those are left unfit."""
        tile_w, tile_h = self.controller_input.get_image_size()
        if tile_w <= 0 or tile_h <= 0:
            return None
        return (
            int(tile_w * self.MAX_LAYOUT_SCALE * 2),
            int(tile_h * self.MAX_LAYOUT_SCALE * 2),
        )

    def _fit_to_budget(self, image: Image.Image) -> Image.Image:
        budget = self._budget_size()
        if budget is None:
            return image
        if image.width > budget[0] or image.height > budget[1]:
            # thumbnail() mutates in place, preserves aspect ratio, and is a
            # no-op if the image already fits -- safe to call unconditionally,
            # the width/height guard above just avoids the call+draft-probe
            # overhead in the (default) common case.
            image.thumbnail(budget, Image.Resampling.LANCZOS)
        return image

    def _ensure_fits_composed(self) -> None:
        """Re-decodes from `path` if the CURRENT composed layout (which may
        have changed since __init__ via set_action_layout()/set_media() --
        both take an unvalidated `size` float with no upper bound enforced)
        asks for more resolution than the retained image has. No-op when
        there's no source file to fall back to (in-memory/SVG images) --
        those keep today's behavior of upscaling the retained copy at
        composite time."""
        if not self.path:
            return
        if not hasattr(self, "image") or self.image is None:
            return
        active_state = self.controller_input.get_active_state()
        if active_state is None:
            return
        tile_w, tile_h = self.controller_input.get_image_size()
        if tile_w <= 0 or tile_h <= 0:
            return
        layout = active_state.layout_manager.get_composed_layout()
        size = layout.size if layout.size is not None else 1
        needed_w = int(tile_w * max(size, 0))
        needed_h = int(tile_h * max(size, 0))
        # Clamp the ask to what the source can actually deliver: when the
        # source is smaller than the composed size (any 64px icon on 72px
        # tiles, any image whose fitted minor dim is sub-tile), no re-decode
        # can ever satisfy it -- without the clamp every composite re-ran
        # Image.open + convert + enhance from disk (per-frame disk I/O on
        # background-video pages, B-03).
        if self._source_native_size is not None:
            needed_w = min(needed_w, self._source_native_size[0])
            needed_h = min(needed_h, self._source_native_size[1])
        if needed_w <= self.image.width and needed_h <= self.image.height:
            return

        try:
            with Image.open(self.path) as fresh:
                native_size = fresh.size
                fresh = fresh.convert("RGBA")
        except (OSError, FileNotFoundError):
            return
        self._source_native_size = native_size

        if abs(self._saturation - 1.0) > 0.001:
            fresh = ImageEnhance.Color(fresh).enhance(self._saturation)

        # Re-fit against a budget sized to the layout that actually asked
        # for more, not the constructor's fixed 200% assumption -- a plugin
        # requesting e.g. 500% shouldn't force full source resolution on
        # every subsequent tick, but the retained copy should still track
        # what's actually live with the same 2x headroom as the initial fit.
        budget = (needed_w * 2, needed_h * 2)
        if fresh.width > budget[0] or fresh.height > budget[1]:
            fresh.thumbnail(budget, Image.Resampling.LANCZOS)

        # The swapped-out image is deliberately NOT closed: the media thread
        # may still be compositing the reference get_raw_image() handed it
        # (closing raised 'Operation on closed image' under load, B-03).
        # Dropping the reference is the stated policy -- it is collected once
        # the last composite releases it.
        self.image = fresh

    def get_raw_image(self) -> Image.Image:
        if not hasattr(self, "image") or self.image is None:
            return
        self._ensure_fits_composed()
        return self.image

    def close(self) -> None:
        if not hasattr(self, "image"):
            # Already closed
            return
        self.image.close()
        self.image = None
        del self.image
        return
