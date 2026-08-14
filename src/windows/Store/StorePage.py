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

from src.windows.Store.StorePageSection import StorePageSection

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

# Import python modules
import threading
from loguru import logger as log

# Import own modules
from src.windows.Store.InfoPage import InfoPage
from src.windows.Store.NoConnectionError import NoConnectionError

# Typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.Store.Store import Store

# Import globals
import globals as gl

class StorePage(Gtk.Stack):
    def __init__(self, store: "Store"):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_margin_start(15)
        self.set_margin_end(15)
        self.set_margin_top(15)
        self.set_margin_bottom(15)
        self.set_transition_duration(200)
        self.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)

        self.store = store

        # A subclass starts no load() thread from __init__. Store starts one
        # at once for the first tab, and for the other tabs on the first
        # notify::visible-child-name. The guard makes a switch back to a
        # loaded tab do nothing instead of fetching from the store backend
        # again.
        self._loaded = False

        self.build()

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        threading.Thread(target=self._load_guarded, name=f"load_{type(self).__name__}").start()

    def load(self) -> None:
        """Subclass hook. Fetch the catalog of this tab and append the previews.

        It runs on the loader thread that ensure_loaded starts.
        """
        raise NotImplementedError

    def _load_guarded(self) -> None:
        """Run the subclass load() and keep the tab retryable.

        Without this wrapper, an exception dies in the log.catch of load(),
        the spinner keeps running, and _loaded stays True, so the tab retries
        only after a rebuild of the store window. A failed load here shows the
        error page and clears _loaded, so the next visit tries again.
        """
        try:
            self.load()
        except Exception:
            log.exception(f"{type(self).__name__}.load() failed")
            self.show_connection_error()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.add_titled(self.main_box, "Store", "Store")

        self.section_stack = Gtk.Stack(vexpand=True)
        self.main_box.append(self.section_stack)

        self.compatible_section = StorePageSection()
        self.incompatible_section = StorePageSection()
        self.incompatible_section.nothing_here.set_icon_name("face-smile-symbolic")


        self.section_switcher = Gtk.StackSwitcher(stack=self.section_stack, margin_bottom=15)
        self.main_box.prepend(self.section_switcher)
        
        self.section_stack.add_titled(self.compatible_section, "Compatible", "Compatible")
        self.section_stack.add_titled(self.incompatible_section, "Incompatible", "Incompatible")
        self.section_stack.set_visible_child(self.compatible_section)

        self.loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True,
                                   visible=False, valign=Gtk.Align.CENTER)
        self.main_box.append(self.loading_box)

        self.spinner = Gtk.Spinner(spinning=False)
        self.loading_box.append(self.spinner)

        self.loading_text = Gtk.Label(label=gl.lm.get("store.page.loading-spinner.label"))
        self.loading_box.append(self.loading_text)

        # Info page
        self.info_page = InfoPage(self)
        self.add_titled(self.info_page, "Info", "Info")

        # Error page
        self.no_connection_page = NoConnectionError()
        self.add_titled(self.no_connection_page, "Error", "Error")

    def append_preview_on_main(self, section, factory):
        """Construct a preview widget on the GTK main loop, then append it.

        The page loaders run on worker threads. A call of the form
        GLib.idle_add(section.append_child, XPreview(...)) marshals the append
        only, and it builds the widget tree as the argument, on the loader
        thread. That is the off-main GTK class that kills the process.
        """
        def _build():
            section.append_child(factory())
            return False
        GLib.idle_add(_build)

    def set_loading(self):
        GLib.idle_add(self.section_stack.set_visible, False)
        # GLib.idle_add(self.bottom_box.set_visible, False)
        GLib.idle_add(self.loading_box.set_visible, True)
        # threading.Thread(target=self.spinner.set_spinning, args=(True,), name="spinner_thread").start()
        GLib.idle_add(self.spinner.set_spinning, True)
        GLib.idle_add(self.section_switcher.set_visible, False)

    def set_loaded(self):
        GLib.idle_add(self.section_stack.set_visible, True)
        # GLib.idle_add(self.bottom_box.set_visible, True)
        GLib.idle_add(self.loading_box.set_visible, False)
        GLib.idle_add(self.spinner.set_spinning, False)
        GLib.idle_add(self.section_switcher.set_visible, True)
        GLib.idle_add(self.hide_stack_switcher_if_all_compatible)

    def hide_stack_switcher_if_all_compatible(self):
        if not self.incompatible_section.are_items_present():
            self.section_switcher.set_visible(False)

    def set_info_visible(self, visible:bool):
        if visible:
            self.set_visible_child(self.info_page)
            self.store.back_button.set_visible(True)
        else:
            self.set_visible_child(self.main_box)
            self.store.back_button.set_visible(False)

    def show_connection_error(self):
        # A failed load stays retryable. The next ensure_loaded(), which a
        # visit to the tab triggers, starts a fresh load instead of reading
        # the error page as loaded.
        self._loaded = False
        # Called from the load() worker thread: marshal the widget change.
        GLib.idle_add(self.set_visible_child, self.no_connection_page)

    def hide_connection_error(self):
        GLib.idle_add(self.set_visible_child, self.main_box)