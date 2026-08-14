from dataclasses import dataclass, field
from PIL import Image

from loguru import logger as log
from packaging import version
from packaging.version import InvalidVersion


def is_min_app_version_satisfied(minimum_app_version: str | None) -> bool:
    """The minimum-app-version gate for a store asset.

    It is the one implementation behind StorePreview.check_required_version.
    The comparison includes the running version, so an asset that requires
    exactly the running version passes.

    It compares base versions and strips the pre-release, post and local
    suffixes, which matches the decision of the runtime plugin loader in
    PluginBase.is_minimum_version_ok. Without that, a pre-release such as
    1.5.0-beta.15 badges an asset that requires 1.5.0 as incompatible while
    the loader loads it. The badge must match the install-time verdict.

    An unparseable version string returns True and logs a warning, which
    matches the None case. A malformed catalog entry must not raise out of a
    store page build.
    """
    import globals as gl  # deferred: keep this leaf module cycle-free

    if minimum_app_version is None:
        return True
    try:
        # .base_version drops pre/post/dev/local segments the same way the
        # runtime gate does; re-parse so the comparison is version-aware,
        # not a string compare.
        minimum = version.parse(version.parse(minimum_app_version).base_version)
        running = version.parse(version.parse(gl.app_version).base_version)
        return minimum <= running
    except InvalidVersion:
        log.warning(
            f"Unparseable minimum app version {minimum_app_version!r}; assuming compatible"
        )
        return True


@dataclass
class StoreData:
    github: str | None = None # Link to the github repository
    # StoreBackend passes "... or None" for each of these, so an absent value
    # is None and not an empty container. LocaleManager.get_custom_translation
    # returns "" for None and None for an empty dict, so the two differ.
    descriptions: dict[str, str] | None = field(default_factory=dict) # All the translations for the description
    short_descriptions: dict[str, str] | None = field(default_factory=dict) # All the translations for the short descriptions
    description: str | None = None # Translated Description of the Content
    short_description: str | None = None # Translated short Description of the Content
    author: str | None = None # Author of the Content
    official: bool | None = None # If the Content is Officially Made or not
    commit_sha: str | None = None # SHA of the github commit that gets used
    local_sha: str | None = None # The local SHA that verifies whether a plugin is installed
    minimum_app_version: str | None = None # Minimum app version that is required to use the Content
    app_version: str | None = None # The Current app version the Plugin is made for
    repository_name: str | None = None # Name of the Repository
    tags: list[str] | None = field(default_factory=list) # If the asset has a compatible version
    is_compatible: bool | None = None
    branch: str | None = None # Repo branch to install from; None = the repo default
    verified: bool = False

@dataclass
class ImageData:
    thumbnail: str | None = None # Path to the Thumbnail used in the Store
    image: Image.Image | None = None # The Image that gets displayed in the Store

@dataclass
class LicenceData:
    copyright: str | None = None
    original_url: str | None = None
    license: str | None = None # The actual licence
    license_descriptions: dict[str, str] | None = field(default_factory=dict) # Translations for the Licence Description

# Each concrete class below names its id, name and version triple
# differently, such as plugin_id, icon_id or wallpaper_id. The asset_id,
# asset_name and asset_version properties give the shared backend code and the
# log lines one name for that triple, so a pipeline reads a property instead
# of a per-type field name. The properties are read-only, and the per-type
# fields stay writable.
#
# StoreBackend uses the name asset_id for three things. This catalog property
# holds the manifest id. InstalledAsset.asset_id holds an install-directory
# name. UpdateCheck.asset_id holds the id of the matched install.

@dataclass
class PluginData(StoreData, ImageData, LicenceData):
    plugin_name: str | None = None # Name of the Plugin
    plugin_version: str | None = None # Version of the Plugin
    plugin_id: str | None = None # Plugin ID in the com.author.name format

    @property
    def asset_id(self) -> str | None:
        return self.plugin_id

    @property
    def asset_name(self) -> str | None:
        return self.plugin_name

    @property
    def asset_version(self) -> str | None:
        return self.plugin_version

@dataclass
class IconData(StoreData, ImageData, LicenceData):
    icon_name: str | None = None # Name of the icon
    icon_version: str | None = None # Version of the icons
    icon_id: str | None = None # Icon ID in the com.author.name format

    @property
    def asset_id(self) -> str | None:
        return self.icon_id

    @property
    def asset_name(self) -> str | None:
        return self.icon_name

    @property
    def asset_version(self) -> str | None:
        return self.icon_version

@dataclass
class WallpaperData(StoreData, ImageData, LicenceData):
    wallpaper_name: str | None = None # Name of the wallpaper
    wallpaper_version: str | None = None # Version of the wallpaper
    wallpaper_id: str | None = None # Icon ID in the com.author.name format

    @property
    def asset_id(self) -> str | None:
        return self.wallpaper_id

    @property
    def asset_name(self) -> str | None:
        return self.wallpaper_name

    @property
    def asset_version(self) -> str | None:
        return self.wallpaper_version

@dataclass
class SDPlusBarWallpaperData(StoreData, ImageData, LicenceData):
    name: str | None = None # Name of the SD+ Bar wallpaper
    version: str | None = None # Version of the SD+ Bar wallpaper
    id: str | None = None # SD+ Bar wallpaper ID in the com.author.name format

    @property
    def asset_id(self) -> str | None:
        return self.id

    @property
    def asset_name(self) -> str | None:
        return self.name

    @property
    def asset_version(self) -> str | None:
        return self.version