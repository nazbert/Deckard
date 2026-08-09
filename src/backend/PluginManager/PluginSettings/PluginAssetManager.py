"""
Author: G4PLS
Year: 2024
"""

import json
import os.path
from typing import TYPE_CHECKING

from loguru import logger as log

from .Manager import Manager
from .Asset import Color, Icon
from src.backend.atomic_json import atomic_write_json

if TYPE_CHECKING:
    # PluginBase imports this module at module scope -- type-only here.
    from src.backend.PluginManager.PluginBase import PluginBase


class AssetManager:
    def __init__(self, plugin_base: "PluginBase"):
        self.plugin_base = plugin_base
        self.colors = Manager(Color, "colors")
        self.icons = Manager(Icon, "icons")

    def _quarantine(self, error: Exception) -> None:
        """Quarantine the plugin's corrupt settings file.

        Imported lazily: PluginBase imports THIS module at module scope, so a
        top-level import back into it would be circular.
        """
        from src.backend.PluginManager.PluginBase import _quarantine_corrupt_json
        plugin = self.plugin_base
        _quarantine_corrupt_json(
            plugin.settings_path,
            f"Plugin {getattr(plugin, 'plugin_name', None) or plugin.PATH}: settings file",
            error,
        )

    def load_assets(self):
        if not os.path.exists(self.plugin_base.settings_path):
            return {}

        # This runs inside PluginBase.__init__ -- a corrupt settings file
        # (e.g. truncated by a crash) must not raise, or the whole plugin
        # silently fails to load.
        try:
            with open(self.plugin_base.settings_path, "r") as f:
                assets = json.load(f)
        except ValueError as e:
            # The EARLIEST reader of this file: __init__ runs before
            # register(), and a plugin that never registers (version-gated,
            # incomplete manifest) has no other reader at all -- so without
            # quarantine here a corrupt settings file could survive every
            # load and still be destroyed by the next save.
            self._quarantine(e)
            return {}
        except OSError as e:
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
            try:
                with open(self.plugin_base.settings_path, "r") as f:
                    content = json.load(f)
            except ValueError as e:
                # This method is reached from five live UI call sites and the
                # write below replaces the file wholesale -- it is the same
                # data-loss moment PluginBase.set_settings guards, on the same
                # file. Preserve the corrupt bytes before overwriting them.
                self._quarantine(e)
                content = {}

        if not isinstance(content, dict):
            # A settings file holding valid JSON that is not an object used to
            # raise TypeError on the assignment below.
            content = {}

        content["assets"] = assets

        # Atomic write so an interrupted save can't truncate the file.
        atomic_write_json(self.plugin_base.settings_path, content)
