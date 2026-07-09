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
    def __init__(self, controller_input: "ControllerInput", image: Image.Image):
        """
        Initialize the class with the given controller key, image, fill mode, size, vertical alignment, and horizontal alignment.

        Parameters:
            controller_key (ControllerKey): The key of the controller.
            image (Image.Image): The image to be displayed.
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
        # afterwards, so text is never re-tinted.
        saturation = self.deck_controller.get_display_saturation()
        if abs(saturation - 1.0) > 0.001:
            image = ImageEnhance.Color(image).enhance(saturation)

        self.image = image

        if self.image is None:
            self.image = self.controller_input.get_empty_background()

    def get_raw_image(self) -> Image.Image:
        if not hasattr(self, "image"):
            return
        return self.image
    
    def close(self) -> None:
        if not hasattr(self, "image"):
            # Already closed
            return
        self.image.close()
        self.image = None
        del self.image
        return