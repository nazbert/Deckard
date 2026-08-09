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
from src.windows.AssetManager.IconPacks.FlowBox import IconPackFlowBox
from src.windows.AssetManager.IconPacks.Preview import IconPackPreview

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.IconPacks.Icons.IconChooser import IconChooserPage


class IconPackChooser(GenericPackChooserPage):
    PACK_FLOW_BOX_CLASS = IconPackFlowBox
    PACK_PREVIEW_CLASS = IconPackPreview
    LEAF_CHILD_NAME = "icon-chooser"

    def get_packs(self) -> dict:
        if gl.icon_pack_manager is None:
            # Boot order: the window cannot be opened before the manager
            # exists, but the type says it may be absent.
            return {}
        return gl.icon_pack_manager.get_icon_packs()

    def get_leaf_chooser(self) -> "IconChooserPage":
        return self.stack.icon_chooser

    def on_build_finished(self) -> None:
        # The icon stack gates deferred show_for_path tasks on BOTH its
        # pages' build_finished flags.
        self.stack.on_load_finished()
