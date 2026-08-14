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
from gi.repository import Gtk, Gdk, GLib

# Import python modules
from loguru import logger as log

# Import own modules
from GtkHelper.GtkHelper import run_on_main
from src.backend import settings_store
from src.windows.AssetManager.ChooserPage import ChooserPage
from src.windows.AssetManager.CustomAssets.FlowBox import CustomAssetChooserFlowBox

# Import globals
import globals as gl

# Import typing modules
from collections.abc import Callable
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.AssetManager import AssetManager

class CustomAssetChooser(ChooserPage):
    def __init__(self, asset_manager: "AssetManager"):
        super().__init__()
        self.asset_manager = asset_manager

        self.asset_chooser: CustomAssetChooserFlowBox | None = None
        self.build_finished = False
        self.build_task_finished_tasks: list[Callable[[], Any]] = []
        # Serializes build_finished with the deferred-task queue. See
        # _finish_build and show_for_path.
        self._build_tasks_lock = threading.Lock()

        threading.Thread(target=self.build).start()

    @log.catch
    def build(self):
        self.build_finished = False
        try:
            # The whole GTK construction runs on the main loop. A build of
            # the flow box, which holds one page of AssetPreviews, and of the
            # button on this worker thread is the off-main GTK crash class.
            # One pause when the tab opens costs less than a segfault. Only
            # the build bookkeeping stays on the thread.
            def _build_ui():
                self.asset_chooser = CustomAssetChooserFlowBox(self)
                # Append to main_box, as the wallpaper, SD+ bar and icon
                # choosers do, and not into the outer ScrolledWindow of
                # ChooserPage. A ScrolledWindow sizes its child to the natural
                # height and never stretches it, which collapses a nested
                # grid, and the flow box brings its own ScrolledWindow and
                # pagination. As a direct main_box child, its own scroller
                # fills the available height. The default scrolled_window also
                # has to go, as it does in the sibling choosers, because an
                # empty vexpand=True child competes for the page height and
                # squeezes the grid.
                self.main_box.remove(self.scrolled_window)
                self.main_box.append(self.asset_chooser)

                self.browse_files_button = Gtk.Button(label=gl.lm.get("asset-chooser.custom.browse-files"), margin_top=15)
                self.browse_files_button.connect("clicked", self.on_browse_files_clicked)
                self.main_box.append(self.browse_files_button)

            run_on_main(_build_ui)

            self.load_defaults()
        finally:
            # Always dismiss the spinner. Without this finally, log.catch
            # swallows an exception from the code above, set_loading(False)
            # never runs, and the Custom Assets page loads forever. log.catch
            # stays, because this finally runs first, and the exception then
            # reaches it and gets logged.
            self.set_loading(False)

            self._finish_build()

    def _finish_build(self) -> None:
        """Set build_finished and snapshot the deferred-task queue in one step.

        show_for_path reads the flag and appends under the same lock, so a
        caller that read build_finished as False cannot add its task after
        this drain took the snapshot. Such a task would stay in the list, and
        the requested path would never show.
        """
        with self._build_tasks_lock:
            self.build_finished = True
            tasks = list(self.build_task_finished_tasks)
            self.build_task_finished_tasks.clear()
        # Run the tasks outside the lock, because they call back into code
        # near show_for_path, which must not hold it.
        for task in tasks:
            try:
                task()
            except Exception as e:
                log.opt(exception=True).warning(f"Deferred asset-chooser task failed: {e}")

    def on_dnd_accept(self, drop, user_data):
        return True
    
    def on_dnd_drop(self, drop_target, value: Gdk.FileList, x, y):
        paths = value.get_files()
        self.add_files(paths)
        return True
    
    def add_asset(self, asset: dict) -> None:
        # asset_chooser.items is the same live list object as the one in
        # gl.asset_manager_backend, so it already holds the new asset. Only
        # the recycler needs a re-render. This can run off the main thread,
        # from the worker of add_custom_media_set_by_ui, so it marshals.
        chooser = self.asset_chooser
        if chooser is None:
            return
        GLib.idle_add(chooser.refresh)

    def add_files(self, files: list) -> None:
        # The window that owns the cursor can be gone when a drop or a
        # file-dialog callback lands, because AssetManager.on_close nulls
        # gl.asset_manager. Skip the busy cursor then, and still add the files.
        asset_manager = gl.asset_manager
        if asset_manager is not None:
            asset_manager.set_cursor_from_name("wait")
        for path in files:

            url = path.get_uri()
            path = path.get_path()

            # gl.asset_manager_backend.add_custom_media_set_by_ui(url=url, path=path)
            threading.Thread(target=gl.asset_manager_backend.add_custom_media_set_by_ui, args=(url, path), name="add_custom_media_set_by_ui").start()

        if asset_manager is not None:
            asset_manager.set_cursor_from_name("default")

    def show_for_path(self, path):
        def show_now() -> None:
            chooser = self.asset_chooser
            if chooser is None:
                # build() failed before the flow box existed. The failure is
                # already in the log, so do not raise into the caller too.
                return
            chooser.show_for_path(path)

        with self._build_tasks_lock:
            if not self.build_finished:
                # Defer under the same lock that _finish_build drains with.
                # The task either reaches the snapshot, or it reads the flag
                # as True here and dispatches at once.
                self.build_task_finished_tasks.append(show_now)
                return
        show_now()

    def on_video_toggled(self, button):
        # Read-modify-write serialized against the other toggle, so flipping
        # both in quick succession cannot lose one.
        with settings_store.get().edit(settings_store.UI_ASSET_MANAGER) as settings:
            settings["video-toggle"] = button.get_active()

        # Update ui
        if self.asset_chooser is not None:
            self.asset_chooser.refresh()

    def on_image_toggled(self, button):
        with settings_store.get().edit(settings_store.UI_ASSET_MANAGER) as settings:
            settings["image-toggle"] = button.get_active()

        # Update ui
        if self.asset_chooser is not None:
            self.asset_chooser.refresh()

    def load_defaults(self):
        # Both toggles start on. The True defaults live in the surface schema.
        settings = settings_store.get().view(settings_store.UI_ASSET_MANAGER)
        # This runs on the build worker, and a toggle write is a GTK call.
        run_on_main(self.video_button.set_active, settings.get("video-toggle"))
        run_on_main(self.image_button.set_active, settings.get("image-toggle"))

    def on_search_changed(self, entry):
        if self.asset_chooser is not None:
            self.asset_chooser.refresh()

    def on_browse_files_clicked(self, button):
        ChooseFileDialog(self) #TODO: Change to Xdp Portal call


class ChooseFileDialog(Gtk.FileDialog):
    def __init__(self, custom_asset_chooser: CustomAssetChooser):
        super().__init__(title=gl.lm.get("asset-chooser.custom.browse-files.dialog.title"),
                         accept_label=gl.lm.get("asset-chooser.custom.browse-files.dialog.select-button"))
        self.custom_asset_chooser = custom_asset_chooser
        self.open_multiple(callback=self.callback)

    def callback(self, dialog, result):
        try:
            selected_files = self.open_multiple_finish(result)
        except GLib.Error as err:
            log.error(err)
            return
        
        self.custom_asset_chooser.add_files(selected_files)