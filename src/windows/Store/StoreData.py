from dataclasses import dataclass, field
from PIL import Image

from loguru import logger as log
from packaging import version
from packaging.version import InvalidVersion


def is_min_app_version_satisfied(minimum_app_version: str | None) -> bool:
    """THE minimum-app-version gate for store assets -- the single
    implementation behind StorePreview.check_required_version.

    It used to exist as four drifting copies (StorePage, PluginPage,
    StorePreview, PluginPreview), each comparing with a strict `<` that
    flagged an asset requiring EXACTLY the running version incompatible.
    Inclusive by design: requiring the running version is satisfied.

    Compares BASE versions (pre-release/post/local suffixes stripped),
    matching what the runtime plugin loader actually decides
    (PluginBase.is_minimum_version_ok via _get_parsed_base_version). Without
    this, running a pre-release like 1.5.0-beta.15 made the store badge an
    asset requiring 1.5.0 as incompatible while the loader would load it
    fine -- the displayed verdict must match the install-time verdict.

    Fails open (True, with a warning) on an unparseable version string --
    consistent with the None case; a malformed remote catalog entry must
    not raise out of a store page build.
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
    # StoreBackend passes `... or None` for every one of these, so absent is
    # None, not an empty container (LocaleManager.get_custom_translation
    # answers "" for None but None for {} -- the two are not interchangeable).
    descriptions: dict[str, str] | None = field(default_factory=dict) # All the translations for the description
    short_descriptions: dict[str, str] | None = field(default_factory=dict) # All the translations for the short descriptions
    description: str | None = None # Translated Description of the Content
    short_description: str | None = None # Translated short Description of the Content
    author: str | None = None # Author of the Content
    official: bool | None = None # If the Content is Officially Made or not
    commit_sha: str | None = None # SHA of the github commit that gets used
    local_sha: str | None = None # The Local SHA that is used to verify if plugins are installed
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

# Each concrete class below names its id/name/version triple differently
# (plugin_id, icon_id, wallpaper_id, bare id ...). The canonical asset_id /
# asset_name / asset_version properties give shared backend code and log lines
# one name for that triple, so the pipelines that used to getattr a per-type
# field name read a property instead. Read-only by design -- the per-type
# fields stay the writable source of truth.
#
# The name asset_id is overloaded inside StoreBackend: this catalog *Data
# property is the manifest id, distinct from InstalledAsset.asset_id (an
# install-directory name) and UpdateCheck.asset_id (the matched install's id).

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