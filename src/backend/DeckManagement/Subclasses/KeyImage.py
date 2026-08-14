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
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

class InputImage(SingleKeyAsset):
    # Without a fit at load, static media retains the source-resolution RGBA
    # forever, which is tens of MB for a photo. The stock UI caps
    # ImageLayout.size at 200% (ImageEditor.py SizeRow, SpinButton(0, 200, 1)),
    # so 2x the tile size is the largest composed size a well-behaved layout
    # asks for. This constant sizes the one-time fit at load below, and the
    # fit budget is 2x it (4x the tile), so a 200% layout never re-decodes.
    # It is not a hard cap. A plugin calls set_action_layout() or
    # set_media(size=...) with an unvalidated float, and it can do so after
    # this constructor runs. _ensure_fits_composed() then re-decodes from path.
    MAX_LAYOUT_SCALE = 2.0

    def __init__(self, controller_input: "ControllerInput", image: Image.Image, path: str = None):
        """
        Initialize the class with the given controller key, image, fill mode, size, vertical alignment, and horizontal alignment.

        Parameters:
            controller_key (ControllerKey): The key of the controller.
            image (Image.Image): The image to be displayed.
            path (str, optional): The source file that image was decoded from,
                if any. None for plugin-supplied in-memory images and SVG
                thumbnails, which have no cheap higher-resolution re-decode.
                Kept so a later composed layout that needs more resolution
                than the fitted copy retains can re-decode from source instead
                of upscaling a blurry copy.
            fill_mode (str, optional): The mode for filling the image. Defaults to "cover".
            size (float, optional): The size of the image. Defaults to 1.
            valign (float, optional): The vertical alignment of the image. Defaults to 0. Ranges from -1 to 1.
            halign (float, optional): The horizontal alignment of the image. Defaults to 0. Ranges from -1 to 1.
        """
        super().__init__(controller_input)
        image = image.convert("RGBA")

        # One-time enhancement at load. This constructor runs once per page or
        # state load, well before per-frame label compositing, and covers key
        # and dial static media. At the default factor the enhance call is
        # skipped. It applies to the raw media layer only. The caller
        # composites labels on top of get_raw_image(), so text is never
        # re-tinted. Store the factor, because _ensure_fits_composed()
        # reapplies it to a fresh decode.
        self._saturation = self.deck_controller.get_display_saturation()
        if abs(self._saturation - 1.0) > 0.001:
            image = ImageEnhance.Color(image).enhance(self._saturation)

        self.path = path
        # Native size of the source file, captured on the first re-decode in
        # _ensure_fits_composed(). It is None until then, because the
        # constructor's image argument may already be a fitted copy, whose
        # size is not the source's.
        self._source_native_size = None
        # close() clears this to None and then deletes it. Every reader guards
        # on both hasattr and None.
        self.image: Image.Image | None = self._fit_to_budget(image)

        if self.image is None:
            self.image = self.controller_input.get_empty_background()

    def _budget_size(self) -> "tuple[int, int] | None":
        """The largest resolution this class retains without a later re-decode.

        None means there is no visual target, e.g. a dial input_image on a
        non-touch deck, where get_image_size() is (0, 0). A fit to a near-zero
        budget there loses resolution and saves no memory, so those stay unfit.
        """
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
            # thumbnail() mutates in place, keeps the aspect ratio, and does
            # nothing when the image already fits. The width and height guard
            # above only avoids the call and the draft probe in the common
            # case.
            image.thumbnail(budget, Image.Resampling.LANCZOS)
        return image

    def _ensure_fits_composed(self) -> None:
        """Re-decodes from path when the composed layout outgrows the image.

        set_action_layout() and set_media() can change the layout after
        __init__, and both take an unvalidated size float with no upper bound.
        Does nothing when there is no source file, e.g. an in-memory or SVG
        image. Those keep upscaling the retained copy at composite time.
        """
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
        # Clamp the ask to what the source delivers. When the source is
        # smaller than the composed size (a 64px icon on 72px tiles, or an
        # image whose fitted minor dimension is sub-tile), no re-decode can
        # satisfy it, and every composite re-runs Image.open, convert and
        # enhance from disk. That is per-frame disk I/O on background-video
        # pages.
        #
        # The native size is memoized from the first re-decode. If something
        # replaces the file at self.path with a larger image, this clamp keeps
        # serving the old resolution. A given path is a stable source, which
        # the whole re-decode-from-path fallback already relies on. A real
        # media change builds a new InputImage, not an in-place file swap.
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

        # Re-fit against a budget sized to the layout that asked for more, not
        # to the constructor's fixed 200% assumption. A plugin that requests
        # 500% must not force full source resolution on every later tick, and
        # the retained copy still tracks what is live with the same 2x
        # headroom as the initial fit.
        budget = (needed_w * 2, needed_h * 2)
        if fresh.width > budget[0] or fresh.height > budget[1]:
            fresh.thumbnail(budget, Image.Resampling.LANCZOS)

        # Do not close the swapped-out image. The media thread may still be
        # compositing the reference that get_raw_image() handed it, and a
        # close raises "Operation on closed image" under load. Drop the
        # reference instead. The collector frees it once the last composite
        # releases it.
        self.image = fresh

    def get_raw_image(self) -> Image.Image | None:
        if not hasattr(self, "image") or self.image is None:
            return None
        self._ensure_fits_composed()
        return self.image

    def close(self) -> None:
        if not hasattr(self, "image"):
            # Already closed
            return
        if self.image is not None:
            self.image.close()
        self.image = None
        del self.image
        return
