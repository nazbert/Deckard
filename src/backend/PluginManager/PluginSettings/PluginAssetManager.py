"""
Author: G4PLS
Year: 2024
"""

from typing import TYPE_CHECKING

from .Manager import Manager
from .Asset import Color, Icon
from src.backend.settings_store import PluginSettings

if TYPE_CHECKING:
    # PluginBase imports this module at module scope -- type-only here.
    from src.backend.PluginManager.PluginBase import PluginBase


class AssetManager:
    """The icon and colour overrides a plugin's settings file carries.

    They live beside the plugin's own settings, in the same file, under an
    ``assets`` key the app owns -- so both halves read and write it through
    the same settings-store surface, and a corrupt or unreadable one is
    handled in exactly one place.
    """

    def __init__(self, plugin_base: "PluginBase"):
        self.plugin_base = plugin_base
        self.colors = Manager(Color, "colors")
        self.icons = Manager(Icon, "icons")

    def _settings_file(self) -> PluginSettings:
        """The plugin's settings file, per call rather than held: the path is
        an attribute of the plugin, and one taken at construction would keep
        pointing at a file the plugin has since stopped using."""
        return PluginSettings(self.plugin_base.settings_path)

    def load_assets(self):
        # The EARLIEST reader of this file: PluginBase.__init__ runs before
        # register(), and a plugin that never registers (version-gated,
        # incomplete manifest) has no other reader at all -- so a corrupt file
        # has to be quarantined from here too, or it could survive every load
        # and still be destroyed by the next save. Nothing about the file may
        # raise here either, or the whole plugin silently fails to load.
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

        # Reached from five live UI call sites (icon override, icon reset,
        # colour pick, ...), and the write replaces the file wholesale -- so
        # whatever else it holds is read back first, and a file that cannot be
        # read comes back as "nothing" rather than as an exception through a
        # colour picker.
        settings = self._settings_file()
        content = settings.document() or {}
        content["assets"] = assets
        settings.save_document(content)
