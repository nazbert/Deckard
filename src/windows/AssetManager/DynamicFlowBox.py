"""
Year: 2024

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

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

# Import python modules
import functools

from collections.abc import Callable
from typing import Any

from loguru import logger as log

# The three hooks that a chooser installs on a flow box. All three are
# optional. A box without them shows its items unfiltered and unsorted, and
# show_range refuses to run without a factory.
FilterFunc = Callable[[Any], bool]
SortFunc = Callable[[Any, Any], int]
FactoryFunc = Callable[[Gtk.Widget, Any], None]

class DynamicFlowBox(Gtk.Box):
    def __init__(self, base_class: type, *args, **kwargs):
        """
        base_class: The class of the items in the flow box. Its constructor is not allowed to require any arguments because empty
                    placeholder objects will be created in the flowbox.
                    You have to use the factory to configure the items.
        """
        super().__init__(*args, **kwargs)
        self.set_orientation(Gtk.Orientation.VERTICAL)

        self.N_ITEMS_PER_PAGE = 50

        self.base_class = base_class
        self.items: list = []

        self.sort_func: SortFunc | None = None
        self.filter_func: FilterFunc | None = None
        self.factory_func: FactoryFunc | None = None

        self.build()

        self.generate_placeholders()

    def build(self):
        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(self.scrolled_window)

        self.scrolled_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.scrolled_window.set_child(self.scrolled_box)

        self.flow_box = Gtk.FlowBox(hexpand=True, orientation=Gtk.Orientation.HORIZONTAL,
                                    selection_mode=Gtk.SelectionMode.SINGLE)
        self.scrolled_box.append(self.flow_box)

        # Fix stretch
        self.scrolled_box.append(Gtk.Box(vexpand=True))

        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                               margin_top=15, margin_bottom=15, margin_start=15, margin_end=15)
        self.append(self.nav_box)

        self.back_button = Gtk.Button(icon_name="go-previous-symbolic")
        self.back_button.connect("clicked", self.on_back)
        self.nav_box.append(self.back_button)

        self.nav_box.append(Gtk.Box(hexpand=True))

        self.next_button = Gtk.Button(icon_name="go-next-symbolic")
        self.next_button.connect("clicked", self.on_next)
        self.nav_box.append(self.next_button)

    def generate_placeholders(self):
        for i in range(self.N_ITEMS_PER_PAGE):
            placeholder = self.base_class()
            self.flow_box.append(placeholder)


    def filter_items(self, items: list):
        if not callable(self.filter_func):
            return items
        
        filtered_items = []
        for item in items:
            if self.filter_func(item):
                filtered_items.append(item)
        return filtered_items

    def sort_items(self, items: list):
        if not callable(self.sort_func):
            return items
        
        return sorted(items, key=functools.cmp_to_key(self.sort_func))


    def get_items_to_show(self) -> list:
        filtered_items = self.filter_items(self.items)
        sorted_items = self.sort_items(filtered_items)
        return sorted_items
    

    def show_range(self, start: int, end: int) -> None:
        if not callable(self.factory_func):
            raise ValueError("factory_func must be callable")

        self.current_start_index = start

        # The whole rebind runs as one main-loop callback. A show of the
        # recycled children here, with a separate idle per child for the bind,
        # leaves a gap in which a click activates the asset of the earlier
        # page, or the None asset of a fresh placeholder, which raises
        # TypeError in on_child_activated. A child that the old page selected
        # also keeps its GTK selection while it shows a different asset. The
        # same main loop dispatches the input events, so one callback leaves
        # no moment where a half-rebound pool is clickable. The filter and
        # sort functions read GTK state, such as the search entry, so
        # get_items_to_show runs on the main thread as well, at apply time.
        GLib.idle_add(self._apply_range, start, end)

    def _apply_range(self, start: int, end: int) -> bool:
        factory_func = self.factory_func
        if factory_func is None:
            # show_range refuses to schedule without one, so a None here
            # means a clear between the schedule and this idle dispatch.
            return False
        items = self.get_items_to_show()
        page_items = items[start:end]

        # Clear the selection of the earlier page or filter before the rebind
        # of the pool. The factory selects the matching child again.
        self.flow_box.unselect_all()

        for i in range(self.N_ITEMS_PER_PAGE):
            preview = self.flow_box.get_child_at_index(i)
            if preview is None:
                break
            if i < len(page_items):
                # Bind before the show, so a child becomes clickable only
                # with its new asset. The guard keeps one bad item from
                # aborting the rest of the rebind. A child whose bind failed
                # stays hidden, or a click reaches the asset of the earlier
                # page.
                try:
                    factory_func(preview, page_items[i])
                except Exception as e:
                    log.opt(exception=True).error(f"Asset factory failed for item {i}: {e}")
                    preview.set_visible(False)
                    continue
                preview.set_visible(True)
            else:
                # Hide left over placeholders
                preview.set_visible(False)

        self.back_button.set_sensitive(start > 0)
        self.next_button.set_sensitive(end < len(items))
        return False  # one-shot idle


    def on_next(self, *args):
        self.current_start_index += self.N_ITEMS_PER_PAGE
        self.show_range(self.current_start_index, self.current_start_index + self.N_ITEMS_PER_PAGE)

    def on_back(self, *args):
        self.current_start_index -= self.N_ITEMS_PER_PAGE
        self.show_range(self.current_start_index, self.current_start_index + self.N_ITEMS_PER_PAGE)


    def set_item_list(self, items: list) -> None:
        self.items = items

    def set_factory(self, factory_func: FactoryFunc) -> None:
        self.factory_func = factory_func

    def set_sort_func(self, sort_func: SortFunc) -> None:
        self.sort_func = sort_func

    def set_filter_func(self, filter_func: FilterFunc) -> None:
        self.filter_func = filter_func