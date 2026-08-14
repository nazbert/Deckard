"""
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
import gi

from src.windows.Store.SDPlusBarWallpapers.SDPlusBarWallpaperPage import SDPlusBarWallpaperPage


gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

# Import Python modules
import threading
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.mainWindow.mainWindow import MainWindow

# Import globals
import globals as gl

# Import own modules
from src.windows.Store.Plugins.PluginPage import PluginPage
from src.windows.Store.Icons.IconPage import IconPage
from src.windows.Store.StorePage import StorePage
from src.windows.Store.Wallpapers.WallpaperPage import WallpaperPage

class Store(Gtk.ApplicationWindow):
    def __init__(self, main_window: "MainWindow", *args, **kwargs):
        super().__init__(  # type: ignore[misc]  # gi stub: the stub models GObject properties as positional-or-keyword params, so *args reads as a second binding for them; at runtime GObject.__init__ takes properties by keyword only
            title="Store",
            default_width=1050,
            default_height=750,
            modal=True,
            transient_for=main_window,
            *args, **kwargs
            )
        self.main_window = main_window

        self.backend = gl.store_backend

        self.currently_downloading: bool = False # Used to prevent multiple downloads because this may lead to errors during plugin initialization
        # Serializes install/uninstall/update across previews. The bool above
        # is informational; the lock is what prevents concurrent downloads
        # (the old check-then-set poll on the bool was racy).
        self.download_lock = threading.Lock()

        self.build()

        self.connect("close-request", self.on_close)

    def on_close(self, *args, **kwargs):
        gl.store = None

    def build(self):
        # Header bar
        self.header = Gtk.HeaderBar(css_classes=["flat"])
        self.set_titlebar(self.header)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(self.main_box)

        self.main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT, hexpand=True, vexpand=True)
        self.main_stack.connect("notify::visible-child-name", self.on_switch)
        self.main_box.append(self.main_stack)

        # Header stack switcher
        self.stack_switcher = Gtk.StackSwitcher(stack=self.main_stack)
        self.header.set_title_widget(self.stack_switcher)

        # Header back button
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic", visible=False)
        self.back_button.connect("clicked", self.on_back_button_click)
        self.header.pack_start(self.back_button)

        # Every page builds its cheap widget skeleton here, so the
        # StackSwitcher shows all tabs at once and no outside code meets a
        # missing page attribute. Only the first tab, Plugins, starts its
        # network fetch and content population at once, which StorePage.load()
        # performs and which costs the time. The other three wait for
        # on_switch(), when each first becomes the visible child.
        self.plugin_page = PluginPage(store=self)
        self.icon_page = IconPage(store=self)
        self.wallpaper_page = WallpaperPage(store=self)
        self.sd_plus_bar_wallpaper_page = SDPlusBarWallpaperPage(store=self)

        self.main_stack.add_titled(self.plugin_page, "Plugins", gl.lm.get("store.plugins.section"))
        self.main_stack.add_titled(self.icon_page, "Icons", gl.lm.get("store.icons.section"))
        self.main_stack.add_titled(self.wallpaper_page, "Wallpapers", gl.lm.get("store.wallpapers.section"))
        self.main_stack.add_titled(self.sd_plus_bar_wallpaper_page, "sdPlusBarWallpapers", gl.lm.get("store.sdPlusBarWallpapers.section"))

        # Load the first tab here, so the store shows content as soon as it
        # opens. Do not rely on the notify that add_titled sends when the
        # first child becomes visible. The ensure_loaded() call in on_switch
        # can also arrive first, and both calls are idempotent.
        self.plugin_page.ensure_loaded()

    def on_back_button_click(self, button: Gtk.Button):
        # Switch active page back from info page
        self.main_stack.get_visible_child().set_info_visible(False)

    def on_switch(self, *args):
        child: StorePage = self.main_stack.get_visible_child()
        # StorePage._loaded guards the load, so this does nothing for a tab
        # that already loaded, which includes the first tab.
        child.ensure_loaded()

        if child.get_visible_child_name() == "Info":
            self.back_button.set_visible(True)
        else:
            self.back_button.set_visible(False)