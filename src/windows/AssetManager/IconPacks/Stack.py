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

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

# Import own modules
from src.windows.AssetManager.IconPacks.PackChooser import IconPackChooser
from src.windows.AssetManager.IconPacks.Icons.IconChooser import IconChooserPage

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.AssetManager import AssetManager

class IconPackChooserStack(Gtk.Stack):
    def __init__(self, asset_manager: "AssetManager", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asset_manager = asset_manager

        self.on_loads_finished_tasks: list[callable] = []
        # Serializes the two build_finished flags with the deferred-task
        # queue -- see on_load_finished / show_for_path. The pack chooser and
        # the icon chooser each build on their OWN worker thread and both call
        # on_load_finished, so unlike the custom-asset chooser this drain can
        # be entered concurrently from two threads.
        self._loads_lock = threading.Lock()

        self.build()

    def build(self):
        self.pack_chooser = IconPackChooser(self, self.asset_manager)
        self.add_titled(self.pack_chooser, "pack-chooser", "Chooser")

        self.icon_chooser = IconChooserPage(self, self.asset_manager)
        self.add_titled(self.icon_chooser, "icon-chooser", "Icon Chooser")


    def show_for_path(self, path):
        with self._loads_lock:
            if not self.get_is_build_finished():
                # Deferred under the same lock on_load_finished drains with:
                # the task either makes a snapshot or sees the flag True
                # here and dispatches directly -- never stranded in the gap
                # between the last flag flip and the drain that follows it.
                self.on_loads_finished_tasks.append(lambda: self.show_for_path(path))
                return
        packs = gl.icon_pack_manager.get_icon_packs()
        for pack in packs.values():
            icons = pack.get_icons()
            for icon in icons:
                if icon.path == path:
                    self.icon_chooser.load_for_pack(pack)
                    self.icon_chooser.select_icon(path=path)
                    self.set_visible_child(self.icon_chooser)
                    self.asset_manager.asset_chooser.set_visible_child_name("icon-packs")
                    self.asset_manager.back_button.set_visible(True)
                    return
                
    def get_is_build_finished(self):
        return hasattr(self, "pack_chooser") and self.pack_chooser.build_finished and hasattr(self, "icon_chooser") and self.icon_chooser.build_finished
                
    def on_load_finished(self):
        """Called from BOTH build worker threads (pack + icon). Snapshots and
        clears the deferred-task queue as one atomic step under the lock, so a
        show_for_path that read a flag as False can no longer slip its task in
        after this drain snapshotted the queue, and two concurrent callers
        can't double-run or double-remove the same task. Tasks run OUTSIDE the
        lock: they recurse into show_for_path, which takes the same lock."""
        with self._loads_lock:
            if not self.get_is_build_finished():
                return
            tasks = list(self.on_loads_finished_tasks)
            self.on_loads_finished_tasks.clear()
        for task in tasks:
            task()