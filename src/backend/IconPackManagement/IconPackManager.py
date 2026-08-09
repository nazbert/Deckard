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

# Import Python modules
import os
from typing import Any
from loguru import logger as log

# Import own modules
from src.backend.IconPackManagement.IconPack import IconPack

# Import globals
import globals as gl

class IconPackManager:
    def __init__(self) -> None:
        self.packs: dict[str, IconPack] = {}

    def get_icon_packs(self) -> dict[str, IconPack]:
        packs: dict[str, IconPack] = {}
        os.makedirs(os.path.join(gl.DATA_PATH, "icons"), exist_ok=True)
        for pack in os.listdir(os.path.join(gl.DATA_PATH, "icons")):
            if pack.startswith("."):
                # Transient install-swap trees (StoreBackend._swap_into_place)
                # and other hidden entries are not packs.
                continue
            icon_pack = IconPack(os.path.join(gl.DATA_PATH, "icons", pack))
            if icon_pack.is_valid:
                packs[pack] = icon_pack
            else:
                log.warning(f"Icon pack {pack} is not valid.")
        return packs

    def get_pack_icons(self, icon_pack: dict[str, Any]) -> dict[str, Any]:
        path = icon_pack.get("path")
        if path is None:
            return {}
        icons_path = os.path.join(path, "icons")

        attribution: dict[str, Any] = icon_pack.get("attribution") or {}

        icons: dict[str, Any] = {}
        if os.path.exists(icons_path):
            for icon in os.listdir(icons_path):
                icons.setdefault(icon, {})
                icons[icon] =  self.get_icon_attribution(attribution, icon)

        return icons

    def get_icon_attribution(self, attribution: dict[str, Any], icon_name: str) -> dict[str, Any] | None:
        if icon_name in attribution:
            # Use specific
            return attribution[icon_name]
        else:
            # Use default
            return attribution.get("generic", attribution.get("default", attribution.get("general")))
            
    def prepare_icon_packs(self):
        return
        packs = self.get_icon_packs()

        prepared_packs = {}

        for pack in packs:
            prepared_packs[pack] = self.get_pack_icons(packs[pack])

        return prepared_packs