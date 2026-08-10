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

# Re-exports, not local dependencies: DeckController does
# `from src.backend.DeckManagement.ImageHelpers import *` and then uses
# ImageOps and PILHelper as bare names without importing either itself.
from PIL import ImageOps  # noqa: F401
from StreamDeck.ImageHelpers import PILHelper  # noqa: F401

# GLib/GdkPixbuf are imported lazily inside image2pixbuf: it is their
# only consumer and every one of its callers lives under src/windows/, so a
# module-level import would drag the widget stack into the engine's import
# closure (ImageHelpers is core to the render path).


def is_transparent(img: Image.Image):
    """
    Determines if an image has transparency.

    Args:
        img (PIL.Image.Image): The image to check for transparency.

    Returns:
        bool: True if the image has transparency, False otherwise.
    """
    return img.has_transparency_data


def image2pixbuf(img, force_transparency=False):
    """
    Converts an image to a GdkPixbuf.Pixbuf object.

    Args:
        img (PIL.Image.Image): The image to convert.

    Returns:
        GdkPixbuf.Pixbuf: The converted GdkPixbuf.Pixbuf object.
    """
    from gi.repository import GLib, GdkPixbuf

    img = img.convert("RGBA")
    force_transparency = True

    data = img.tobytes()
    w, h = img.size
    data = GLib.Bytes.new(data)
    transparent = True if force_transparency else is_transparent(img)
    channels = 4 if transparent else 3

    try:
        pix = GdkPixbuf.Pixbuf.new_from_bytes(data, GdkPixbuf.Colorspace.RGB,
                transparent, 8, w, h, w * channels)
        # Clean up memory
        data = None
        w, h = None, None
        del data
        return pix
    except TypeError as e:
         # This usually happens if the image is a non RGB image
         return