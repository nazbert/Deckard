"""
Author: G4PLS
Year: 2024
"""

from typing import TYPE_CHECKING

from .Manager import Manager
from .Asset import Color, Icon
from src.backend.settings_store import PluginSettings

if TYPE_CHECKING:
    # PluginBase imports this module at module scope, so keep this type-only.
    from src.backend.PluginManager.PluginBase import PluginBase


class AssetManager:
    """The icon and colour overrides a plugin's settings file carries.

    They live beside the plugin's own settings, in one file, under an assets
    key the app owns. Both halves read and write that file through the
    settings-store surface, which handles a corrupt file in one place.
    """

    def __init__(self, plugin_base: "PluginBase"):
        self.plugin_base = plugin_base
        self.colors = Manager(Color, "colors")
        self.icons = Manager(Icon, "icons")

    def _settings_file(self) -> PluginSettings:
        """Give the plugin's settings file, once per call.

        The path is an attribute of the plugin. A path read at construction
        keeps pointing at a file the plugin stopped using."""
        return PluginSettings(self.plugin_base.settings_path)

    def load_assets(self):
        # The first reader of this file. PluginBase.__init__ runs before
        # register(), and a plugin that never registers, because a version gate
        # or an incomplete manifest stops it, has no other reader. A corrupt
        # file therefore needs a quarantine from here too, or it survives every
        # load and the next save destroys it. Nothing here may raise, or the
        # whole plugin fails to load without a trace.
        content = self._settings_file().document()
        if content is None:
            return {}

        assets = content.get("assets", {})
        self.icons.load_json(assets)
        self.colors.load_json(assets)

    def save_assets(self):
        assets = {}
        assets[self.colors.get_save_key()] = self.colors.get_override_json()
        assets[self.icons.get_save_key()] = self.icons.get_override_json()

        # Five UI call sites reach this, among them the icon override, the
        # icon reset and the colour pick. The write replaces the file
        # wholesale, so it reads the rest of the content back first. An
        # unreadable file reads as empty and raises nothing at a colour
        # picker.
        settings = self._settings_file()
        content = settings.document() or {}
        content["assets"] = assets
        settings.save_document(content)
