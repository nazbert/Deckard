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
import threading
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

# Import python modules
from loguru import logger as log

# Import own modules
from GtkHelper.GtkHelper import run_on_main
from src.windows.AssetManager.ChooserPage import ChooserPage
from src.windows.AssetManager.WallpaperPacks.FlowBox import WallpaperPackFlowBox
from src.windows.AssetManager.WallpaperPacks.Preview import WallpaperPackPreview

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.AssetManager import AssetManager


class WallpaperPackChooser(ChooserPage):
    def __init__(self, asset_manager: "AssetManager"):
        super().__init__()
        self.asset_manager = asset_manager

        threading.Thread(target=self.build).start()
        
    @log.catch
    def build(self):
        # Pack discovery hits the disk -- that stays on this worker thread.
        # Every widget below is built and appended on the main loop: building
        # the flow box and a WallpaperPackPreview per pack HERE was the
        # off-main GTK construction crash class (#136, same class as #10).
        packs = list(gl.wallpaper_pack_manager.get_wallpaper_packs().values())

        run_on_main(self._build_ui, packs)

        self.set_loading(False)

    def _build_ui(self, packs: list) -> None:
        self.type_box.set_visible(False)

        self.wallpaper_pack_chooser = WallpaperPackFlowBox(self, orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        self.scrolled_box.prepend(self.wallpaper_pack_chooser)

        self.wallpaper_pack_chooser.flow_box.connect("child-activated", self.on_child_activated)

        flow_box = self.wallpaper_pack_chooser.flow_box
        for pack in packs:
            preview = WallpaperPackPreview(self, pack)
            flow_box.append(preview)

    def on_child_activated(self, flow_box, child):
        # Load wallpapers
        self.asset_manager.asset_chooser.wallpaper_pack_chooser.wallpaper_chooser.load_for_pack(child.pack)
        # Switch to wallpaper chooser
        self.asset_manager.asset_chooser.wallpaper_pack_chooser.set_visible_child_name("wallpaper-chooser")
        # Show back button
        self.asset_manager.back_button.set_visible(True)
