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

# Cap the glibc per-thread arenas before the allocation-heavy imports run.
# glibc reads these variables at libc init, so the process must re-exec to
# apply them. SC_REEXEC stops a loop, and a set MALLOC_ARENA_MAX lets a
# packaged launcher skip the re-exec. sys.orig_argv keeps interpreter flags
# such as -X. execve replaces this process, so this must precede the DBus
# calls in main().
if "MALLOC_ARENA_MAX" not in os.environ and "SC_REEXEC" not in os.environ:
    os.environ["MALLOC_ARENA_MAX"] = "2"
    os.environ["MALLOC_TRIM_THRESHOLD_"] = "131072"
    os.environ["SC_REEXEC"] = "1"
    os.execve(sys.executable, sys.orig_argv, os.environ)

import setproctitle

setproctitle.setproctitle("Deckard")

# Dump all-thread tracebacks on a fatal signal, or on demand via SIGQUIT.
# Output goes to stderr here. main() re-points it at logs/faulthandler.log
# after it resolves gl.DATA_PATH, which --data or the settings file can set.
import faulthandler, signal
try:
    faulthandler.enable()
    faulthandler.register(signal.SIGQUIT)
except (AttributeError, ValueError, OSError):
    pass

# One-time rename migration from StreamController to Deckard. It moves the
# ~/.var/app tree to the new id and leaves a symlink at the old path. It must
# run before the globals import, because globals.py makes the data directory
# at import time, which breaks the migration's existence checks.
import appinfo
from rebrand_migration import migrate as _rebrand_migrate, migrate_native_var_app_to_xdg as _xdg_migrate
_rebrand_migrate()
# Native only, and does nothing under flatpak. It moves ~/.var/app/<id> to
# the XDG data dir, after the rename, so the renamed tree lands first.
_xdg_migrate()

import sys
from loguru import logger as log
import os
import time
import threading

# Cap the OpenCV parallel_for_ pool before the first cv2 call. OpenCV builds
# the pool lazily and sizes it to nproc, which starts one background thread
# per core. cvtColor is the only parallel_for_ user here. PIL does the
# resizing, and FFmpeg owns the video threads, so this knob leaves both alone.
import cv2
cv2.setNumThreads(2)

import globals as gl

from src.app import App
from src.api import start_dbus_service
from src.backend.DeckManagement.DeckManager import DeckManager
from locales.LocaleManager import LocaleManager
from src.backend.MediaManager import MediaManager
from src.backend.AssetManagerBackend import AssetManagerBackend
from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
from src.backend.SettingsManager import SettingsManager
from src.backend.PluginManager.PluginManager import PluginManager
from src.backend.IconPackManagement.IconPackManager import IconPackManager
from src.backend.WallpaperPackManagement.WallpaperPackManager import WallpaperPackManager
from src.backend.SDPlusBarWallpaperPackManagement.SDPlusBarWallpaperPackManager import SDPlusBarWallpaperPackManager
from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.store_result import Err
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

from src.backend.Migration.MigrationManager import MigrationManager
from src.backend.Migration.Migrators.Migrator_1_5_0 import Migrator_1_5_0
from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

import globals as gl

DEFAULT_DATA_PATH = os.path.expanduser(f"~/.var/app/{appinfo.APP_ID}/data")

# Rotated files kept per log sink, oldest deleted first. loguru keeps every
# rotation without this bound, so the log directory grows without limit.
LOG_RETENTION_FILES = 10
# By default the files and the in-app ring take DEBUG and up, and the console
# takes INFO and up. TRACE fills the files fast. SC_LOG_TRACE=1 puts every
# sink back to TRACE; the value must be exactly "1", so SC_LOG_TRACE=off
# cannot read as on. config_logger() runs once at boot, so this reads at
# import time.
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
    # Create the log files. Omit backtrace= and diagnose=. The redaction
    # patcher clears record["exception"] and folds a scrubbed traceback into
    # the message before any sink reads the record, so both flags stay inert.
    log.add(os.path.join(gl.DATA_PATH, "logs/logs.log"), rotation="3 days",
            retention=LOG_RETENTION_FILES, level=FILE_LOG_LEVEL)
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
    gl.tray_icon = TrayIcon()
    # gl.tray_icon.run_detached()

    gl.lm = LocaleManager(csv_path=os.path.join(main_path, "locales", "locales.csv"))
    gl.lm.set_to_os_default()
    gl.lm.set_fallback_language("en_US")

    gl.flatpak_permission_manager = FlatpakPermissionManager()

    gl.gnome_extensions = GnomeExtensions()

    gl.settings_manager = SettingsManager()

    # Construct before anything that reports to the user. The plugin load
    # below is the first caller, and the desktop-notification fallback reads
    # the app settings.
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

    gl.store_backend = StoreBackend()

    gl.plugin_manager = PluginManager()
    gl.plugin_manager.load_plugins(show_notification=True)
    gl.plugin_manager.generate_action_index()

    gl.window_grabber = WindowGrabber()

    if os.getenv("WAYLAND_DISPLAY", False):
        gl.wayland = Wayland()

    # Construct before LockScreenManager, whose __init__ starts setup() on a
    # daemon thread at once. A lock event in the gap finds gl.presence_monitor
    # still None, and the event is lost.
    gl.presence_monitor = PresenceMonitor()

    gl.lock_screen_detector = LockScreenManager()

    
    # gl.dekstop_grabber = DesktopGrabber()

@log.catch
def update_assets():
    auto_update = gl.settings_manager.app().auto_update

    if gl.argparser.parse_args().devel:
        auto_update = False

    if not auto_update:
        log.info("Skipping store asset update")
        return

    log.info("Updating store assets")
    start = time.time()
    result = gl.store_backend.update_everything()
    if isinstance(result, Err):
        log.error("Failed to update store assets")
        gl.notify.error("Failed to update store assets")
        return
    number_of_installed_updates = result.value
    log.info(f"Updating {number_of_installed_updates} store assets took {time.time() - start} seconds")

    if number_of_installed_updates <= 0:
        return

    # Show toast in ui
    gl.notify.info(f"{number_of_installed_updates} assets updated")


def handle_listing_commands():
    """Run --list-devices and --list-pages. True means this call handled one."""
    args = gl.argparser.parse_args()
    
    if args.list_devices:
        print("Scanning for connected StreamDeck devices...")
        print()
        
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
                    # Read the basic info without opening the device
                    device_id = getattr(device, 'id', lambda: 'Unknown')()
                    print(f"  Device ID: {device_id}")
                    
                    try:
                        deck_type = getattr(device, 'deck_type', lambda: 'Unknown StreamDeck')()
                        print(f"  Product Name: {deck_type}")
                    except Exception:
                        # The HID backend raises an unspecified error when the
                        # process has no permission for the device.
                        print("  Product Name: Unknown (permission issue)")
                    
                    # Open the device to read the detailed info
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
            # Read the pages from the file system
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
                    # Read the basic info from the page file
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
    """Apply the --change-page and --change-state requests from argv.

    True means a running instance took them and this process stops.
    False means the requests are parked, or absent, and this process boots.
    Everything but reading argv and leaving the process lives in cli_forward,
    where a test can reach it, because this module re-execs itself on import.
    """
    verdict = cli_forward.forward_cli_requests(gl.argparser.parse_args())
    for failure in verdict.failures:
        print(failure, file=sys.stderr)
    if verdict.failures:
        sys.exit(1)
    return verdict.handled


@log.catch
def main():
    # Install first. From here on, uncaught exceptions on the main thread, in
    # GLib callbacks, in plain threads and in finalizers all route through
    # loguru. They go to stderr until config_logger() adds the file and ring
    # sinks. The same hooks then feed all three sinks, with no re-install.
    install_exception_hooks()

    # Run the listing commands first; they need no full initialization
    if handle_listing_commands():
        return

    if make_api_calls():
        return

    # Add the sinks before the instance gate and the migrations, so the
    # earliest startup phase reaches logs.log and the ring. Keep this after
    # the two early returns, because a short-lived CLI call must not open, or
    # rotate, the running app's log files.
    config_logger()
    redirect_faulthandler(os.path.join(gl.DATA_PATH, "logs"))

    gsk_render_env_var = os.environ.get("GSK_RENDERER")
    if gsk_render_env_var != "ngl":
        log.warning('Should you get an Gtk X11 error preventing the app from starting please add '
                    'GSK_RENDERER=ngl to your "/etc/environment" file')

    # Create the application object before anything it owns. Registration
    # decides whether this launch is the primary instance, and that decision
    # must come before the first expensive or exclusive step.
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
        # The requests that make_api_calls() parked belong to the instance
        # that owns the name. This process parked them while nothing owned the
        # name, and it now exits without opening a deck.
        try:
            failures = cli_forward.forward_parked_requests()
        except Exception as e:
            # The requests are already popped. A loss plus an exit code of 0
            # is the silent drop this arm prevents.
            failures = [f"Could not hand the parked requests over: {e}"]
        for failure in failures:
            print(failure, file=sys.stderr)

        # GApplication forwards this activation to the running instance, and
        # its activate handler presents the window. If the primary instance
        # dies between the two calls, the forward fails and this process only
        # exits.
        try:
            app.activate()
        except Exception as e:
            log.warning(f"Could not present the running instance's window: {e}")
        log.info("Already running, exiting")
        sys.exit(1 if failures else 0)

    migration_manager = MigrationManager()
    migration_manager.add_migrator(Migrator_1_5_0())
    migration_manager.add_migrator(Migrator_1_5_0_beta_5())
    migration_manager.run_migrators()

    create_global_objects()

    setup_autostart(gl.settings_manager.app().autostart)
    ensure_app_desktop_entry()
    
    create_cache_folder()
    threading.Thread(target=update_assets, name="update_assets").start()

    from src.backend.DeckManagement.Subclasses.video_cache_sweeper import sweep_stale_video_caches
    threading.Thread(target=sweep_stale_video_caches, args=(15,), name="video_cache_sweep", daemon=True).start()

    # Diagnostic only. Does nothing unless SC_MEM_TELEMETRY=1.
    from src.backend.mem_telemetry import start_if_enabled as start_mem_telemetry
    start_mem_telemetry()

    log.info("Loading app")
    gl.deck_manager = DeckManager()
    gl.deck_manager.load_decks()

    # Install here. on_quit reads gl.deck_manager without a guard, and the
    # deck manager is the last global to exist. An earlier install lets a TERM
    # in the gap raise AttributeError, which aborts the teardown before the
    # plugin backends stop, and the re-entry latch then sends every later quit
    # route to its early return. Keep this before run(), so PyGObject's
    # register_sigint_fallback finds the custom SIGINT handler and stays inert.
    app.register_signal_handlers()

    # Publish the slot just before the loop starts. Boot-time user reports,
    # such as plugin load notifications, read this slot and defer onto the
    # startup queue while it is None. An earlier assignment routes that traffic
    # through an application that has no window yet.
    gl.app = app
    app.run(gl.argparser.parse_args().app_args)

if __name__ == "__main__":
    main()


log.trace("Reached end of main.py")
