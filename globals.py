import json
import os
from typing import TYPE_CHECKING
import argparse
import sys
import threading

import appinfo
from collections import deque
from loguru import logger as log

# Automatically detect macOS
IS_MAC = sys.platform == "darwin"

from cli_args import argparser

MAIN_PATH: str
VAR_APP_PATH = os.path.join(os.path.expanduser("~"), ".var", "app", appinfo.APP_ID)
STATIC_SETTINGS_FILE_PATH = os.path.join(VAR_APP_PATH, "static", "settings.json")

DATA_PATH = os.path.join(VAR_APP_PATH, "data") # Maybe use XDG_DATA_HOME instead
if argparser.parse_args().data:
    DATA_PATH = argparser.parse_args().data
elif not argparser.parse_args().devel:
    # Check static settings
    if os.path.exists(STATIC_SETTINGS_FILE_PATH):
        try:
            with open(STATIC_SETTINGS_FILE_PATH) as f:
                settings = json.load(f)
                if "data-path" in settings:
                    DATA_PATH = settings["data-path"]
            log.info(f"Using data path from static settings: {DATA_PATH}")
        except Exception as e:
            log.error(f"Failed to set data path from static settings: {e}")

if not os.path.exists(DATA_PATH):
    log.info(f"Creating data path: {DATA_PATH}")
    try:
        os.makedirs(DATA_PATH)
    except Exception as e:
        log.error(f"Failed to create data path: {e}\nPlease change the data path manually in the config file under {STATIC_SETTINGS_FILE_PATH}")
        sys.exit(1)

PLUGIN_DIR = os.path.join(DATA_PATH, "plugins")
# Used for nix packaging
if os.getenv("PLUGIN_DIR") is not None:
    PLUGIN_DIR = os.getenv("PLUGIN_DIR")
    top_level_folder = os.path.dirname(PLUGIN_DIR)
    sys.path.append(top_level_folder)

    if os.path.exists(os.path.join(DATA_PATH, "plugins")):
        log.warning(f"You're using a plugin dir path outside of your data dir, but also have a plugin dir in the data dir. This may cause problems.")

os.makedirs(PLUGIN_DIR, exist_ok=True)

# Add data path to sys.path
sys.path.append(DATA_PATH)

if TYPE_CHECKING:
    import Pyro5.api
    from src.app import App
    from locales.LocaleManager import LocaleManager
    from src.backend.AssetManagerBackend import AssetManagerBackend
    from src.windows.AssetManager.AssetManager import AssetManager
    from src.backend.MediaManager import MediaManager
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
    from src.backend.SettingsManager import SettingsManager
    from src.backend.DeckManagement.DeckManager import DeckManager
    from src.backend.PluginManager.PluginManager import PluginManager
    from src.backend.IconPackManagement.IconPackManager import IconPackManager
    from src.backend.WallpaperPackManagement.WallpaperPackManager import WallpaperPackManager
    from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPackManager import SDPlusBarWallpaperPackManager
    from src.backend.Store.StoreBackend import StoreBackend
    from src.Signals.SignalManager import SignalManager
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
    from src.backend.GnomeExtensions import GnomeExtensions
    from src.windows.Store.Store import Store
    from src.backend.PermissionManagement.FlatpakPermissionManager import FlatpakPermissionManager
    from src.windows.PageManager.PageManager import PageManager
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager
    from src.tray import TrayIcon
    from src.backend.Logger import Logger


top_level_dir:str = os.path.dirname(__file__)
lm:"LocaleManager" = None
media_manager:"MediaManager" = None #MediaManager
asset_manager_backend:"AssetManagerBackend" = None #AssetManager
asset_manager: "AssetManager" = None
page_manager_window: "PageManager" = None # Only if opened
page_manager:"PageManagerBackend" = None #PageManager #TODO: Rename to page_manager_backend in 2.0.0
gnome_extensions:"GnomeExtensions" = None
settings_manager:"SettingsManager" = None #SettingsManager
app:"App" = None #App
deck_manager:"DeckManager" = None #DeckManager
plugin_manager:"PluginManager" = None #PluginManager
video_extensions = ["mp4", "mov", "MP4", "MOV", "mkv", "MKV", "webm", "WEBM", "gif", "GIF"]
image_extensions = ["png", "jpg", "jpeg"]
svg_extensions = ["svg", "SVG"]
icon_pack_manager: "IconPackManager" = None
wallpaper_pack_manager: "WallpaperPackManager" = None
sd_plus_bar_wallpaper_pack_manager: "SDPlusBarWallpaperPackManager" = None
store_backend: "StoreBackend" = None
pyro_daemon: "Pyro5.api.Daemon" = None  # never actually set/read (P3.2 grep); Pyro5 stays TYPE_CHECKING-only
signal_manager: "SignalManager" = None
window_grabber: "WindowGrabber" = None
lock_screen_detector: "LockScreenManager" = None
store: "Store" = None # Only if opened
flatpak_permission_manager: "FlatpakPermissionManager" = None
threads_running: bool = True
app_loading_finished_tasks: callable = []
api_page_requests: dict[str, str] = {} # Stores api page requests made my --change-page
api_state_requests: dict[str, dict] = {} # Stores api state change requests made by --change-state
tray_icon: "TrayIcon" = None
showed_donate_window: bool = False
screen_locked: bool = False
loggers: dict[str, "Logger"] = {}

app_version: str = "1.5.0-beta.15"  # In breaking.feature.fix-state format
exact_app_version_check: bool = False

# Deckard fork release version, stamped into the root VERSION file by the CI
# release pipeline (issue #128). Kept DISTINCT from app_version above -- that one
# stays upstream-aligned so plugin `min_app_version` gates and the migration
# system keep working; deckard_version is purely the number shown to users in the
# About dialog. Resolved from the repo root beside this file so it works for both
# a source checkout and the /opt/deckard native install. "dev" when unstamped.
def _read_deckard_version() -> str:
    try:
        _root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(_root, "VERSION")) as f:
            return f.read().strip() or "dev"
    except OSError:
        return "dev"

deckard_version: str = _read_deckard_version()
del _read_deckard_version
# Bounded ring buffer of recent log records (shown in the About dialog).
# logs_lock guards appends/reads against concurrent iteration.
logs: "deque[str]" = deque(maxlen=10000)
logs_lock = threading.Lock()

release_notes: str = """
<p>Features:</p>
    <ul>
        <li>Add uninstall button to plugin settings page</li>
    </ul>
<p>Improvements:</p>
    <ul>
        <li>Improved page switch speed</li>
        <li>Reduce idle CPU usage</li>
        <li>Improve Hyprland active window detection</li>
        <li>Switch to new GNOME runtime</li>
    </ul>
<p>Fixes:</p>
    <ul>
    </ul>
"""


def __getattr__(name: str):
    """PEP 562 module-level lazy attribute.

    `fallback_font` used to be computed eagerly at import time via
    HelperMethods.find_fallback_font(), which did a full matplotlib system
    font scan before a single window existed. font_resolver.fallback_font()
    is one fontconfig round trip instead of a directory walk, but there's
    still no reason to pay for it before the first label actually renders.
    Deferring the lookup to first access (and caching the result as a plain
    module attribute, so this function only runs once) keeps `gl.fallback_font`
    working unmodified for every existing reader -- including plugins.
    """
    if name == "fallback_font":
        from src.backend.DeckManagement.font_resolver import fallback_font
        value = fallback_font()
        globals()["fallback_font"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
