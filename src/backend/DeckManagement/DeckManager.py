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
# Import Python modules
import threading
import time
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.Devices import StreamDeck
from StreamDeck.ImageHelpers import PILHelper
from loguru import logger as log
from usbmonitor import USBMonitor
import usb.core
import usb.util
import os
import types


# Import own modules
from src.backend.DeckManagement.Subclasses.RemoteDeckManager import RemoteDeckManager
from src.backend.DeckManagement.Subclasses.RemoteDeck import RemoteDeck
from src.backend.DeckManagement.DeckController import DeckController, ClearAndCloseMsg
from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
from src.backend.SettingsManager import SettingsManager
from src.backend.DeckManagement.HelperMethods import get_sys_param_value, recursive_hasattr
from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck

# Import globals first to get IS_MAC
import globals as gl

import gi
from gi.repository import GLib

if not gl.IS_MAC:
    gi.require_version("Xdp", "1.0")
    from gi.repository import Xdp

ELGATO_VENDOR_ID = "0fd9"


class DeckManager:
    # Backoff schedule for the startup re-enumeration (issue #106): ~60s
    # total window. Instance-overridable so the harness can shrink it.
    BOOT_RESCAN_DELAYS: tuple[float, ...] = (2.0, 3.0, 5.0, 10.0, 15.0, 25.0)

    def __init__(self):
        #TODO: Maybe outsource some objects
        self.deck_controller: list[DeckController] = []
        # Guards concurrent add/remove of deck_controller (called from the USB
        # monitor, resume, Flatpak poll and media-thread error paths).
        self._controllers_lock = threading.Lock()
        # Serializes connect_new_decks() callers (USB hotplug monitor vs the
        # boot rescan below): the already-loaded check and the controller
        # registration must be atomic against each other, or two concurrent
        # enumerations of the same freshly-arrived deck both pass the check
        # and register it twice.
        self._connect_decks_lock = threading.Lock()
        # Startup re-enumeration (issue #106): armed by load_hardware_decks()
        # when the boot enumeration comes back empty (autostart racing USB
        # device init); stopped by deck arrival, exhausted backoff, or quit.
        self._boot_rescan_thread: threading.Thread | None = None
        self._boot_rescan_stop = threading.Event()
        self.fake_deck_controller = []
        self.settings_manager = SettingsManager()
        self.page_manager = gl.page_manager
        # self.page_manager.load_pages()

        # USB monitor to detect connections and disconnections
        self.usb_monitor = USBMonitor()
        self.usb_monitor.start_monitoring(on_connect=self.on_connect, on_disconnect=self.on_disconnect)

        self.flatpak_disconnect_thread = FlatpakDeckDisconnectThread(self)

        self.flatpak = False
        if not gl.IS_MAC:
            portal = Xdp.Portal.new()
            self.flatpak = portal.running_under_flatpak() # on_disconnect is not working under Flatpak - we use a separate thread #TODO: Find a better solution
        if self.flatpak:
            log.info("Running under Flatpak. Using separate thread to detect device disconnection.")
            self.flatpak_disconnect_thread.start()

        self.remote_deck_manager = RemoteDeckManager(self)
        if gl.settings_manager.get_app_settings().get("dev", {}).get("n-remote-decks", 0) > 0:
            self.load_remote_decks()


    def load_remote_decks(self):
        print(" load remote decks")
        self.remote_deck_manager.start()
        for controller in self.remote_deck_manager.deck_controllers:
            if controller in self.deck_controller:
                continue

            self.deck_controller.append(controller)
            if recursive_hasattr(gl, "app.main_win.leftArea.deck_stack"):
                # Add to deck stack
                for controller in self.remote_deck_manager.deck_controllers:
                    GLib.idle_add(gl.app.main_win.leftArea.deck_stack.add_page, controller)

        if recursive_hasattr(gl, "app.main_win.sidebar.page_selector"):
            GLib.idle_add(gl.app.main_win.sidebar.page_selector.update)

        if recursive_hasattr(gl, "app.main_win"):
            gl.app.main_win.check_for_errors()

    def remove_remote_decks(self):
        for controller in self.remote_deck_manager.deck_controllers:
            self.remove_controller(controller)
        gl.app.main_win.check_for_errors()
        self.remote_deck_manager.stop()

    def load_decks(self):
        if not gl.argparser.parse_args().skip_load_hardware_decks:
            self.load_hardware_decks()

        self.load_fake_decks()
    
    def load_hardware_decks(self):
        if gl.IS_MAC:
            return
        decks=DeviceManager().enumerate()
        for deck in decks:
            self.load_hardware_deck(deck)
        if not decks:
            # Autostart can race USB device init at boot: the deck isn't
            # enumerable yet, and the USB monitor only reports *future*
            # hotplug events -- without a re-scan the user must replug the
            # deck and restart the app (issue #106). Only WARN when a
            # hardware deck has been seen before (deck settings exist) --
            # on a machine that never had one this is normal, not alarming.
            message = "No decks enumerable at startup; starting bounded re-enumeration"
            if self._hardware_decks_expected():
                log.warning(message)
            else:
                log.info(message)
            self.start_boot_rescan()

    def _hardware_decks_expected(self) -> bool:
        """True when a hardware deck has been used on this install before:
        deck settings persist per serial under settings/decks, and fake/
        remote decks use recognizable serial prefixes."""
        decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
        try:
            names = os.listdir(decks_dir)
        except OSError:
            return False
        for name in names:
            base = os.path.splitext(name)[0]
            if base and not base.startswith(("fake-deck", "remote-deck")):
                return True
        return False

    def start_boot_rescan(self) -> None:
        """Re-enumerate decks in the background with bounded backoff
        (BOOT_RESCAN_DELAYS, ~60s total) after an empty startup enumeration.

        Never blocks startup (daemon thread) and stops on the first
        successful REGISTRATION (not mere enumerability -- see
        _boot_rescan_loop), on exhausted backoff, or promptly on app quit
        (stop_boot_rescan). Registration goes through connect_new_decks(),
        whose lock + already-loaded check guarantee a deck that arrives via
        the USB hotplug monitor mid-backoff is not registered a second
        time. Only ever *adds* fresh controllers -- it never touches (or
        resurrects) existing/closed ones.
        """
        if self._boot_rescan_thread is not None and self._boot_rescan_thread.is_alive():
            return
        self._boot_rescan_stop.clear()
        self._boot_rescan_thread = threading.Thread(
            target=self._boot_rescan_loop,
            name="BootDeckRescan",
            daemon=True,
        )
        self._boot_rescan_thread.start()

    def _boot_rescan_loop(self) -> None:
        for attempt, delay in enumerate(self.BOOT_RESCAN_DELAYS, start=1):
            if self._boot_rescan_stop.wait(delay):
                return
            if not gl.threads_running:
                return
            try:
                n_registered = self.connect_new_decks()
            except Exception as e:
                log.error(f"Boot deck rescan attempt {attempt} failed: {e}")
                continue
            # Stop only once a deck is actually REGISTERED (a controller
            # exists), never on mere enumerability: a deck that appears but
            # flakes its open (TransportError -1, the boot-storm failure)
            # would otherwise end the rescan with a success log while its
            # deck stays stranded -- with no future hotplug event, because
            # the device is already present (#106 review round 1). Failed
            # pickups simply leave the deck unloaded, so the next round's
            # connect_new_decks() retries it; the bounded schedule still
            # terminates if it never initializes.
            if n_registered > 0:
                log.info(f"Boot deck rescan attempt {attempt}: {n_registered} deck(s) registered")
                return
        log.info(
            "Boot deck rescan exhausted its backoff window without registering a deck "
            "(any initialization errors are logged above); USB hotplug monitoring remains active"
        )

    def stop_boot_rescan(self) -> None:
        """Stop a pending boot rescan promptly (called on app quit). Safe to
        call when no rescan is running."""
        self._boot_rescan_stop.set()
        thread = self._boot_rescan_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def load_hardware_deck(self, deck, attempts: int = 3, retry_delay: float = 0.5):
        deck_controller = self._init_deck_controller_with_retry(deck, attempts=attempts, retry_delay=retry_delay)
        if deck_controller is not None:
            self.deck_controller.append(deck_controller)

    def _init_deck_controller_with_retry(self, deck, attempts: int = 3, retry_delay: float = 0.5) -> DeckController | None:
        # Opening a deck and reading its serial right after open is occasionally
        # flaky (TransportError -1): retry, and never let one bad deck crash
        # startup. Shared by the startup path (load_hardware_deck) and the
        # hotplug/boot-rescan path (add_newly_connected_deck) -- the boot-storm
        # flake this retries is exactly as likely on a deck picked up mid-boot
        # by the rescan as on one enumerated at startup (#106 review round 1).
        for attempt in range(1, attempts + 1):
            try:
                if not deck.is_open():
                    # Resume-from-suspend handle reopen is the library's only
                    # mode now (plan §9.1, decided 2026-07-04) -- always on.
                    deck.open(True)
                return DeckController(self, deck)
            except StreamDeck.TransportError as e:
                log.warning(f"Transport error initializing deck (attempt {attempt}/{attempts}): {e}")
                try:
                    deck.close()
                except Exception:
                    pass
                if attempt < attempts:
                    time.sleep(retry_delay)
            except Exception as e:
                log.error(f"Failed to initialize deck, maybe it's already connected to another instance? Error: {e}")
                return None
        log.error("Giving up on deck after repeated transport errors; skipping it. Replugging the deck usually fixes this.")
        return None

    def load_fake_decks(self):
        old_n_fake_decks = len(self.fake_deck_controller)
        n_fake_decks = int(gl.settings_manager.load_settings_from_file(os.path.join(gl.DATA_PATH, "settings", "settings.json")).get("dev", {}).get("n-fake-decks", 0))

        if n_fake_decks > old_n_fake_decks:
            log.info(f"Loading {n_fake_decks - old_n_fake_decks} fake deck(s)")
            # Load difference in number of fake decks
            for controller in range(n_fake_decks - old_n_fake_decks):
                a = f"Fake Deck {len(self.fake_deck_controller)+1}"
                fake_deck = FakeDeck(serial_number = f"fake-deck-{len(self.fake_deck_controller)+1}", deck_type=f"Fake Deck {len(self.fake_deck_controller)+1}")
                self.add_newly_connected_deck(fake_deck, is_fake=True)

            # Update header deck switcher if the new deck is the only one
            if len(self.deck_controller) == 1 and False:
                # Check if ui is loaded - if not it will grab the controller automatically
                if recursive_hasattr(gl, "app.main_win.header_bar.deckSwitcher"):
                    gl.app.main_win.header_bar.deckSwitcher.set_show_switcher(True)

        elif n_fake_decks < old_n_fake_decks:
            # Remove difference in number of fake decks
            log.info(f"Removing {old_n_fake_decks - n_fake_decks} fake deck(s)")
            for controller in self.fake_deck_controller[-(old_n_fake_decks - n_fake_decks):]:
                # Remove controller from fake_decks
                self.fake_deck_controller.remove(controller)
                # Route through remove_controller (plan P1.3, design doc bug
                # 4): this used to just pop the two lists and detach the
                # stack page directly, never tearing the controller down at
                # all -- its media/tick threads and action executor ran
                # forever after "removing" a fake deck.
                self.remove_controller(controller)

            # Update header deck switcher if there are no more decks
            if len(self.deck_controller) == 0 and False:
                # Check if ui is loaded - if not it will grab the controller automatically
                if recursive_hasattr(gl, "app.main_win.header_bar.deckSwitcher"):
                    gl.app.main_win.header_bar.deckSwitcher.set_show_switcher(False)
        if hasattr(gl.app, "main_win"):
            gl.app.main_win.check_for_errors()

    def on_connect(self, device_id, device_info):
        log.info(f"Device {device_id} with info: {device_info} connected")
        # Check if it is a supported device
        if device_info["ID_VENDOR_ID"] != ELGATO_VENDOR_ID:
            return

        self.connect_new_decks()

    def connect_new_decks(self) -> int:
        """Register every enumerable deck that isn't already loaded.

        Serialized by _connect_decks_lock: the USB hotplug monitor and the
        boot rescan (start_boot_rescan) can call this concurrently, and the
        already-loaded check plus the registration below must be atomic
        against each other or the same deck registers twice.

        Returns the number of enumerated decks that are REGISTERED after
        this pass (already loaded or picked up here) -- the boot rescan's
        stop condition. Decks that enumerated but failed to initialize are
        deliberately not counted, so the rescan keeps retrying them.
        """
        # NOTE: serialization is global, not per-deck -- a slow open/retry of
        # deck A delays deck B's pickup. Safe: the usbmonitor is a poll-diff
        # loop that coalesces device changes, it never drops events while
        # this lock is held; the deferred deck is simply picked up when its
        # caller gets the lock.
        with self._connect_decks_lock:
            decks = DeviceManager().enumerate()

            # Get already loaded deck serial ids
            loaded_deck_ids = []
            for controller in self.deck_controller:
                loaded_deck_ids.append(controller.deck.id())

            for deck in decks:
                if deck.id() in loaded_deck_ids:
                    continue
                # Add deck
                self.add_newly_connected_deck(deck)

            # Recompute AFTER the adds: add_newly_connected_deck returns
            # without registering when a deck's open flaked out even after
            # retries, and those must not count as picked up.
            loaded_after = {controller.deck.id() for controller in self.deck_controller}
            n_registered = sum(1 for deck in decks if deck.id() in loaded_after)

        # Guarded: the boot rescan (or an early hotplug event) can fire
        # before the main window exists. idle_add: this method runs on the
        # USB monitor thread or the boot rescan thread, and
        # check_for_errors() is pure GTK work.
        if recursive_hasattr(gl, "app.main_win"):
            GLib.idle_add(gl.app.main_win.check_for_errors)

        return n_registered


    def on_disconnect(self, device_id, device_info):
        log.info(f"Device {device_id} with info: {device_info} disconnected")
        if device_info["ID_VENDOR_ID"] != ELGATO_VENDOR_ID:
            return

        for controller in list(self.deck_controller):
            if not controller.deck.connected():
                self.remove_controller(controller)

        # USB events can arrive before on_activate has set gl.app / main_win.
        if recursive_hasattr(gl, "app.main_win"):
            gl.app.main_win.check_for_errors()

    def remove_controller(self, deck_controller: DeckController) -> None:
        # Idempotent: several threads may call this for the same controller;
        # only the first removal proceeds to close().
        with self._controllers_lock:
            if deck_controller not in self.deck_controller:
                return
            self.deck_controller.remove(deck_controller)

        # UI detach first, as one early idle (plan P1.3): this method runs
        # from the USB monitor thread (or the Flatpak disconnect poll
        # thread), and DeckStack.remove_page does pure GTK work -- calling
        # it directly here was already a latent off-main-thread GTK bug.
        # Queuing it ahead of the slow close() below also means a fast
        # unplug/replug can't race a late detach against a fresh add_page
        # idle and leave two stack children registered for one serial.
        if recursive_hasattr(gl, "app.main_win.leftArea.deck_stack"):
            GLib.idle_add(gl.app.main_win.leftArea.deck_stack.remove_page, deck_controller)

        # The teardown sweep runs plugin hooks (step 6) and can block on a
        # wedged callback; it must never run on the USB monitor thread
        # (would stall future connect/disconnect events) or the shared
        # GtkHelper background pool (quit's shutdown_background_pool() would
        # cancel it mid-close) -- see DeckController.close()'s docstring.
        threading.Thread(
            target=deck_controller.close,
            args=(True,),
            name=f"DeckClose-{getattr(deck_controller, '_serial_number', None) or 'unknown'}",
            daemon=True,
        ).start()

    def get_controller_for_deck(self, deck: StreamDeck) -> DeckController | None:
        for controller in self.deck_controller:
            if controller.deck is deck:
                return controller

    def add_newly_connected_deck(self, deck:StreamDeck, is_fake: bool = False):
        # Retrying init (not a bare DeckController construction): a deck
        # arriving mid-boot-storm via hotplug or the boot rescan hits the
        # same flaky-open/serial-read window the startup path retries
        # (#106 review round 1). On final failure this returns without
        # registering, so a later rescan round (or replug) can try again.
        deck_controller = self._init_deck_controller_with_retry(deck)
        if deck_controller is None:
            return

        # Check if ui is loaded - if not it will grab the controller automatically
        if recursive_hasattr(gl, "app.main_win.leftArea.deck_stack"):
            # Add to deck stack
            GLib.idle_add(gl.app.main_win.leftArea.deck_stack.add_page, deck_controller)

        if recursive_hasattr(gl, "app.main_win.sidebar.page_selector"):
            GLib.idle_add(gl.app.main_win.sidebar.page_selector.update)



        self.deck_controller.append(deck_controller)
        if is_fake:
            self.fake_deck_controller.append(deck_controller)

        # The trailing dot ("app.main_win.") made this check always False and
        # the call below dead code.
        if not recursive_hasattr(gl, "app.main_win"):
            return
        gl.app.main_win.check_for_errors()

    def close_all(self):
        log.info("Closing all decks")
        # Submit the terminal ClearAndClose to every controller first, THEN
        # join each media thread with a bound (plan §2.4): the message drives
        # the writer's own clear+close, so this only waits for it to land --
        # the app's force_quit timer (or, on the headerBar quit path, nothing
        # -- this bounded join IS that path's only safety) backstops a stuck
        # writer.
        pending_joins: list["DeckController"] = []
        for controller in list(self.deck_controller):
            if controller.deck is None:
                continue
            if not controller.deck.is_open():
                continue

            log.info(f"Closing deck: {controller.deck.get_serial_number()}")
            media_player = getattr(controller, "media_player", None)
            if media_player is None:
                # No writer thread (e.g. controller failed mid-construction):
                # best-effort direct close, same as before this change.
                try:
                    controller.deck.close()
                except Exception as e:
                    log.error(f"Failed to close deck cleanly: {e}")
                continue
            try:
                media_player.submit_control(ClearAndCloseMsg())
                pending_joins.append(controller)
            except Exception as e:
                log.error(f"Failed to submit ClearAndClose for deck: {e}")

        for controller in pending_joins:
            controller.media_player.stop(timeout=2.0)

    def stop_usb_monitoring(self):
        self.usb_monitor.stop_monitoring(timeout=2)

    def reset_all_decks(self):
        # Find all USB devices
        devices = usb.core.find(find_all=True)
        for device in devices:
            try:
                # Check if it's a StreamDeck
                if device.idVendor == DeviceManager.USB_VID_ELGATO and device.idProduct in [
                    DeviceManager.USB_PID_STREAMDECK_ORIGINAL,
                    DeviceManager.USB_PID_STREAMDECK_ORIGINAL_V2,
                    DeviceManager.USB_PID_STREAMDECK_MINI,
                    DeviceManager.USB_PID_STREAMDECK_XL,
                    DeviceManager.USB_PID_STREAMDECK_MK2,
                    DeviceManager.USB_PID_STREAMDECK_PEDAL,
                    DeviceManager.USB_PID_STREAMDECK_PLUS,
                    DeviceManager.USB_PID_STREAMDECK_NEO
                ]:
                    # Reset deck
                    usb.util.dispose_resources(device)
                    device.reset()
            except:
                log.error("Failed to reset deck, maybe it's already connected to another instance? Skipping...")

    def get_connected_serials(self) -> list[str]:
        return [controller.serial_number() for controller in self.deck_controller]


class FlatpakDeckDisconnectThread(threading.Thread):
    def __init__(self, deck_manager: DeckManager):
        super().__init__(name="FlatpakDeckDisconnectThread")
        self.deck_manager = deck_manager

    def run(self):
        while gl.threads_running:
            time.sleep(2)
            for controller in list(self.deck_manager.deck_controller):
                if not controller.deck.connected():
                    self.deck_manager.remove_controller(controller)
                    gl.app.main_win.check_for_errors()
