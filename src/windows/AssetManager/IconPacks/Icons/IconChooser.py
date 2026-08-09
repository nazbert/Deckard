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
from src.windows.AssetManager.IconPacks.Icons.IconFlowBox import WallpaperFlowBox
from src.windows.AssetManager.IconPacks.Icons.IconPreview import IconPreview

# Import python modules

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.IconPackManagement.Icon import Icon
    from src.backend.IconPackManagement.IconPack import IconPack


class IconChooserPage(GenericAssetChooserPage):
    # NOTE: IconFlowBox.py's class is (mis)named WallpaperFlowBox upstream.
    FLOW_BOX_CLASS = WallpaperFlowBox
    PREVIEW_CLASS = IconPreview

    def get_assets(self, pack: "IconPack") -> list:
        return pack.get_icons()

    def bind_preview(self, preview: IconPreview, icon: "Icon") -> None:
        preview.set_icon(icon)

    def get_child_asset(self, child) -> "Icon":
        return child.icon

    def on_build_finished(self) -> None:
        # The icon stack gates deferred show_for_path tasks on BOTH its
        # pages' build_finished flags.
        self.stack.on_load_finished()
