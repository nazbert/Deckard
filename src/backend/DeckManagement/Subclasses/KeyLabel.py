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
from src.backend.DeckManagement import font_resolver
from PIL import ImageFont
from dataclasses import dataclass
from functools import lru_cache
from fontTools.ttLib import TTFont

import globals as gl


@lru_cache(maxsize=128)
def _load_font(font_path: str, font_size: int, encoding: str) -> ImageFont.FreeTypeFont:
    # ImageFont.truetype re-reads the file and re-parses the FreeType face on
    # every call. The label rasterization itself is now cached per
    # composed label (LabelManager._draw_static_label), so this is no longer
    # on the per-frame path -- but get_font() is still called per label per
    # composite to build that cache's key, and the scroll path measures
    # through it too.
    return ImageFont.truetype(font_path, font_size, encoding=encoding)


@lru_cache(maxsize=128)
def _is_symbol_font(font_path: str) -> bool:
    """Check if font uses symbol encoding (e.g., Webdings, Wingdings).
    
    Symbol fonts have a cmap table with platformID=3 (Windows) and 
    platEncID=0 (Symbol encoding). Results are cached.
    """
    try:
        font = TTFont(font_path)
        for table in font['cmap'].tables:
            if table.platformID == 3 and table.platEncID == 0:
                return True
        return False
    except Exception:
        return False


def _find_font_path(font_name: str | None, font_weight: int | None, style: str | None) -> str | None:
    # font_resolver.resolve() is itself lru_cache'd on these same attributes
    # (size doesn't affect which file is picked, so it isn't part of the key).
    # None comes back only when fontconfig is unreachable entirely.
    return font_resolver.resolve(font_name, font_weight, style)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import ControllerInput

@dataclass
class KeyLabel:
    # The owning input, whatever kind it is: dial labels pass a
    # ControllerDial and LabelManager itself is typed on the base, so the
    # honest declaration is the shared base rather than ControllerKey.
    # Nothing in this class touches it -- it is carried, not used.
    controller_input: "ControllerInput"
    text: str | None = None
    font_size: int | None = None
    font_name: str | None = None
    font_weight: int | None = None
    style: str | None = None # normal, oblique, italic
    color: list[int] | None = None
    outline_width: int | None = None
    outline_color: list[int] | None = None
    alignment: str | None = None  # left, center, right

    def get_font_path(self) -> str | None:
        font_name = self.font_name
        if font_name is None or font_name == "":
            font_name = gl.fallback_font

        return _find_font_path(font_name, self.font_weight, self.style)

    def clear_values(self):
        self.text = None
        self.font_size = None
        self.font_name = None
        self.font_weight = None
        self.style = None
        self.color = None
        self.outline_width = None
        self.outline_color = None
        self.alignment = None

    def get_font(self) -> ImageFont.FreeTypeFont:
        font_path = self.get_font_path()
        font_size = self.font_size
        if font_path is None or font_size is None:
            # Rasterizing needs both a resolved file and a size. font_size is
            # filled in by DeckController.inject_defaults before anything
            # renders; a missing path means fontconfig could not be reached at
            # all (no libfontconfig AND no fc-match binary). Either way there
            # is nothing to load -- say so here rather than failing inside
            # PIL's truetype loader.
            raise RuntimeError(
                f"cannot load a font for this label (path={font_path!r}, size={font_size!r})")
        encoding = "symb" if _is_symbol_font(font_path) else "unic"
        return _load_font(font_path, font_size, encoding)