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
from src.windows.AssetManager.SDPlusBarWallpaperPacks.FlowBox import SDPlusBarWallpaperPackFlowBox
from src.windows.AssetManager.SDPlusBarWallpaperPacks.Preview import SDPlusBarWallpaperPackPreview

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperChooser import SDPlusBarWallpaperChooserPage


class SDPlusBarWallpaperPackChooser(GenericPackChooserPage):
    PACK_FLOW_BOX_CLASS = SDPlusBarWallpaperPackFlowBox
    PACK_PREVIEW_CLASS = SDPlusBarWallpaperPackPreview
    LEAF_CHILD_NAME = "wallpaper-chooser"

    def get_packs(self) -> dict:
        return gl.sd_plus_bar_wallpaper_pack_manager.get_wallpaper_packs()

    def get_leaf_chooser(self) -> "SDPlusBarWallpaperChooserPage":
        return self.stack.wallpaper_chooser
