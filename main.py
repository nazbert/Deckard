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
# Must run before quit_running()/make_api_calls() in main() -- execve
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

import usb.core
import usb.util
from StreamDeck.DeviceManager import DeviceManager

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

from gi.repository import Gio, GLib

# Import own modules
from src.app import App
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
from src.backend import single_instance

# Migration
from src.backend.Migration.MigrationManager import MigrationManager
from src.backend.Migration.Migrators.Migrator_1_5_0 import Migrator_1_5_0
from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

# Import globals
import globals as gl

# Define constants
DEFAULT_DATA_PATH = os.path.expanduser(f"~/.var/app/{appinfo.APP_ID}/data")
MAX_REASONABLE_X = 10
MAX_REASONABLE_Y = 10

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

class Main:
    def __init__(self, application_id, deck_manager):
        # Launch gtk application
        self.app = App(application_id=application_id, deck_manager=deck_manager)

        gl.app = self.app

        self.app.run(gl.argparser.parse_args().app_args)

@log.catch
def load():
    log.info("Loading app")
    gl.deck_manager = DeckManager()
    gl.deck_manager.load_decks()
    gl.main = Main(application_id=appinfo.APP_ID, deck_manager=gl.deck_manager)

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

@log.catch
def reset_all_decks():
    # Find all USB devices
    devices = usb.core.find(find_all=True, idVendor=DeviceManager.USB_VID_ELGATO)
    for device in devices:
        try:
            # Check if it's a StreamDeck
            if device.idProduct in [
                DeviceManager.USB_PID_STREAMDECK_ORIGINAL,
                DeviceManager.USB_PID_STREAMDECK_ORIGINAL_V2,
                DeviceManager.USB_PID_STREAMDECK_MINI,
                DeviceManager.USB_PID_STREAMDECK_XL,
                DeviceManager.USB_PID_STREAMDECK_MK2,
                DeviceManager.USB_PID_STREAMDECK_PEDAL,
                DeviceManager.USB_PID_STREAMDECK_PLUS,
                DeviceManager.USB_PID_STREAMDECK_MK2_SCISSOR,
                DeviceManager.USB_PID_STREAMDECK_MK2_MODULE,
                DeviceManager.USB_PID_STREAMDECK_MINI_MK2_MODULE,
                DeviceManager.USB_PID_STREAMDECK_XL_V2_MODULE,
            ]:
                # Reset deck
                usb.util.dispose_resources(device)
                device.reset()
        except (usb.core.USBError, NotImplementedError):
            log.error("Failed to reset deck, maybe it's already connected to another instance? Skipping...")

# Every CLI-side D-Bus call carries this instead of the 25s bus default:
# without it a wedged instance that accepts but never replies blocks startup
# for that long.
DBUS_CALL_TIMEOUT_MS = 5000


def name_has_owner(session_bus: Gio.DBusConnection, name: str) -> bool:
    """Is `name` owned on the session bus right now?

    Asking the bus daemon (and with NO_AUTO_START) is what keeps a probe a
    probe: addressing a well-known name directly would D-Bus-activate it.
    """
    return session_bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (name,)),
        GLib.VariantType("(b)"),
        Gio.DBusCallFlags.NO_AUTO_START,
        DBUS_CALL_TIMEOUT_MS,
        None
    ).unpack()[0]


def activate_action(session_bus: Gio.DBusConnection, name: str, object_path: str,
                    action: str, parameter: GLib.Variant = None) -> None:
    """Invoke one of the running instance's GActions over org.gtk.Actions."""
    session_bus.call_sync(
        name,
        object_path,
        "org.gtk.Actions",
        "Activate",
        GLib.Variant("(sava{sv})", (action, [] if parameter is None else [parameter], {})),
        None,
        Gio.DBusCallFlags.NO_AUTO_START,
        DBUS_CALL_TIMEOUT_MS,
        None
    )


def is_no_reply(error: GLib.Error) -> bool:
    """The peer took the call and never answered.

    Two distinct shapes carry that meaning: a NoReply the bus hands back, and
    the timeout GDBus raises client-side when the reply never lands. Both
    leave the caller in the same position, so both are matched here.
    """
    if Gio.DBusError.get_remote_error(error) == "org.freedesktop.DBus.Error.NoReply":
        return True
    return error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT)


def quit_running():
    log.info("Checking if another instance is running")
    session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    running = False
    try:
        running = name_has_owner(session_bus, appinfo.APP_ID)
    except GLib.Error as e:
        # A probe that cannot complete reads as "nobody home", exactly as a
        # failed lookup did before: booting is the safer outcome than refusing
        # to start over a bus hiccup.
        log.debug(e)
    if not running:
        # Expected path on every normal launch -- not an error.
        log.info("No other instance running, continuing")

    if running:
        if gl.argparser.parse_args().close_running:
            log.info("Closing running instance")
            try:
                activate_action(session_bus, appinfo.APP_ID, appinfo.DBUS_OBJECT_PATH, "quit")
            except GLib.Error as e:
                if is_no_reply(e):
                    log.error("Could not close running instance: " + str(e))
                    sys.exit(0)
            time.sleep(5)

        else:
            activate_action(session_bus, appinfo.APP_ID, appinfo.DBUS_OBJECT_PATH, "reopen")
            log.info("Already running, exiting")
            sys.exit(0)

    # Transition guard for the rename (docs/rename-deckard-plan.md, Phase 2):
    # a pre-rename build still owning the old bus name is invisible to the
    # gate above, and reset_all_decks() below would USB-reset decks it owns.
    # Probe with NameHasOwner, and never address the old name directly:
    # a plain call to a well-known name activates it, which for the old id
    # could START an upstream install via its D-Bus service file -- the very
    # race this guard exists to prevent. NameHasOwner==False (the normal case)
    # is also the effective sunset: once nothing owns the old name, this is a
    # single cheap no-op round trip per launch.
    try:
        if not name_has_owner(session_bus, appinfo.OLD_APP_ID):
            return
    except GLib.Error as e:
        log.debug(f"Could not probe the pre-rename bus name: {e}")
        return
    log.warning("Pre-rename StreamController instance detected on the session bus; asking it to quit")
    try:
        activate_action(session_bus, appinfo.OLD_APP_ID, appinfo.OLD_DBUS_OBJECT_PATH, "quit")
    except GLib.Error as e:
        log.error(f"Could not close the pre-rename instance: {e}")
        return
    # Poll (bounded) for it to drop the name instead of a flat 5s sleep.
    for _ in range(25):
        try:
            if not name_has_owner(session_bus, appinfo.OLD_APP_ID):
                break
        except GLib.Error:
            break
        time.sleep(0.2)

def hand_off_to_lock_owner(closing: bool = False):
    """Called when single_instance.claim() lost the race: another
    launch is booting right now. Poll for it to finish; if it instead DIES
    mid-boot (releases the lock without ever owning the app name), take the
    lock over and continue booting -- RETURNING from this function means
    "proceed as the single instance". Every other outcome exits the process.

    `closing` (--close-running): the user asked to CLOSE the running
    instance, so if it is still alive after the grace period, fail loudly
    instead of presenting its window."""
    log.info("Another launch holds the single-instance lock; handing off")
    session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    owner_seen = False
    for _ in range(50):  # up to 10 s for the winner to finish booting
        try:
            if name_has_owner(session_bus, appinfo.APP_ID):
                owner_seen = True
                break
        except GLib.Error:
            break
        # The winner may have crashed mid-boot and released the lock (its
        # connection died): take it over rather than stranding the user's
        # session with zero instances. This cannot steal from a live winner
        # -- the claim only succeeds once the holder's connection is gone.
        if single_instance.claim(appinfo.APP_ID):
            log.warning("Lock holder vanished before owning the app name; taking over as the single instance")
            return
        time.sleep(0.2)
    if not owner_seen:
        # Never address an ownerless well-known name: it would D-Bus-
        # activate a service file if one ever ships (same rule as the
        # pre-rename probe in quit_running()).
        log.error("Timed out waiting for the winning launch to appear; exiting with no instance running")
        sys.exit(1)
    if closing:
        log.error("--close-running: the running instance did not exit within the grace period")
        sys.exit(1)
    try:
        activate_action(session_bus, appinfo.APP_ID, appinfo.DBUS_OBJECT_PATH, "reopen")
    except GLib.Error as e:
        log.warning(f"Could not hand off to the winning instance: {e}")
    sys.exit(0)

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

def validate_state_change_args(args):
    """
    Validate CLI arguments for --change-state
    Returns (is_valid, error_message)
    """
    if not args.change_state:
        return True, None
    
    for i, (serial_number, page_name, coords, state_number) in enumerate(args.change_state):
        # Validate serial number format (basic check)
        if not serial_number or not isinstance(serial_number, str):
            return False, f"Invalid serial number in argument {i+1}: '{serial_number}'"
        
        # Validate page name
        if not page_name or not isinstance(page_name, str):
            return False, f"Invalid page name in argument {i+1}: '{page_name}'"
        
        # Validate coordinate format
        if not coords or not isinstance(coords, str):
            return False, f"Invalid coordinates in argument {i+1}: '{coords}'"
        
        if ',' not in coords:
            return False, f"Invalid coordinate format in argument {i+1}: '{coords}'. Expected format: 'x,y' (e.g., '0,0')"
        
        try:
            x, y = map(int, coords.split(','))
            if x < 0 or y < 0:
                return False, f"Coordinates must be non-negative in argument {i+1}: '{coords}'"
            if x > MAX_REASONABLE_X or y > MAX_REASONABLE_Y:  # Reasonable bounds check
                return False, f"Coordinates seem too large in argument {i+1}: '{coords}'. Most StreamDecks have coordinates 0-4"
        except ValueError:
            return False, f"Invalid coordinate format in argument {i+1}: '{coords}'. Expected integers like '0,0'"
        
        # Validate state number
        try:
            state_num = int(state_number)
            if state_num < 0:
                return False, f"State number must be non-negative in argument {i+1}: '{state_number}'"
            if state_num > 20:  # Reasonable bounds check
                return False, f"State number seems too large in argument {i+1}: '{state_number}'. Most items have 1-5 states"
        except ValueError:
            return False, f"Invalid state number in argument {i+1}: '{state_number}'. Must be an integer"
    
    return True, None

def make_api_calls():
    args = gl.argparser.parse_args()
    has_page_requests = args.change_page
    has_state_requests = args.change_state
    
    if not has_page_requests and not has_state_requests:
        return False
    
    # Validate state change arguments before proceeding
    if has_state_requests:
        is_valid, error_msg = validate_state_change_args(args)
        if not is_valid:
            print(f"Error: {error_msg}", file=sys.stderr)
            print("\nUsage examples:", file=sys.stderr)
            print("  --change-state CL123456789 Main 0,0 1", file=sys.stderr)
            print("  --change-state CL123456789 Soundboard 2,1 0", file=sys.stderr)
            print("\nParameters:", file=sys.stderr)
            print("  SERIAL_NUMBER: Device serial (e.g., CL123456789)", file=sys.stderr)
            print("  PAGE_NAME: Page name (e.g., Main, Soundboard)", file=sys.stderr)
            print("  COORDINATES: Position as x,y (e.g., 0,0 for top-left)", file=sys.stderr)
            print("  STATE_NUMBER: State to change to (e.g., 0, 1, 2)", file=sys.stderr)
            sys.exit(1)
    
    session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    running = False
    try:
        running = name_has_owner(session_bus, appinfo.APP_ID)
    except GLib.Error:
        running = False

    # Handle page change requests
    if has_page_requests:
        for serial_number, page_name in args.change_page:
            if not running or args.close_running:
                gl.api_page_requests[serial_number] = page_name
            else:
                # Other instance is running - call dbus interfaces
                activate_action(session_bus, appinfo.APP_ID, appinfo.DBUS_OBJECT_PATH,
                                "change_page", GLib.Variant("as", [serial_number, page_name]))
                return True

    # Handle state change requests
    if has_state_requests:
        for serial_number, page_name, coords, state_number in args.change_state:
            if not running or args.close_running:
                try:
                    state_num = int(state_number)
                    gl.api_state_requests[serial_number] = {
                        "page_name": page_name,
                        "coords": coords,
                        "state": state_num
                    }
                except ValueError:
                    print(f"Error: Invalid state number '{state_number}'. Must be an integer.", file=sys.stderr)
                    sys.exit(1)
            else:
                # Other instance is running - call dbus interfaces
                activate_action(session_bus, appinfo.APP_ID, appinfo.DBUS_OBJECT_PATH,
                                "change_state", GLib.Variant("as", [serial_number, page_name, coords, state_number]))
                return True

    return False


    
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

    # Sinks up before the dbus probe / deck reset / migrations, so the
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

    # Dbus
    quit_running()
    # quit_running() catches a fully-booted instance; two launches booting
    # at the same moment (login autostart + session restore) both pass its
    # probe. The lock claim is the atomic tie-breaker, and it must happen
    # BEFORE reset_all_decks() so a losing launch never USB-resets decks
    # the winner is initializing (field incident 2026-07-16).
    closing = gl.argparser.parse_args().close_running
    if not single_instance.claim(appinfo.APP_ID, wait_seconds=10.0 if closing else 0.0):
        hand_off_to_lock_owner(closing=closing)

    reset_all_decks()

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

    load()

if __name__ == "__main__":
    main()


log.trace("Reached end of main.py")
