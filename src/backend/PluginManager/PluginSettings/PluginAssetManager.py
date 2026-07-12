"""
Author: G4PLS
Year: 2024
"""

import json
import os.path

from loguru import logger as log

from .Manager import Manager
from .Asset import Color, Icon
from src.backend.atomic_json import atomic_write_json


class AssetManager:
    def __init__(self, plugin_base: "PluginBase"):
        self.plugin_base = plugin_base
        self.colors = Manager(Color, "colors")
        self.icons = Manager(Icon, "icons")

    def load_assets(self):
        if not os.path.exists(self.plugin_base.settings_path):
            return {}

        # This runs inside PluginBase.__init__ -- a corrupt settings file
        # (e.g. truncated by a crash) must not raise, or the whole plugin
        # silently fails to load.
        try:
            with open(self.plugin_base.settings_path, "r") as f:
                assets = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.opt(exception=e).error(
                f"Could not read plugin assets from {self.plugin_base.settings_path} "
                f"-- continuing without custom assets"
            )
            return {}

        if not isinstance(assets, dict):
            log.error(
                f"Plugin settings file {self.plugin_base.settings_path} does not "
                f"contain a JSON object -- continuing without custom assets"
            )
            return {}

        assets = assets.get("assets", {})
        self.icons.load_json(assets)
        self.colors.load_json(assets)

    def save_assets(self):
        assets = {}
        assets[self.colors.get_save_key()] = self.colors.get_override_json()
        assets[self.icons.get_save_key()] = self.icons.get_override_json()

        content = {}
        if os.path.isfile(self.plugin_base.settings_path):
            with open(self.plugin_base.settings_path, "r") as f:
                try:
                    content = json.load(f)
                except json.JSONDecodeError:
                    content = {}

        content["assets"] = assets

        # Atomic write so an interrupted save can't truncate the file.
        atomic_write_json(self.plugin_base.settings_path, content)
