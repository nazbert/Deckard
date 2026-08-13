"""One row per store asset class, so the backend can carry a single
implementation of the work that used to exist four times over.

The four asset classes -- plugins, icon packs, wallpapers and SD+ bar
wallpapers -- differ only in a handful of names: which catalog file lists
them, which directory they install into, which dataclass describes them,
and what the id/name/version fields on that dataclass are called. A
descriptor names those differences as DATA so the prepare/install/update
pipelines can be written once and select the right names by looking them up
here.

Two constraints shape the field types, both driven by how the store is
tested and how the data dir moves under it:

  * Every cross-method reference the backend makes is a METHOD NAME, resolved
    with ``getattr(self, name)`` at call time -- never a bound callable
    captured here. The store's tests stub instance attributes
    (``sb.get_all_icons = fake``, ``sb.install_icon = fake``); a descriptor
    that held a function reference would run the original past the stub and
    silently defeat every one of those tests.

  * Install directories are the NAME of a backend method, not a path. The
    backend resolves ``gl.DATA_PATH`` / ``gl.PLUGIN_DIR`` when the method is
    called, and the test harness re-points the data dir per process -- a path
    frozen at import time here would point at the wrong tree.

This module reads no globals and touches neither GTK nor json at import
time; it imports only the dataclasses it names.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.windows.Store.StoreData import (
    IconData,
    PluginData,
    SDPlusBarWallpaperData,
    StoreData,
    WallpaperData,
)


@dataclass(frozen=True)
class AssetTypeDescriptor:
    """The names that distinguish one store asset class from the others."""

    display_name: str            # human label for log/notification strings
    data_cls: type[StoreData]    # the dataclass a prepared entry becomes
    catalog_file: str            # the store's catalog filename for this class
    base_dir_attr: str           # backend method NAME giving the install dir
    id_field: str                # the data_cls field holding the asset id
    name_field: str              # the data_cls field holding the display name
    version_field: str           # the data_cls field holding the version
    get_all_attr: str            # backend method NAME: list every entry
    install_attr: str            # backend method NAME: install one entry
    get_custom_attr: str | None  # backend method NAME for user-added entries
    # Whether an install call reported success. Plugins answer True, the
    # other three answer the HTTP-style 200 -- the one dialect the collapse
    # cannot yet erase, carried here as data until the install boundary
    # returns a single success value and this predicate is removed. Unlike the
    # method-name fields above, this is a predicate over an already-RETURNED
    # value, not a reference to a method the backend would dispatch through --
    # so it carries no stub-bypass risk and is allowed to be a callable.
    install_ok: Callable[[object], bool]
    is_plugin: bool              # gates the plugin-only install/prepare paths


PLUGIN = AssetTypeDescriptor(
    display_name="plugin",
    data_cls=PluginData,
    catalog_file="Plugins.json",
    base_dir_attr="plugins_dir",
    id_field="plugin_id",
    name_field="plugin_name",
    version_field="plugin_version",
    get_all_attr="get_all_plugins",
    install_attr="install_plugin",
    get_custom_attr="get_custom_plugins",
    install_ok=lambda result: result is True,
    is_plugin=True,
)

ICON = AssetTypeDescriptor(
    display_name="icon pack",
    data_cls=IconData,
    catalog_file="Icons.json",
    base_dir_attr="icons_dir",
    id_field="icon_id",
    name_field="icon_name",
    version_field="icon_version",
    get_all_attr="get_all_icons",
    install_attr="install_icon",
    get_custom_attr=None,
    install_ok=lambda result: result == 200,
    is_plugin=False,
)

WALLPAPER = AssetTypeDescriptor(
    display_name="wallpaper",
    data_cls=WallpaperData,
    catalog_file="Wallpapers.json",
    base_dir_attr="wallpapers_dir",
    id_field="wallpaper_id",
    name_field="wallpaper_name",
    version_field="wallpaper_version",
    get_all_attr="get_all_wallpapers",
    install_attr="install_wallpaper",
    get_custom_attr=None,
    install_ok=lambda result: result == 200,
    is_plugin=False,
)

SD_PLUS_BAR = AssetTypeDescriptor(
    display_name="SD+ bar wallpaper",
    data_cls=SDPlusBarWallpaperData,
    catalog_file="SDPlusBarWallpapers.json",
    base_dir_attr="sd_plus_bar_wallpapers_dir",
    id_field="id",
    name_field="name",
    version_field="version",
    get_all_attr="get_all_sd_plus_bar_wallpapers",
    install_attr="install_sd_plus_bar_wallpaper",
    get_custom_attr=None,
    install_ok=lambda result: result == 200,
    is_plugin=False,
)

ASSET_TYPES: tuple[AssetTypeDescriptor, ...] = (PLUGIN, ICON, WALLPAPER, SD_PLUS_BAR)
