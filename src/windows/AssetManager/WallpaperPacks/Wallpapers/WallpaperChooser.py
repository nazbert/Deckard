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
from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperFlowBox import WallpaperFlowBox
from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperPreview import WallpaperPreview

# Import python modules

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.WallpaperPackManagement.Wallpaper import Wallpaper
    from src.backend.WallpaperPackManagement.WallpaperPack import WallpaperPack


class WallpaperChooserPage(GenericAssetChooserPage):
    FLOW_BOX_CLASS = WallpaperFlowBox
    PREVIEW_CLASS = WallpaperPreview

    def get_assets(self, pack: "WallpaperPack") -> list:
        return pack.get_wallpapers()

    def bind_preview(self, preview: WallpaperPreview, wallpaper: "Wallpaper") -> None:
        preview.set_wallpaper(wallpaper)

    def get_child_asset(self, child) -> "Wallpaper":
        return child.wallpaper
