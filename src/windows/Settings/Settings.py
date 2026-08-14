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
# Import gtk modules
import threading
import gi

from loguru import logger as log

from GtkHelper.debounce import TrailingDebouncer
from GtkHelper.GtkHelper import BetterPreferencesGroup
from autostart import setup_autostart
from src.backend.DeckManagement.HelperMethods import color_values_to_gdk, gdk_color_to_values, get_pango_font_description, get_values_from_pango_font_description
from src.backend.PresenceMonitor.PresenceMonitor import MODE_SCREENSAVER, MODE_SYSTEM_IDLE
from src.backend.SettingsManager import AppSettings
from src.backend.Store.StoreURL import parse_repo_url
from src.windows.Settings.PluginSettingsPage import PluginSettingsPage

# Import globals
import globals as gl

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

import os

class Settings(Adw.PreferencesWindow):
    def __init__(self):
        super().__init__(title="Settings")
        self.set_default_size(1000, 700)

        # Center settings win over main_win (depends on DE)
        self.set_transient_for(gl.app.main_win)
        # Keep the settings dialog on top of (and blocking) the main window
        self.set_modal(True)

        self.settings_json:dict = None
        self.load_json()

        self.general_page = GeneralPage(settings=self)
        self.ui_page = UIPage(settings=self)
        self.store_page = StorePage(settings=self)
        self.performance_page = PerformancePage(settings=self)
        self.dev_page = DevPage(settings=self)
        self.system_page = SystemPage(settings=self)
        self.plugin_page = PluginSettingsPage(settings=self)

        self.add(self.general_page)
        self.add(self.ui_page)
        self.add(self.store_page)
        self.add(self.performance_page)
        self.add(self.system_page)
        self.add(self.dev_page)
        self.add(self.plugin_page)

    @property
    def app(self) -> AppSettings:
        """Typed view onto the snapshot of this dialog.

        It is not the shared cached dict, so the batch-save behaviour of
        save_json() stays the same. Each access rebuilds it, because
        load_json() rebinds settings_json.
        """
        return AppSettings(self.settings_json)

    def load_json(self):
        # A snapshot read from disk, and not the shared app-settings dict.
        # This dialog edits its own picture of the file and writes the whole
        # picture back. See the batch-save note on the app property above. A
        # shared dict would publish half-made edits to the other writers.
        self.settings_json = gl.settings_manager.app_snapshot().data

    def save_json(self):
        gl.settings_manager.save_app_settings(self.settings_json)


class UIPage(Adw.PreferencesPage):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.set_title(gl.lm.get("settings-ui-settings-title"))
        self.set_icon_name("window-new-symbolic")

        self.add(UIPageGroup(settings=settings))

class UIPageGroup(Adw.PreferencesGroup):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title=gl.lm.get("settings-ui-settings-key-grid-header"))

        self.trayicon_row = Adw.SwitchRow(title=gl.lm.get("settings-show-tray-icon"), active=True)
        self.add(self.trayicon_row)

        self.emulate_row = Adw.SwitchRow(title=gl.lm.get("settings-emulate-at-double-click"), active=True)
        self.add(self.emulate_row)

        self.enable_fps_warnings_row = Adw.SwitchRow(title=gl.lm.get("settings.enable-fps-warnings"), active=True)
        self.add(self.enable_fps_warnings_row)

        self.allow_white_mode = Adw.SwitchRow(title=gl.lm.get("settings-allow-white-mode"), subtitle=gl.lm.get("settings-allow-white-mode-subtitle"), active=False)
        self.add(self.allow_white_mode)

        self.show_notifications = Adw.SwitchRow(title=gl.lm.get("settings-show-notifications"), subtitle=gl.lm.get("settings-show-notifications-subtitle"), active=True)
        self.add(self.show_notifications)

        self.auto_config_row = Adw.SwitchRow(title=gl.lm.get("settings-auto-open-action-config"), subtitle=gl.lm.get("settings-auto-open-action-config-subtitle"), active=True)
        self.add(self.auto_config_row)

        self.load_defaults()

        # Connect signals
        self.trayicon_row.connect("notify::active", self.on_trayicon_row_toggled)
        self.emulate_row.connect("notify::active", self.on_emulate_row_toggled)
        self.enable_fps_warnings_row.connect("notify::active", self.on_enable_fps_warnings_row_toggled)
        self.allow_white_mode.connect("notify::active", self.on_allow_white_mode_toggled)
        self.show_notifications.connect("notify::active", self.on_show_notifications_toggled)
        self.auto_config_row.connect("notify::active", self.on_auto_config_row_toggled)

    def load_defaults(self):
        app = self.settings.app
        self.trayicon_row.set_active(app.tray_icon)
        self.emulate_row.set_active(app.emulate_at_double_click)
        self.enable_fps_warnings_row.set_active(app.enable_fps_warnings)
        self.allow_white_mode.set_active(app.allow_white_mode)
        self.show_notifications.set_active(app.show_notifications)
        self.auto_config_row.set_active(app.auto_open_action_config)


    def on_trayicon_row_toggled(self, *args):
        self.settings.app.tray_icon = self.trayicon_row.get_active()

        self.settings.save_json()
        if self.settings.app.tray_icon:
            gl.tray_icon.start()
        else:
            gl.tray_icon.stop()

    def on_emulate_row_toggled(self, *args):
        self.settings.app.emulate_at_double_click = self.emulate_row.get_active()

        # Save
        self.settings.save_json()

    def on_enable_fps_warnings_row_toggled(self, *args):
        self.settings.app.enable_fps_warnings = self.enable_fps_warnings_row.get_active()

        # Save
        self.settings.save_json()

        # Inform all deck controllers
        for controller in gl.deck_manager.deck_controller:
            controller.media_player.set_show_fps_warnings(self.enable_fps_warnings_row.get_active())

    def on_allow_white_mode_toggled(self, *args):
        self.settings.app.allow_white_mode = self.allow_white_mode.get_active()

        if self.allow_white_mode.get_active():
            gl.app.style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        else:
            gl.app.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        # Save
        self.settings.save_json()

    def on_show_notifications_toggled(self, *args):
        self.settings.app.show_notifications = self.show_notifications.get_active()

        # Save
        self.settings.save_json()

    def on_auto_config_row_toggled(self, *args):
        self.settings.app.auto_open_action_config = self.auto_config_row.get_active()

        # Save
        self.settings.save_json()


class DevPage(Adw.PreferencesPage):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__()
        self.set_title(gl.lm.get("settings-dev-settings-title"))
        self.set_icon_name("text-editor-symbolic")

        self.add(FakeDecksGroup(settings=settings))
        self.add(RemoteDecksGroup(settings=settings))
        self.add(DataPathGroup(settings=settings))

class FakeDecksGroup(Adw.PreferencesGroup):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title=gl.lm.get("settings-fake-decks-header"))

        self.n_fake_decks_row = Adw.SpinRow.new_with_range(min=0, max=3, step=1)
        self.n_fake_decks_row.set_title(gl.lm.get("settings-number-of-fake-decks"))
        self.n_fake_decks_row.set_subtitle(gl.lm.get("settings-number-of-fake-decks-hint"))
        self.n_fake_decks_row.set_range(0, 3)
        self.add(self.n_fake_decks_row)

        self.load_defaults()

        # Connect signals
        self.n_fake_decks_row.connect("changed", self.on_n_fake_decks_row_changed)

    def load_defaults(self):
        self.n_fake_decks_row.set_value(self.settings.app.n_fake_decks)

    def on_n_fake_decks_row_changed(self, *args):
        #FIXME: For some reason this gets called twice
        # Cast with int(). The SpinRow returns a float, and the setting is a
        # count, as n-cached-pages is.
        self.settings.app.n_fake_decks = int(self.n_fake_decks_row.get_value())

        # Save
        self.settings.save_json()

        # Reload decks
        gl.deck_manager.load_fake_decks()


class RemoteDecksGroup(Adw.PreferencesGroup):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title="Remote Decks")

        self.n_remote_decks_row = Adw.SpinRow.new_with_range(min=0, max=1, step=1)
        self.n_remote_decks_row.set_title("Number of remote decks")
        self.n_remote_decks_row.set_subtitle("Use remote.sc.core447.com to connect (beta)")
        self.n_remote_decks_row.set_range(0, 1)
        self.add(self.n_remote_decks_row)

        self.load_defaults()

        # Connect signals
        self.n_remote_decks_row.connect("changed", self.on_row_changed)

    def load_defaults(self):
        self.n_remote_decks_row.set_value(gl.settings_manager.app().n_remote_decks)

    def on_row_changed(self, *args):
        #FIXME: For some reason this gets called twice
        # Cast with int(). The SpinRow returns a float, and the setting is a
        # count, as n-cached-pages is.
        n_decks = int(self.n_remote_decks_row.get_value())
        app_settings = gl.settings_manager.app()
        app_settings.n_remote_decks = n_decks

        # Save
        app_settings.save()

        if n_decks > 0:
            gl.deck_manager.load_remote_decks()
        else:
            gl.deck_manager.remove_remote_decks()


class DataPathGroup(Adw.PreferencesGroup):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title="Data path")

        self.data_path = Adw.EntryRow(title="Data path (requires restart)", show_apply_button=True)
        self.add(self.data_path)

        self.open_data_path_button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        self.open_data_path_button.connect("clicked", self.on_open_data_path_button_clicked)
        self.data_path.add_suffix(self.open_data_path_button)

        self.load_defaults()

        # Connect signals.
        # Persist only on an explicit apply, which is Enter or the check
        # button. A persist on notify::text saves every keystroke, so a
        # half-typed edit that the user abandons becomes the data path that
        # globals.py adopts at the next launch, which creates a wrong
        # directory and boots an empty profile. A close without an apply now
        # discards the edit.
        self.data_path.connect("apply", self.on_data_path_apply)

    def load_defaults(self):
        static_settings = gl.settings_manager.get_static_settings()
        self.data_path.set_text(static_settings.get("data-path", gl.DATA_PATH))

    def on_data_path_apply(self, *args):
        new_path = os.path.expanduser(self.data_path.get_text().strip())

        if not self._validate_data_path(new_path):
            self.data_path.add_css_class("error")
            self.data_path.set_tooltip_text("Path must be absolute and creatable/writable")
            return
        self.data_path.remove_css_class("error")
        self.data_path.set_tooltip_text(None)

        # Show the expanded path in the row, so the user sees the value that
        # the app stores and adopts at boot. The store holds the expanded
        # value, not the "~/..." text that the user typed.
        if self.data_path.get_text() != new_path:
            self.data_path.set_text(new_path)

        static_settings = gl.settings_manager.get_static_settings()
        old_path = static_settings.get("data-path")
        if old_path and old_path != new_path:
            # Keep the previous value recoverable (manually, via the static
            # settings file) in case the new location turns out to be wrong.
            static_settings["data-path-previous"] = old_path
        static_settings["data-path"] = new_path
        gl.settings_manager.save_static_settings(static_settings)

    @staticmethod
    def _validate_data_path(path: str) -> bool:
        """True when a data path is absolute and usable.

        Usable means an existing writable directory, or a directory that this
        call creates. It runs on the GTK main thread, and only on an explicit
        apply, not per keystroke.
        """
        # globals.py creates the directory at boot in any case, and a create
        # here shows the failure while the user still looks at the row. The
        # stat and the makedirs can stall the UI on a hung network mount, and
        # that cost arrives once, when the user commits.
        if not path or not os.path.isabs(path):
            return False
        if os.path.isdir(path):
            return os.access(path, os.W_OK)
        try:
            os.makedirs(path, exist_ok=True)
            return True
        except OSError:
            return False

    def on_open_data_path_button_clicked(self, *args):
        # Use Gio instead of a shell call to xdg-open, for the reason that
        # HelperMethods.open_web gives. Gio does not block the GTK main loop,
        # it routes through the OpenURI portal inside a sandbox, and the entry
        # text never becomes a command.
        path = os.path.expanduser(self.data_path.get_text())
        uri = Gio.File.new_for_path(path).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            log.error(f"Failed to open data path {path}: {e}")


class GeneralPage(Adw.PreferencesPage):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__()
        self.set_title("General")
        self.set_icon_name("open-menu-symbolic")

        self.add(GeneralPageGroup(settings=settings))
        self.add(FontPageGroup(settings=settings))

class GeneralPageGroup(Adw.PreferencesGroup):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title=gl.lm.get("General app settings"))

        self.hold_time_row = Adw.SpinRow.new_with_range(min=0.1, max=3, step=0.1)
        self.hold_time_row.set_title("Minimum hold duration (s)")
        self.hold_time_row.set_subtitle("Minimum hold duration for keys and dials")
        self.hold_time_row.set_range(0.1, 3)
        self.add(self.hold_time_row)

        self.rolling_labels = Adw.SwitchRow(title="Rolling labels", subtitle="Enable automatic rolling/scrolling of too long labels")
        self.add(self.rolling_labels)

        self.load_defaults()

        # Connect signals
        self.hold_time_row.connect("changed", self.on_n_fake_decks_row_changed)
        self.rolling_labels.connect("notify::active", self.on_rolling_labels_changed)

    def load_defaults(self):
        app = self.settings.app
        self.hold_time_row.set_value(app.hold_time)
        self.rolling_labels.set_active(app.rolling_labels)

    def on_n_fake_decks_row_changed(self, *args):
        self.settings.app.hold_time = self.hold_time_row.get_value()

        for controller in gl.deck_manager.deck_controller:
            controller.hold_time = self.hold_time_row.get_value()

        # Save
        self.settings.save_json()

        # Reload decks
        gl.deck_manager.load_fake_decks()

    def on_rolling_labels_changed(self, *args):
        self.settings.app.rolling_labels = self.rolling_labels.get_active()

        # Save
        self.settings.save_json()

        # Reload all pages - TODO: might not be necessary
        for controller in gl.deck_manager.deck_controller:
            controller.reload_page()

class FontPageGroup(Adw.PreferencesGroup):
    # Trailing window for the shared page-reload debounce, in milliseconds.
    # The saturation row in DeckSettings/DeckGroup.py uses the same 300 ms.
    RELOAD_DEBOUNCE_MS = 300

    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title=gl.lm.get("settings-font-settings-header"))

        # All four rows share one debouncer. Font changes arrive in bursts:
        # family and size from one dialog, then colour, then outline, and a
        # colour-picker drag fires many times on its own. Without the shared
        # debouncer each row starts its own reload-all-pages thread, so one
        # visit here runs several page reloads at once.
        #
        # Every font change reaches exactly one reload. reload_all_pages calls
        # create_n_states, which rebuilds every LabelManager, and the label
        # memos rely on that rebuild for pixel correctness. So no equality
        # check against the previous value, and no early return for an
        # unchanged look, may drop the trailing fire.
        self.reload_debouncer = TrailingDebouncer(self.RELOAD_DEBOUNCE_MS, self._reload_all_pages)

        self.font_row = FontRow(self)
        self.add(self.font_row)

        self.font_color_row = FontColorRow(self)
        self.add(self.font_color_row)

        self.font_outline_width_row = FontOutlineWidthRow(self)
        self.add(self.font_outline_width_row.row)

        self.font_outline_color_row = FontOutlineColorRow(self)
        self.add(self.font_outline_color_row)

    def request_page_reload(self) -> None:
        """Every font row asks for its reload through this method.

        See the debouncer note in __init__. The settings write already
        finished when a row calls this, and only the reload waits.
        """
        self.reload_debouncer.trigger()

    def _reload_all_pages(self) -> None:
        page_manager = gl.page_manager
        if page_manager is None:
            return
        threading.Thread(target=page_manager.reload_all_pages, daemon=True, name="reload-all-pages").start()


class FontRow(Adw.ActionRow):
    def __init__(self, font_page_group: FontPageGroup):
        super().__init__(title=gl.lm.get("settings-font-settings-header"),
                         subtitle=gl.lm.get("settings-font-settings-subtitle"))
        
        self.font_page_group = font_page_group
        
        self.font_chooser_button = Gtk.FontButton(valign=Gtk.Align.CENTER)
        self.add_suffix(self.font_chooser_button)

        app = self.font_page_group.settings.app

        desc = get_pango_font_description(app.font_default("font-family"),
                                          app.font_default("font-size"),
                                          app.font_default("font-weight"),
                                          app.font_default("font-style"))
        self.font_chooser_button.set_font_desc(desc)

        self.font_chooser_button.connect("font-set", self.on_set)

    def on_set(self, widget):
        font_desc = widget.get_font_desc()
        family, size, weight, style = get_values_from_pango_font_description(font_desc)

        gl.settings_manager.font_defaults["font-family"] = family
        gl.settings_manager.font_defaults["font-size"] = size
        gl.settings_manager.font_defaults["font-weight"] = weight
        gl.settings_manager.font_defaults["font-style"] = style

        self.font_page_group.settings.app.default_font = gl.settings_manager.font_defaults
        gl.settings_manager.save_font_defaults()
        # No save_json() call here, unlike the toggle rows. save_font_defaults
        # already merged the font into the shared settings, the copy that every
        # write refreshes, and wrote that. A write of the snapshot that this
        # dialog took at construction would restore every general value as it
        # stood when the window opened, and revert what another window, or the
        # app itself, changed on disk since.

        self.font_page_group.request_page_reload()

class FontColorRow(Adw.ActionRow):
    def __init__(self, font_page_group: FontPageGroup):
        super().__init__(title=gl.lm.get("settings-font-color-settings-header"),
                         subtitle=gl.lm.get("settings-font-color-settings-subtitle"))
        
        self.font_page_group = font_page_group
        
        self.font_color_chooser_button = Gtk.ColorButton(valign=Gtk.Align.CENTER)
        self.add_suffix(self.font_color_chooser_button)

        font_color = self.font_page_group.settings.app.font_default("font-color")
        self.font_color_chooser_button.set_rgba(color_values_to_gdk(font_color))

        self.font_color_chooser_button.connect("color-set", self.on_set)

    def on_set(self, widget):
        font_color = widget.get_rgba()

        gl.settings_manager.font_defaults["font-color"] = gdk_color_to_values(font_color)
        self.font_page_group.settings.app.default_font = gl.settings_manager.font_defaults
        gl.settings_manager.save_font_defaults()

        self.font_page_group.request_page_reload()

class FontOutlineColorRow(Adw.ActionRow):
    def __init__(self, font_page_group: FontPageGroup):
        super().__init__(title=gl.lm.get("settings-font-outline-color-settings-header"),
                         subtitle=gl.lm.get("settings-font-outline-color-settings-subtitle"))
        
        self.font_page_group = font_page_group

        self.outline_color_chooser_button = Gtk.ColorButton(valign=Gtk.Align.CENTER)
        self.add_suffix(self.outline_color_chooser_button)

        outline_color = self.font_page_group.settings.app.font_default("outline-color")
        self.outline_color_chooser_button.set_rgba(color_values_to_gdk(outline_color))

        self.outline_color_chooser_button.connect("color-set", self.on_set)

    def on_set(self, widget):
        outline_color = widget.get_rgba()

        gl.settings_manager.font_defaults["outline-color"] = gdk_color_to_values(outline_color)
        self.font_page_group.settings.app.default_font = gl.settings_manager.font_defaults
        gl.settings_manager.save_font_defaults()

        self.font_page_group.request_page_reload()

class FontOutlineWidthRow:
    """
    Can't inherit from Adw.SpinRow
    """
    def __init__(self, font_page_group: FontPageGroup):
        self.font_page_group = font_page_group

        self.row = Adw.SpinRow.new_with_range(min=0, max=10, step=1)
        self.row.set_title(gl.lm.get("settings-font-outline-width-settings-header"))
        self.row.set_subtitle(gl.lm.get("settings-font-outline-width-settings-subtitle"))

        outline_width = self.font_page_group.settings.app.font_default("outline-width")
        self.row.set_value(round(outline_width))

        self.row.connect("changed", self.on_set)

    def on_set(self, widget):
        outline_width = widget.get_value()

        gl.settings_manager.font_defaults["outline-width"] = outline_width
        self.font_page_group.settings.app.default_font = gl.settings_manager.font_defaults
        gl.settings_manager.save_font_defaults()

        self.font_page_group.request_page_reload()


class StorePage(Adw.PreferencesPage):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__()
        self.set_title(gl.lm.get("settings-store-settings-title"))
        self.set_icon_name("go-home-symbolic")

        self.add(StorePageGroup(settings=settings))

class StorePageGroup(Adw.PreferencesGroup):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title=gl.lm.get("settings-store-settings-header"))

        self.auto_update = Adw.SwitchRow(title=gl.lm.get("settings-store-settings-auto-update"), active=True)
        self.add(self.auto_update)

        self.custom_stores = CustomContentGroup(title=gl.lm.get("settings-store-custom-stores-header"),
                                                description=gl.lm.get("settings-store-custom-stores-subtitle"),
                                                custom_type="stores", margin_top=12)
        self.add(self.custom_stores)

        self.custom_plugins = CustomContentGroup(title=gl.lm.get("settings-store-custom-plugins-header"),
                                                 description=gl.lm.get("settings-store-custom-plugins-subtitle"),
                                                 custom_type="plugins", margin_top=12)
        self.add(self.custom_plugins)

        self.load_defaults()

        # Connect signals
        self.auto_update.connect("notify::active", self.on_auto_update_toggled)

    def load_defaults(self):
        self.auto_update.set_active(self.settings.app.auto_update)

    def on_auto_update_toggled(self, *args):
        self.settings.app.auto_update = self.auto_update.get_active()

        # Save
        self.settings.save_json()

class CustomContentGroup(BetterPreferencesGroup):
    def __init__(self, title: str, description: str,custom_type: str, **kwargs):
        super().__init__(title=title, description=description, **kwargs)

        self.custom_type = custom_type
        self.enable_key = f"enable-custom-{self.custom_type}"
        self.store_key = f"custom-{self.custom_type}"

        self.suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_header_suffix(self.suffix_box)
        
        self.enable_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.enable_switch.connect("state-set", self.on_toggle_enable)
        self.suffix_box.append(self.enable_switch)

        self.add_button = Gtk.Button(icon_name="list-add-symbolic", css_classes=["flat"])
        self.add_button.connect("clicked", self.on_add_button_clicked)
        self.suffix_box.append(self.add_button)

        self.load_config_values()

    def on_toggle_enable(self, switch: Gtk.Switch, *args):
        settings = gl.settings_manager.app()
        settings.set("store", self.enable_key, switch.get_active())

        settings.save()

    def add_row(self, i: int, url: str, branch: str):
        self.add(CustomContentEntry(content_group=self, i=i, url=url, branch=branch))

    def load_config_values(self):
        settings = gl.settings_manager.get_app_settings()

        self.enable_switch.set_active(AppSettings(settings).get("store", self.enable_key))

        for i, entry in enumerate(settings.get("store", {}).get(self.store_key, [])):
            self.add_row(i, entry.get("url", ""), entry.get("branch", ""))

    def on_add_button_clicked(self, *args):
        settings = gl.settings_manager.get_app_settings()

        settings.setdefault("store", {})
        settings["store"].setdefault(self.store_key, [])
        settings["store"][self.store_key].append({"url": None, "branch": None})

        self.add_row(len(settings["store"][self.store_key]) - 1, None, None)

        gl.settings_manager.save_app_settings(settings)

    def update_indicies(self):
        for i, row in enumerate(self.get_rows()):
            row.i = i

class CustomContentEntry(Adw.PreferencesRow):
    def __init__(self, content_group: CustomContentGroup, i: int, url: str, branch: str):
        super().__init__(activatable=False)

        self.content_group = content_group
        self.i = i

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5, margin_start=5, margin_end=5)
        self.set_child(self.main_box)

        self.entry_grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        self.main_box.append(self.entry_grid)

        self.url = Adw.EntryRow(title="Repository URL", valign=Gtk.Align.CENTER, text=url or "")
        self.url.connect("changed", self.on_value_changed)
        self.entry_grid.append(self.url)

        self.branch = Adw.EntryRow(title="Branch", valign=Gtk.Align.CENTER, text=branch or "")
        self.branch.connect("changed", self.on_value_changed)
        self.entry_grid.append(self.branch)

        self.button_remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["destructive-action-on-hover", "flat"])
        self.main_box.append(self.button_remove)

        self.button_remove.connect("clicked", self.on_remove)

        # Flag an already-stored url the store cannot use, so the reason a
        # custom entry never shows up is visible in the row itself.
        self.refresh_url_validity()

    def refresh_url_validity(self) -> str | None:
        """Returns the url to persist, or None when the field holds
        something the store could not use.

        parse_repo_url validates the url, and the store runs the same parse
        later, so the catalog load never skips a url that this method accepts.
        Main-thread only, because it restyles the row.
        """
        url = self.url.get_text().strip()
        if url and parse_repo_url(url) is None:
            self.url.add_css_class("error")
            self.url.set_tooltip_text(gl.lm.get("settings-store-custom-url-invalid"))
            return None
        self.url.remove_css_class("error")
        self.url.set_tooltip_text(None)
        return url

    def on_value_changed(self, *args):
        url = self.refresh_url_validity()
        if url is None:
            # Keep the stored entry. A url that the store skips gains
            # nothing, and the row stays flagged until it parses. An empty
            # field does persist, as an empty string, so a clear takes effect.
            return

        settings = gl.settings_manager.get_app_settings()

        settings.setdefault("store", {})
        settings["store"].setdefault(self.content_group.store_key, [])
        settings["store"][self.content_group.store_key][self.i]["url"] = url
        settings["store"][self.content_group.store_key][self.i]["branch"] = self.branch.get_text().strip()

        gl.settings_manager.save_app_settings(settings)

    def on_remove(self, *args):
        self.content_group.remove(self)

        settings = gl.settings_manager.get_app_settings()
        stores = settings.get("store", {}).get(self.content_group.store_key, [])
        if self.i < len(stores):
            stores.pop(self.i)

        settings.setdefault("store", {})
        settings["store"][self.content_group.store_key] = stores

        gl.settings_manager.save_app_settings(settings)

        self.content_group.update_indicies()


class PerformancePage(Adw.PreferencesPage):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__()
        self.set_title(gl.lm.get("settings.performance.title"))
        self.set_icon_name("power-profile-performance-symbolic")

        self.add(PerformancePageGroup(settings=settings))

class PerformancePageGroup(Adw.PreferencesGroup):
    # Row order == stored value; the ComboRow only knows indices.
    PAUSE_MODES = (MODE_SCREENSAVER, MODE_SYSTEM_IDLE)

    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title=gl.lm.get("settings.performance.header"))

        self.n_cached_pages = Adw.SpinRow.new_with_range(min=0, max=50, step=1)
        self.n_cached_pages.set_title(gl.lm.get("settings.performance.n-cached-pages.title"))
        self.n_cached_pages.set_subtitle(gl.lm.get("settings.performance.n-cached-pages.subtitle"))
        self.n_cached_pages.set_tooltip_text(gl.lm.get("settings.performance.n-cached-pages.tooltip"))
        self.add(self.n_cached_pages)

        self.cache_videos = Adw.SwitchRow(title=gl.lm.get("settings.performance.cache-videos.title"), active=True,
                                          subtitle=gl.lm.get("settings.performance.cache-videos.subtitle"),
                                          tooltip_text=gl.lm.get("settings.performance.cache-videos.tooltip"))
        self.add(self.cache_videos)

        # Quiescence gating. The default pauses only while the deck
        # screensaver is up, which matches the behaviour without these rows,
        # so an untouched setting changes nothing.
        self.animation_pause_mode = Adw.ComboRow(
            title=gl.lm.get("settings.performance.animation-pause.title"),
            subtitle=gl.lm.get("settings.performance.animation-pause.subtitle"),
        )
        self.animation_pause_mode.set_model(Gtk.StringList.new([
            gl.lm.get("settings.performance.animation-pause.screensaver"),
            gl.lm.get("settings.performance.animation-pause.system-idle"),
        ]))
        self.add(self.animation_pause_mode)

        self.animation_idle_minutes = Adw.SpinRow.new_with_range(min=1, max=120, step=1)
        self.animation_idle_minutes.set_title(gl.lm.get("settings.performance.animation-idle-minutes.title"))
        self.animation_idle_minutes.set_subtitle(gl.lm.get("settings.performance.animation-idle-minutes.subtitle"))
        self.add(self.animation_idle_minutes)

        self.load_defaults()

        # Connect signals
        self.n_cached_pages.connect("changed", self.on_n_cached_pages_changed)
        self.cache_videos.connect("notify::active", self.on_cache_videos_toggled)
        self.animation_pause_mode.connect("notify::selected", self.on_animation_pause_mode_changed)
        self.animation_idle_minutes.connect("changed", self.on_animation_idle_minutes_changed)

    def get_selected_pause_mode(self) -> str:
        index = self.animation_pause_mode.get_selected()
        if index >= len(self.PAUSE_MODES):
            # Gtk.INVALID_LIST_POSITION, so nothing is selected.
            return self.PAUSE_MODES[0]
        return self.PAUSE_MODES[index]

    def load_defaults(self):
        app = self.settings.app
        self.n_cached_pages.set_value(app.n_cached_pages)
        self.cache_videos.set_active(app.cache_videos)
        mode = app.animation_pause_mode
        self.animation_pause_mode.set_selected(
            self.PAUSE_MODES.index(mode) if mode in self.PAUSE_MODES else 0
        )
        self.animation_idle_minutes.set_value(app.animation_idle_minutes)
        self.sync_idle_row_sensitivity()

    def sync_idle_row_sensitivity(self):
        # The delay only means anything in the mode that watches system idle.
        self.animation_idle_minutes.set_sensitive(
            self.get_selected_pause_mode() == MODE_SYSTEM_IDLE
        )

    def on_n_cached_pages_changed(self, *args):
        self.settings.app.n_cached_pages = int(self.n_cached_pages.get_value())

        # Save
        self.settings.save_json()

        # Update value in page manager
        gl.page_manager.set_pages_to_cache(int(self.n_cached_pages.get_value()))

    def on_cache_videos_toggled(self, *args):
        self.settings.app.cache_videos = self.cache_videos.get_active()

        # Save
        self.settings.save_json()

    def on_animation_pause_mode_changed(self, *args):
        self.settings.app.animation_pause_mode = self.get_selected_pause_mode()

        # Save
        self.settings.save_json()

        self.sync_idle_row_sensitivity()
        self.push_to_presence_monitor()

    def on_animation_idle_minutes_changed(self, *args):
        self.settings.app.animation_idle_minutes = int(self.animation_idle_minutes.get_value())

        # Save
        self.settings.save_json()

        self.push_to_presence_monitor()

    def push_to_presence_monitor(self):
        # A runtime push, in the same pattern as the fan-out of the
        # FPS-warning row to the media players. The monitor re-evaluates at
        # once instead of waiting for the next lock or idle event.
        if gl.presence_monitor is not None:
            gl.presence_monitor.set_mode(
                self.settings.app.animation_pause_mode,
                self.settings.app.animation_idle_minutes,
            )


class SystemPage(Adw.PreferencesPage):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__()
        self.set_title(gl.lm.get("settings-system-settings-title"))
        self.set_icon_name("system-run-symbolic")

        self.add(SystemGroup(settings=settings))

class SystemGroup(Adw.PreferencesGroup):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(title=gl.lm.get("settings-system-settings-header"))

        self.keep_running = Adw.SwitchRow(title=gl.lm.get("settings-system-settings-keep-running"), subtitle=gl.lm.get("settings-system-settings-keep-running-subtitle"), active=False)
        self.add(self.keep_running)

        self.autostart = Adw.SwitchRow(title=gl.lm.get("settings-system-settings-autostart"), subtitle=gl.lm.get("settings-system-settings-autostart-subtitle"), active=True)
        self.add(self.autostart)

        self.lock_on_lock_screen = Adw.SwitchRow(title="Lock decks when screen is locked", subtitle="Works on GNOME, KDE, Cinnamon and Hyprland; other environments use systemd-logind", active=True)
        self.add(self.lock_on_lock_screen)

        self.load_defaults()

        # Connect signals
        self.keep_running.connect("notify::active", self.on_keep_running_toggled)
        self.autostart.connect("notify::active", self.on_autostart_toggled)
        self.lock_on_lock_screen.connect("notify::active", self.on_lock_on_lock_screen_toggled)

    def load_defaults(self):
        app = self.settings.app
        # keep-running is tri-state (None == never asked); the switch only
        # reflects an explicit True.
        self.keep_running.set_active(app.keep_running is True)
        self.autostart.set_active(app.autostart)
        self.lock_on_lock_screen.set_active(app.lock_on_lock_screen)

    def on_keep_running_toggled(self, *args):
        self.settings.app.keep_running = self.keep_running.get_active()

        # Save
        self.settings.save_json()

    def on_autostart_toggled(self, *args):
        self.settings.app.autostart = self.autostart.get_active()

        setup_autostart(self.autostart.get_active())

        # Save
        self.settings.save_json()

    def on_lock_on_lock_screen_toggled(self, *args):
        self.settings.app.lock_on_lock_screen = self.lock_on_lock_screen.get_active()

        # Save
        self.settings.save_json()