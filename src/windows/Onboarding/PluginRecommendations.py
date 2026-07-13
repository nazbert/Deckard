import threading
from gi.repository import Gtk, Adw, GLib
from loguru import logger as log

from GtkHelper.GtkHelper import BetterPreferencesGroup, LoadingScreen

import globals as gl
from src.backend.Store.StoreBackend import NoConnectionError
from src.windows.Store.StoreData import PluginData

class PluginRecommendations(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.defaults = [
            "com_core447_DeckPlugin",
            "com_core447_OSPlugin",
            "com_core447_OBSPlugin",
            "com_core447_MediaPlugin",
            "com_core447_VolumeMixer"
        ]

        self.title = Gtk.Label(label="Plugins", css_classes=["title-1"], margin_top=20)
        self.append(self.title)

        self.main_stack = Gtk.Stack(hexpand=True, vexpand=True)
        self.append(self.main_stack)

        self.loading_box = LoadingScreen()
        self.main_stack.add_named(self.loading_box, "loading")

        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True, margin_top=10)
        self.main_stack.add_named(self.scrolled_window, "scrolled")

        self.scrolled_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.scrolled_window.set_child(self.scrolled_box)

        self.clamp = Adw.Clamp(margin_start=40, margin_end=40)
        self.scrolled_box.append(self.clamp)

        self.scrolled_box.append(Gtk.Label(label="You can always install more plugins from the store", css_classes=["dim-label"], margin_top=5, margin_bottom=5))

        self.group = BetterPreferencesGroup()
        self.group.set_sort_func(self.sort_func)
        self.clamp.set_child(self.group)

        # Error state for a failed store fetch (issue #118): without it the
        # fetch failure killed the loader thread and left the spinner up
        # forever -- the user paged past, installed nothing, and landed in
        # the main window with an empty Add-Action list.
        self.error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True,
                                 valign=Gtk.Align.CENTER)
        self.main_stack.add_named(self.error_box, "error")
        self.error_label = Gtk.Label(
            label="Could not reach the plugin store -- check your internet connection.\n"
                  "You can skip this step and install plugins later from the Store.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
            css_classes=["dim-label"],
        )
        self.error_box.append(self.error_label)
        self.retry_button = Gtk.Button(label="Retry", css_classes=["pill", "suggested-action"],
                                       halign=Gtk.Align.CENTER, margin_top=20)
        self.retry_button.connect("clicked", self.on_retry_clicked)
        self.error_box.append(self.retry_button)

        threading.Thread(target=self.load).start()

    def set_loading(self, loading: bool):
        # Marshalled wholesale: load() calls this from a plain thread, and
        # set_spinning is as much a GTK call as set_visible_child.
        GLib.idle_add(self.loading_box.set_spinning, loading)
        GLib.idle_add(self.main_stack.set_visible_child,
                      self.loading_box if loading else self.scrolled_window)

    def show_connection_error(self):
        GLib.idle_add(self.loading_box.set_spinning, False)
        GLib.idle_add(self.main_stack.set_visible_child, self.error_box)
        # Re-arm the retry button (disabled on click so a double-click can't
        # run two loaders and duplicate the rows on success).
        GLib.idle_add(self.retry_button.set_sensitive, True)

    def on_retry_clicked(self, button):
        self.retry_button.set_sensitive(False)
        threading.Thread(target=self.load).start()

    def load(self):
        self.set_loading(True)

        # Only the data fetch belongs on this thread. Building PluginRows
        # (Adw.ActionRow + CheckButton) and group.add() ran here too -- the
        # process-fatal off-main-GTK construction class (issue #10), racing
        # the carousel on every first launch.
        #
        # The fetch returns a NoConnectionError SENTINEL when every store is
        # unreachable (offline, GitHub rate limit); iterating it raised
        # TypeError, killing this thread with the spinner still up (issue
        # #118's fresh-install mode). Exceptions get the same error state.
        try:
            plugins = gl.store_backend.get_all_plugins()
        except Exception as e:
            log.opt(exception=e).error("Onboarding: plugin recommendations fetch failed")
            plugins = None

        if plugins is None or isinstance(plugins, NoConnectionError):
            self.show_connection_error()
            return

        def build_rows():
            for plugin in plugins:
                if not plugin:
                    continue
                if not plugin.is_compatible:
                    continue

                row = PluginRow(plugin=plugin)
                if plugin.plugin_id in self.defaults:
                    row.check.set_active(True)

                self.group.add(row)
            self.set_loading(False)
            return False

        GLib.idle_add(build_rows)

    def get_selected_plugins(self) -> list[str]:
        return [row.plugin for row in self.group.get_rows() if row.check.get_active()]
    
    def sort_func(self, row1, row2):
        title1 = row1.plugin.plugin_name or ""
        title2 = row2.plugin.plugin_name or ""

        if title1 < title2:
            return -1
        if title1 > title2:
            return 1
        return 0

class PluginRow(Adw.ActionRow):
    def __init__(self, plugin: PluginData, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plugin = plugin

        self.set_title(self.plugin.plugin_name or "")
        self.set_subtitle(self.plugin.short_description or "")
        self.check = Gtk.CheckButton()
        self.add_prefix(self.check)

        self.set_activatable(True)

        self.connect("activated", self.on_activated)
        self.check.connect("toggled", self.on_toggled)

    def on_activated(self, row):
        self.check.set_active(not self.check.get_active())

    def on_toggled(self, button):
        pass