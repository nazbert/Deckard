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
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

# Import python modules

# Import own modules
from src.windows.AssetManager.GenericAssetChooser import GenericPackChooserPage
from src.windows.AssetManager.WallpaperPacks.FlowBox import WallpaperPackFlowBox
from src.windows.AssetManager.WallpaperPacks.Preview import WallpaperPackPreview

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser import WallpaperChooserPage


class WallpaperPackChooser(GenericPackChooserPage):
    PACK_FLOW_BOX_CLASS = WallpaperPackFlowBox
    PACK_PREVIEW_CLASS = WallpaperPackPreview
    LEAF_CHILD_NAME = "wallpaper-chooser"

    def get_packs(self) -> dict:
        return gl.wallpaper_pack_manager.get_wallpaper_packs()

    def get_leaf_chooser(self) -> "WallpaperChooserPage":
        return self.stack.wallpaper_chooser
