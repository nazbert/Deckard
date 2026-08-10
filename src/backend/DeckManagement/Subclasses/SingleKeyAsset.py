"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from PIL import Image
import os

import globals as gl

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

_error_image: Image.Image | None = None

class SingleKeyAsset:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input
        self.deck_controller = controller_input.deck_controller

    def get_raw_image(self) -> Image.Image | None:
        # None is part of the contract for the hierarchy, not for this base
        # implementation: InputImage returns None once its image has been
        # closed (KeyImage.get_raw_image), so the declaration has to allow it
        # here or that override is not substitutable.
        #
        # Decode the fallback/error image once; return a copy so callers can
        # composite/close it freely.
        global _error_image
        if _error_image is None:
            # Resolved against the repo root (globals.py's directory) -- a
            # CWD-relative path breaks whenever the app is launched from
            # anywhere but the checkout root.
            path = os.path.join(gl.top_level_dir, "Assets", "images", "error.png")
            with Image.open(path) as img:
                _error_image = img.copy()
        return _error_image.copy()
    
    def close(self):
        pass