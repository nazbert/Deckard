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

# Import own modules
from src.windows.AssetManager.GenericAssetChooser import GenericAssetChooserPage
from src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperFlowBox import SDPlusBarWallpaperFlowBox
from src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperPreview import SDPlusBarWallpaperPreview

# Import python modules

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaper import SDPlusBarWallpaper
    from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPack import SDPlusBarWallpaperPack


class SDPlusBarWallpaperChooserPage(GenericAssetChooserPage):
    FLOW_BOX_CLASS = SDPlusBarWallpaperFlowBox
    PREVIEW_CLASS = SDPlusBarWallpaperPreview

    def get_assets(self, pack: "SDPlusBarWallpaperPack") -> list:
        return pack.get_wallpapers()

    def bind_preview(self, preview: SDPlusBarWallpaperPreview,
                     wallpaper: "SDPlusBarWallpaper") -> None:
        preview.set_wallpaper(wallpaper)

    def get_child_asset(self, child) -> "SDPlusBarWallpaper":
        return child.wallpaper
