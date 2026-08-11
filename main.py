"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import os
import sys

# Cap glibc's per-thread arena multiplication before any allocation-heavy
# import runs (docs/memory-footprint-plan.md §4 D5). Re-exec (rather than
# just setting os.environ) because glibc reads these at libc init, not on
# demand -- setting them mid-process here would be too late for the arenas
# already carved out by this interpreter's own startup. SC_REEXEC guards
# against a loop; the MALLOC_ARENA_MAX check lets a packaged launcher
# (flatpak/launch.sh) set the vars itself and skip this re-exec entirely.
# sys.orig_argv (not sys.argv) preserves interpreter flags like -X/-O.
# Must run before the instance gate/make_api_calls() in main() -- execve
# replaces this process outright, so there is no double DBus send.
if "MALLOC_ARENA_MAX" not in os.environ and "SC_REEXEC" not in os.environ:
    os.environ["MALLOC_ARENA_MAX"] = "2"
    os.environ["MALLOC_TRIM_THRESHOLD_"] = "131072"
    os.environ["SC_REEXEC"] = "1"
    os.execve(sys.executable, sys.orig_argv, os.environ)

# Import Python modules
import setproctitle

setproctitle.setproctitle("Deckard")

# Dump all-thread tracebacks on a fatal signal, or on demand via SIGQUIT.
# stderr-only from time zero; main() re-points it at logs/faulthandler.log
# via log_hooks.redirect_faulthandler() once gl.DATA_PATH is resolved (it
# can come from --data or the static settings file, so not knowable here).
import faulthandler, signal
try:
    faulthandler.enable()
    faulthandler.register(signal.SIGQUIT)
except (AttributeError, ValueError, OSError):
    pass

# One-time rename migration (StreamController -> Deckard): move the whole
# ~/.var/app tree to the new id and leave a compat symlink at the old path.
# MUST run before `import globals` below -- globals.py os.makedirs()es the
# data dir at import time on every invocation, which would poison the
# migration's existence checks (docs/rename-deckard-plan.md, Phase 2).
import appinfo
from rebrand_migration import migrate as _rebrand_migrate, migrate_native_var_app_to_xdg as _xdg_migrate
_rebrand_migrate()
# Native only (no-op under flatpak): relocate ~/.var/app/<id> -> XDG data dir.
# After the rename above so a StreamController->Deckard tree lands first.
_xdg_migrate()

import sys
from loguru import logger as log
import os
import time
import threading

# Cap OpenCV's global parallel_for_ pool before the first cv2 call anywhere
# in the app -- the pool is created lazily and sized to nproc by default,
# which is where the 32 same-second "background_0"-named threads come from
# on a 32-core box (docs/memory-footprint-plan.md §2). cvtColor is the only
# parallel_for_ user in this app; PIL does all resizing and
# VideoWriter/VideoCapture threading is FFmpeg-side, unaffected by this knob.
import cv2
cv2.setNumThreads(2)

# Import globals
import globals as gl

# Import own modules
from src.app import App
from src.api import start_dbus_service
from src.backend.DeckManagement.DeckManager import DeckManager
from locales.LocaleManager import LocaleManager
from src.backend.MediaManager import MediaManager
from src.backend.AssetManagerBackend import AssetManagerBackend
from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
from src.backend.SettingsManager import AppSettings, SettingsManager
from src.backend.PluginManager.PluginManager import PluginManager
from src.backend.IconPackManagement.IconPackManager import IconPackManager
from src.backend.WallpaperPackManagement.WallpaperPackManager import WallpaperPackManager
from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPackManager import SDPlusBarWallpaperPackManager
from src.backend.Store.StoreBackend import StoreBackend, NoConnectionError
from src.backend.notify import Notify
from autostart import setup_autostart, ensure_app_desktop_entry
from src.Signals.SignalManager import SignalManager
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
from src.backend.GnomeExtensions import GnomeExtensions
from src.backend.PermissionManagement.FlatpakPermissionManager import FlatpakPermissionManager
from src.backend.Wayland.Wayland import Wayland
from src.backend.LockScreenManager.LockScreenManager import LockScreenManager
from src.backend.PresenceMonitor.PresenceMonitor import PresenceMonitor
from src.tray import TrayIcon
from src.backend.Logger import Logger, LoggerConfig, Loglevel
from src.backend.log_hooks import install_exception_hooks, redirect_faulthandler
from src.backend import cli_forward, instance_gate

# Migration
from src.backend.Migration.MigrationManager import MigrationManager
from src.backend.Migration.Migrators.Migrator_1_5_0 import Migrator_1_5_0
from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

# Import globals
import globals as gl

# Define constants
DEFAULT_DATA_PATH = os.path.expanduser(f"~/.var/app/{appinfo.APP_ID}/data")

# Rotated files kept per log sink, oldest deleted first. Bounding this is the
# only thing standing between a long-lived install and a log directory that
# grows without limit -- loguru keeps every rotation unless told otherwise.
LOG_RETENTION_FILES = 10
# Default verbosity: the log files and the in-app ring take DEBUG and up, the
# console INFO and up. TRACE is bulk, and bulk is what fills the files.
# SC_LOG_TRACE=1 puts every sink back to TRACE for diagnosis; strictly "1",
# matching SC_NO_ERROR_HOOKS/SC_STRONG_CALLBACKS, so SC_LOG_TRACE=off cannot
# read as on. Read at import because config_logger() runs once, at boot.
LOG_TRACE = os.environ.get("SC_LOG_TRACE") == "1"
FILE_LOG_LEVEL = "TRACE" if LOG_TRACE else "DEBUG"
CONSOLE_LOG_LEVEL = "TRACE" if LOG_TRACE else "INFO"

main_path = os.path.abspath(os.path.dirname(__file__))
gl.MAIN_PATH = main_path

def write_logs(record):
    with gl.logs_lock:
        gl.logs.append(record)

@log.catch
def config_logger():
    log.remove()
    # Create log files. No backtrace=/diagnose=: the redaction patcher clears
    # record["exception"] and folds a scrubbed traceback into the message
    # before any sink sees the record, so there is no exception left for a
    # sink to expand -- both flags would be inert, and diagnose= in particular
    # would promise a variable dump that never arrives.
    log.add(os.path.join(gl.DATA_PATH, "logs/logs.log"), rotation="3 days",
            retention=LOG_RETENTION_FILES, level=FILE_LOG_LEVEL)
    # Set min level to print
    log.add(sys.stderr, level=CONSOLE_LOG_LEVEL)
    log.add(write_logs, level=FILE_LOG_LEVEL)

    plugin_logger = Logger(
        LoggerConfig(
            name="PLUGIN",
            log_file_path=os.path.join(gl.DATA_PATH, "logs/plugins.log"),
            base_log_level=FILE_LOG_LEVEL,
            rotation="3 days",
            retention=LOG_RETENTION_FILES,
            compression="zip"
        ),
        [
            Loglevel("TRACE", "trace", 5, "<bold><cyan>"),
            Loglevel("DEBUG", "debug", 10, "<bold><blue>"),
            Loglevel("INFO", "info", 20, "<bold><white>"),
            Loglevel("SUCCESS", "success", 25, "<bold><green>"),
            Loglevel("WARNING", "warning", 30, "<bold><yellow>"),
            Loglevel("ERROR", "error", 40, "<red>"),
            Loglevel("CRITICAL", "critical", 50, "<bold><red>"),
        ]
    )

    gl.loggers["plugins"] = plugin_logger

@log.catch
def create_cache_folder():
    os.makedirs(os.path.join(gl.DATA_PATH, "cache"), exist_ok=True)

def create_global_objects():
    # Setup locales
    gl.tray_icon = TrayIcon()
    # gl.tray_icon.run_detached()

    gl.lm = LocaleManager(csv_path=os.path.join(main_path, "locales", "locales.csv"))
    gl.lm.set_to_os_default()
    gl.lm.set_fallback_language("en_US")

    gl.flatpak_permission_manager = FlatpakPermissionManager()

    gl.gnome_extensions = GnomeExtensions()

    gl.settings_manager = SettingsManager()

    # Before anything that can report to the user -- the plugin load below is
    # the earliest caller, and the facade's desktop-notification fallback
    # reads the app settings.
    gl.notify = Notify()

    gl.signal_manager = SignalManager()

    gl.media_manager = MediaManager()
    gl.asset_manager_backend = AssetManagerBackend()
    gl.page_manager = PageManagerBackend(gl.settings_manager)
    gl.page_manager.remove_old_backups()
    gl.page_manager.backup_pages()
    gl.icon_pack_manager = IconPackManager()
    gl.wallpaper_pack_manager = WallpaperPackManager()
    gl.sd_plus_bar_wallpaper_pack_manager = SDPlusBarWallpaperPackManager()

    # Store
    gl.store_backend = StoreBackend()

    # Plugin Manager
    gl.plugin_manager = PluginManager()
    gl.plugin_manager.load_plugins(show_notification=True)
    gl.plugin_manager.generate_action_index()

    gl.window_grabber = WindowGrabber()

    if os.getenv("WAYLAND_DISPLAY", False):
        gl.wayland = Wayland()

    # Before LockScreenManager on purpose: its __init__ starts
    # setup() on a daemon thread immediately, so a lock arriving in the gap
    # would find gl.presence_monitor still None and be dropped.
    gl.presence_monitor = PresenceMonitor()

    gl.lock_screen_detector = LockScreenManager()

    
    # gl.dekstop_grabber = DesktopGrabber()

@log.catch
def update_assets():
    settings = gl.settings_manager.load_settings_from_file(os.path.join(gl.DATA_PATH, "settings", "settings.json"))
    auto_update = AppSettings(settings).auto_update

    if gl.argparser.parse_args().devel:
        auto_update = False

    if not auto_update:
        log.info("Skipping store asset update")
        return

    log.info("Updating store assets")
    start = time.time()
    number_of_installed_updates = gl.store_backend.update_everything()
    if isinstance(number_of_installed_updates, NoConnectionError):
        log.error("Failed to update store assets")
        gl.notify.error("Failed to update store assets")
        return
    log.info(f"Updating {number_of_installed_updates} store assets took {time.time() - start} seconds")

    if number_of_installed_updates <= 0:
        return

    # Show toast in ui
    gl.notify.info(f"{number_of_installed_updates} assets updated")


def handle_listing_commands():
    """
    Handle --list-devices and --list-pages commands
    Returns True if a listing command was handled, False otherwise
    """
    args = gl.argparser.parse_args()
    
    if args.list_devices:
        print("Scanning for connected StreamDeck devices...")
        print()
        
        # We need to initialize deck manager to scan for devices
        try:
            # Minimal initialization to scan for devices
            from StreamDeck.DeviceManager import DeviceManager
            devices = DeviceManager().enumerate()
            
            if not devices:
                print("No StreamDeck devices found.")
                print("\nTips:")
                print("- Make sure your StreamDeck is connected via USB")
                print("- Check that the device is recognized by your system")
                print("- Try running with sudo if you have permission issues")
                return True
            
            print(f"Found {len(devices)} StreamDeck device(s):")
            print()
            
            for i, device in enumerate(devices):
                print(f"Device {i+1}:")
                try:
                    # Try to get basic info without opening if possible
                    device_id = getattr(device, 'id', lambda: 'Unknown')()
                    print(f"  Device ID: {device_id}")
                    
                    # Try to get info that doesn't require opening the device
                    try:
                        deck_type = getattr(device, 'deck_type', lambda: 'Unknown StreamDeck')()
                        print(f"  Product Name: {deck_type}")
                    except Exception:
                        # Genuinely unknowable: whatever the HID backend raises
                        # for a device we may not have permission to talk to.
                        print("  Product Name: Unknown (permission issue)")
                    
                    # Try to open device to get detailed info
                    device_opened = False
                    try:
                        if not device.is_open():
                            device.open()
                            device_opened = True
                        
                        print(f"  Serial Number: {device.get_serial_number()}")
                        key_layout = device.key_layout()
                        print(f"  Key Layout: {key_layout[1]}x{key_layout[0]} ({device.key_count()} keys)")
                        
                        if hasattr(device, 'dial_count') and device.dial_count() > 0:
                            print(f"  Dials: {device.dial_count()}")
                        if hasattr(device, 'is_touch') and device.is_touch():
                            print("  Touchscreen: Yes")
                        print(f"  Connected: {'Yes' if device.connected() else 'No'}")
                        
                        if device_opened:
                            device.close()
                            
                    except PermissionError:
                        print("  Status: Permission denied")
                        print("  Note: Run 'sudo python main.py --list-devices' or install udev rules")
                    except Exception as open_error:
                        print(f"  Status: Could not access device ({open_error})")
                        print("  Note: This may be a permission issue or device is in use")
                        
                except Exception as e:
                    print(f"  Error: {e}")
                    if "permission" in str(e).lower() or "access" in str(e).lower():
                        print("  Note: Try running with sudo or install proper udev rules")
                
                print()
        except ImportError:
            print("Error: StreamDeck library not available")
        except Exception as e:
            print(f"Error scanning devices: {e}")
        
        # Add helpful information about permissions
        print("\nTroubleshooting:")
        print("- If you see permission errors, try: sudo python main.py --list-devices")
        print("- For permanent fix, install udev rules: sudo cp udev.rules /etc/udev/rules.d/70-streamdeck.rules")
        print("- Then run: sudo udevadm control --reload-rules && sudo udevadm trigger")
        print("- After installing udev rules, unplug and replug your StreamDeck")
        
        return True
    
    if args.list_pages:
        print("Scanning for available pages...")
        print()
        
        try:
            # Try to get pages from the file system
            import os
            data_path = gl.DATA_PATH if hasattr(gl, 'DATA_PATH') else DEFAULT_DATA_PATH
            pages_dir = os.path.join(data_path, "pages")
            
            if not os.path.exists(pages_dir):
                print(f"Pages directory not found: {pages_dir}")
                print("\nThis might mean Deckard hasn't been set up yet.")
                return True
            
            page_files = [f for f in os.listdir(pages_dir) if f.endswith('.json') and not f.startswith('.')]
            
            if not page_files:
                print("No pages found.")
                print(f"\nPages should be located in: {pages_dir}")
                return True
            
            print(f"Found {len(page_files)} page(s):")
            print()
            
            for page_file in sorted(page_files):
                page_name = os.path.splitext(page_file)[0]
                page_path = os.path.join(pages_dir, page_file)
                
                try:
                    # Try to read basic info from the page file
                    import json
                    with open(page_path, 'r') as f:
                        page_data = json.load(f)
                    
                    print(f"  {page_name}")
                    
                    # Count items with states
                    items_with_states = 0
                    for input_type in ['keys', 'dials', 'touchscreens']:
                        if input_type in page_data:
                            for item_id, item_data in page_data[input_type].items():
                                if 'states' in item_data and item_data['states']:
                                    states_count = len(item_data['states'])
                                    items_with_states += 1
                                    if states_count > 1:
                                        print(f"    - {input_type[:-1]} {item_id}: {states_count} states")
                    
                    if items_with_states == 0:
                        print("    - No configured items")
                    
                except Exception as e:
                    print(f"    - Error reading page: {e}")
                
                print()
                    
        except Exception as e:
            print(f"Error scanning pages: {e}")
        
        return True
    
    return False

def make_api_calls():
    """Apply this invocation's --change-page / --change-state requests.

    True means a running instance took them and this process is finished;
    False means they are parked (or there were none) and this process boots.
    Everything but reading argv and leaving the process lives in cli_forward,
    where a test can reach it -- this module re-execs itself on import.
    """
    verdict = cli_forward.forward_cli_requests(gl.argparser.parse_args())
    for failure in verdict.failures:
        print(failure, file=sys.stderr)
    if verdict.failures:
        sys.exit(1)
    return verdict.handled


@log.catch
def main():
    # Safety net first: from here on, uncaught exceptions on the
    # main thread, GLib callbacks, plain threads and GC-time finalizers all
    # route through loguru. Until config_logger() below adds the file/ring
    # sinks these land on loguru's default stderr sink; afterwards the same
    # hooks hit all three -- no re-install needed.
    install_exception_hooks()

    # Handle listing commands first (they don't need full initialization)
    if handle_listing_commands():
        return

    if make_api_calls():
        return

    # Sinks up before the instance gate and the migrations, so the
    # earliest startup phase reaches logs.log + the ring (deep-audit §4 App
    # shell: this phase used to be stderr-only). Deliberately AFTER the two
    # early returns above: a short-lived CLI invocation must not open (and
    # possibly rotate) the running app's log files.
    config_logger()
    redirect_faulthandler(os.path.join(gl.DATA_PATH, "logs"))

    gsk_render_env_var = os.environ.get("GSK_RENDERER")
    if gsk_render_env_var != "ngl":
        log.warning('Should you get an Gtk X11 error preventing the app from starting please add '
                    'GSK_RENDERER=ngl to your "/etc/environment" file')

    # The application object exists before anything it will own: registering
    # it is what decides whether this launch is the instance, and that verdict
    # has to come before the first expensive or exclusive thing happens.
    app = App(application_id=appinfo.APP_ID)

    try:
        decision = instance_gate.establish(
            app,
            publish=start_dbus_service,
            close_running=gl.argparser.parse_args().close_running,
        )
    except instance_gate.LaunchAborted as e:
        log.error(str(e))
        sys.exit(1)

    if decision is instance_gate.Decision.REMOTE:
        # The running instance takes it from here: GApplication forwards this
        # activation to it, and its own activate handler presents the window.
        # Best-effort on purpose -- if the primary dies between the two calls
        # the forward errors, and there is still nothing for this process to
        # do but leave.
        try:
            app.activate()
        except Exception as e:
            log.warning(f"Could not present the running instance's window: {e}")
        log.info("Already running, exiting")
        sys.exit(0)

    migration_manager = MigrationManager()
    # Add migrators
    migration_manager.add_migrator(Migrator_1_5_0())
    migration_manager.add_migrator(Migrator_1_5_0_beta_5())
    # Run migrators
    migration_manager.run_migrators()

    create_global_objects()

    setup_autostart(gl.settings_manager.app().autostart)
    ensure_app_desktop_entry()
    
    create_cache_folder()
    threading.Thread(target=update_assets, name="update_assets").start()

    from src.backend.DeckManagement.Subclasses.video_cache_sweeper import sweep_stale_video_caches
    threading.Thread(target=sweep_stale_video_caches, args=(15,), name="video_cache_sweep", daemon=True).start()

    # Diagnostic only -- no-ops unless SC_MEM_TELEMETRY=1 (docs/memory-footprint-plan.md Phase 0).
    from src.backend.mem_telemetry import start_if_enabled as start_mem_telemetry
    start_mem_telemetry()

    log.info("Loading app")
    gl.deck_manager = DeckManager()
    gl.deck_manager.load_decks()

    # Every global on_quit dereferences now exists -- the deck manager last of
    # them, and unguarded (stop_boot_rescan). Installing the handlers any
    # earlier would turn a TERM arriving in the gap into an AttributeError that
    # aborts the teardown before the plugin backends are terminated, and the
    # re-entry latch would send every later quit route to its early return: the
    # orphan this teardown exists to prevent, reachable only by force_quit.
    # Still before run(), so PyGObject's register_sigint_fallback finds the
    # custom SIGINT handler and stays inert.
    app.register_signal_handlers()

    # Published just before the loop starts, not at construction: everything
    # that reports to the user during boot (plugin load notifications) checks
    # this slot and defers onto the startup queue while it is None. Setting it
    # earlier would route that traffic through an application whose window does
    # not exist yet.
    gl.app = app
    app.run(gl.argparser.parse_args().app_args)

if __name__ == "__main__":
    main()


log.trace("Reached end of main.py")
