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

import os
from typing import Any
from loguru import logger as log

import globals as gl

from src.backend.WallpaperPackManagement.WallpaperPack import WallpaperPack

class WallpaperPackManager:
    def __init__(self) -> None:
        self.packs: dict[str, WallpaperPack] = {}

    def get_wallpaper_packs(self) -> dict[str, WallpaperPack]:
        packs: dict[str, WallpaperPack] = {}
        os.makedirs(os.path.join(gl.DATA_PATH, "wallpapers"), exist_ok=True)
        for pack in os.listdir(os.path.join(gl.DATA_PATH, "wallpapers")):
            if pack.startswith("."):
                # Transient install-swap trees (StoreBackend._swap_into_place)
                # and other hidden entries are not packs.
                continue
            wallpaper_pack = WallpaperPack(os.path.join(gl.DATA_PATH, "wallpapers", pack))
            if wallpaper_pack.is_valid:
                packs[pack] =  wallpaper_pack
            else:
                log.warning(f"Wallpaper pack {pack} is not valid.")
        return packs

    def get_pack_wallpapers(self, wallpaper_pack: dict[str, Any]) -> dict[str, Any]:
        path = wallpaper_pack.get("path")
        if path is None:
            return {}
        wallpaper_path = os.path.join(path, "wallpapers")

        attribution: dict[str, Any] = wallpaper_pack.get("attribution") or {}

        wallpapers: dict[str, Any] = {}
        if os.path.exists(wallpaper_path):
            for wallpaper in os.listdir(wallpaper_path):
                wallpapers.setdefault(wallpaper, {})
                wallpapers[wallpaper] =  self.get_wallpaper_attribution(attribution, wallpaper)

        return wallpapers

    def get_wallpaper_attribution(self, attribution: dict[str, Any], wallpaper_name: str) -> dict[str, Any] | None:
        if wallpaper_name in attribution:
            return attribution[wallpaper_name]
        else:
            return attribution.get("generic", attribution.get("default", attribution.get("general")))