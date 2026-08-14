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
from src.windows.AssetManager.Preview import _PIXBUF_UNSET, Preview
from src.backend.IconPackManagement.IconPack import IconPack

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from windows.AssetManager.IconPacks.PackChooser import IconPackChooser

class IconPackPreview(Preview):
    def __init__(self, icon_pack_chooser: "IconPackChooser", pack: IconPack, pixbuf=_PIXBUF_UNSET):
        # The build worker of the chooser decodes pixbuf. This constructor
        # decodes the thumbnail itself, on its own thread, only when the
        # caller supplies no pixbuf.
        super().__init__(
            image_path=pack.get_thumbnail_path() if pixbuf is _PIXBUF_UNSET else None,
            text=pack.name,
            pixbuf=pixbuf
        )
        self.pack = pack
        self.icon_pack_chooser = icon_pack_chooser

    def on_click_info(self, *args):
        attribution = self.pack.get_pack_attribution()
        self.icon_pack_chooser.asset_manager.show_info(
            internal_path = None,
            licence_name = attribution.get("license"),
            license_url = attribution.get("license-url"),
            author = attribution.get("copyright"),
            license_comment = attribution.get("comment")
        )