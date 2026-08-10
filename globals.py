import json
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
import sys
import threading

import appinfo
from collections import deque
from loguru import logger as log

from cli_args import argparser

MAIN_PATH: str
# Data root. Flatpak keeps its per-app dir (~/.var/app/<id>); native installs use
# the XDG data dir ($XDG_DATA_HOME/deckard, default ~/.local/share/deckard). A
# pre-XDG native tree at ~/.var/app/<id> is relocated here first by
# rebrand_migration.migrate_native_var_app_to_xdg() (main.py, pre-import);
# native_data_root() falls back to that old path if the move was skipped (e.g.
# across a filesystem boundary), so the app never starts empty.
if os.path.isfile("/.flatpak-info"):
    VAR_APP_PATH = os.path.join(os.path.expanduser("~"), ".var", "app", appinfo.APP_ID)
else:
    import rebrand_migration
    VAR_APP_PATH = rebrand_migration.native_data_root()
STATIC_SETTINGS_FILE_PATH = os.path.join(VAR_APP_PATH, "static", "settings.json")

DATA_PATH = os.path.join(VAR_APP_PATH, "data")
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
    PLUGIN_DIR = os.environ["PLUGIN_DIR"]
    top_level_folder = os.path.dirname(PLUGIN_DIR)
    sys.path.append(top_level_folder)

    if os.path.exists(os.path.join(DATA_PATH, "plugins")):
        log.warning("You're using a plugin dir path outside of your data dir, but also have a plugin dir in the data dir. This may cause problems.")

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
    from src.backend.notify import Notify
    from src.Signals.SignalManager import SignalManager
    from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
    from src.backend.GnomeExtensions import GnomeExtensions
    from src.windows.Store.Store import Store
    from src.backend.PermissionManagement.FlatpakPermissionManager import FlatpakPermissionManager
    from src.windows.PageManager.PageManager import PageManager
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager
    from src.backend.PresenceMonitor.PresenceMonitor import PresenceMonitor
    from src.tray import TrayIcon
    from src.backend.Logger import Logger


top_level_dir:str = os.path.dirname(__file__)
# Two shapes of slot live here, and the difference is load-bearing.
#
# `X | None` -- the value is observably absent to real code: something either
# reads it before main.create_global_objects() publishes it (the pre-App
# notification deferral), or nulls it again later (window close). Every such
# slot has a real `is None` branch somewhere, so it must be Optional: with a
# concrete type mypy narrows that branch to an uninhabited type and SKIPS the
# body entirely, silently leaving the exact code paths that only run during
# boot or teardown -- the ones hardest to cover by hand -- unchecked.
#
# `X` + a late-init ignore -- never observed as None by anything that runs.
# Kept concrete because widening would push union-attr into hundreds of
# post-boot use sites for a None that no code path can see.
lm:"LocaleManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
media_manager:"MediaManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
asset_manager_backend:"AssetManagerBackend" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
asset_manager: "AssetManager | None" = None # Only while the window is open
page_manager_window: "PageManager | None" = None # Only if opened
page_manager:"PageManagerBackend | None" = None # None-checked in DeckController teardown + the DBus API #TODO: Rename to page_manager_backend in 2.0.0
gnome_extensions:"GnomeExtensions" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
settings_manager:"SettingsManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
app:"App | None" = None # Absent until App.on_activate; notify/PluginManager defer onto app_loading_finished_tasks while it is
deck_manager:"DeckManager | None" = None # None-checked in the DBus API
plugin_manager:"PluginManager | None" = None # None-checked in ActionChooser's load-health readout
video_extensions = ["mp4", "mov", "MP4", "MOV", "mkv", "MKV", "webm", "WEBM", "gif", "GIF"]
image_extensions = ["png", "jpg", "jpeg"]
svg_extensions = ["svg", "SVG"]
icon_pack_manager: "IconPackManager | None" = None # None-checked in the DBus API
wallpaper_pack_manager: "WallpaperPackManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
sd_plus_bar_wallpaper_pack_manager: "SDPlusBarWallpaperPackManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
store_backend: "StoreBackend | None" = None # None-checked in App.on_quit's cache flush
notify: "Notify" = None  # type: ignore[assignment]  # late-init: main.create_global_objects; see src/backend/notify.py
pyro_daemon: "Pyro5.api.Daemon | None" = None  # never actually set/read (P3.2 grep); Pyro5 stays TYPE_CHECKING-only
signal_manager: "SignalManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
window_grabber: "WindowGrabber | None" = None # None-checked in the DBus API
lock_screen_detector: "LockScreenManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
presence_monitor: "PresenceMonitor | None" = None  # quiescence signal; see src/backend/PresenceMonitor
store: "Store | None" = None # Only if opened
flatpak_permission_manager: "FlatpakPermissionManager" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
threads_running: bool = True
# Zero-arg deliveries queued before gl.app exists; App.on_activate drains them
# on the main thread. Return values are ignored (see src/backend/notify.py).
app_loading_finished_tasks: list[Callable[[], Any]] = []
api_page_requests: dict[str, str] = {} # Stores api page requests made my --change-page
api_state_requests: dict[str, dict] = {} # Stores api state change requests made by --change-state
tray_icon: "TrayIcon" = None  # type: ignore[assignment]  # late-init: main.create_global_objects
showed_donate_window: bool = False
screen_locked: bool = False
loggers: dict[str, "Logger"] = {}

app_version: str = "1.5.0-beta.15"  # In breaking.feature.fix-state format
exact_app_version_check: bool = False

# Deckard fork release version, stamped into the root VERSION file by the CI
# release pipeline. Kept DISTINCT from app_version above -- that one
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
