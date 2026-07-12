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
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

# Import python modules
from fuzzywuzzy import fuzz
import threading
from loguru import logger as log

# Import globals
import globals as gl

# Import own modules
from src.backend.DeckManagement.HelperMethods import is_video
from src.windows.AssetManager.CustomAssets.AssetPreview import AssetPreview
from src.windows.AssetManager.DynamicFlowBox import DynamicFlowBox

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.CustomAssets.Chooser import CustomAssetChooser


class CustomAssetChooserFlowBox(DynamicFlowBox):
    def __init__(self, asset_chooser, *args, **kwargs):
        super().__init__(base_class=AssetPreview, *args, **kwargs)
        self.set_hexpand(True)

        self.asset_chooser:"CustomAssetChooser" = asset_chooser
        self.selected_asset: str = None

        self.set_factory(self.preview_factory)
        self.set_filter_func(self.filter_func)
        self.set_sort_func(self.sort_func)

        self.flow_box.connect("child-activated", self.on_child_activated)

        # There is only ever one "pack" for custom assets (the whole backend list), so it can
        # be loaded eagerly once the recycler itself is built -- see DynamicFlowBox docstring.
        self.load_assets()

    def load_assets(self) -> None:
        self.set_item_list(gl.asset_manager_backend.get_all())
        self.refresh()

    def refresh(self) -> None:
        self.show_range(0, self.N_ITEMS_PER_PAGE)

    def show_for_path(self, path):
        self.select_asset(path)
        self.refresh()

    def select_asset(self, path) -> None:
        self.selected_asset = path

    def preview_factory(self, preview: AssetPreview, asset: dict):
        preview.set_asset(self, asset)
        if self.selected_asset == asset.get("internal-path"):
            self.flow_box.select_child(preview)

    def filter_func(self, asset: dict) -> bool:
        search_string = self.asset_chooser.search_entry.get_text()
        show_image = self.asset_chooser.image_button.get_active()
        show_video = self.asset_chooser.video_button.get_active()

        asset_is_video = is_video(asset["internal-path"])

        if asset_is_video and not show_video:
            return False
        if not asset_is_video and not show_image:
            return False

        if search_string == "":
            return True

        fuzz_score = fuzz.ratio(search_string.lower(), asset["name"].lower())
        if fuzz_score < 40:
            return False

        return True

    def sort_func(self, a: dict, b: dict) -> int:
        search_string = self.asset_chooser.search_entry.get_text()

        if search_string == "":
            # Sort alphabetically
            if a["name"] < b["name"]:
                return -1
            if a["name"] > b["name"]:
                return 1
            return 0

        a_fuzz = fuzz.ratio(search_string.lower(), a["name"].lower())
        b_fuzz = fuzz.ratio(search_string.lower(), b["name"].lower())

        if a_fuzz > b_fuzz:
            return -1
        elif a_fuzz < b_fuzz:
            return 1

        return 0

    def on_child_activated(self, flow_box, child):
        # Capture the selection and callback *before* spawning the thread (P4.2 prerequisite b):
        # under window reuse, a stale thread that re-reads `self.asset_chooser.asset_manager`
        # from inside the thread body could end up calling a *new* callback with a *new*
        # window's state if the user reopens the Asset Manager while this thread is still
        # in flight.
        asset_path = child.asset["internal-path"]
        callback = self.asset_chooser.asset_manager.callback_func
        callback_args = self.asset_chooser.asset_manager.callback_args
        callback_kwargs = self.asset_chooser.asset_manager.callback_kwargs

        # Captured -- drop the manager's own refs so the hidden singleton
        # doesn't keep pinning the opener's bound callback (and through it
        # the action/page graph) until the next show_for_path.
        self.asset_chooser.asset_manager.callback_func = None
        self.asset_chooser.asset_manager.callback_args = ()
        self.asset_chooser.asset_manager.callback_kwargs = {}

        if callable(callback):
            callback_thread = threading.Thread(
                target=self.callback_thread,
                args=(asset_path, callback, callback_args, callback_kwargs),
                name="flow_box_callback_thread"
            )
            callback_thread.start()

        # Hide (not close()) so the window survives for reuse (P4.2): close() falls through to
        # GTK4's default close-request handling, which destroys the window on the next
        # main-loop iteration even without an explicit destroy() call (verified empirically).
        self.asset_chooser.asset_manager.hide()

    @log.catch
    def callback_thread(self, asset_path, callback, callback_args, callback_kwargs):
        callback(asset_path, *callback_args, **callback_kwargs)

    def remove_asset(self, asset: dict) -> None:
        gl.asset_manager_backend.remove_asset_by_id(asset["id"])
        self.flow_box.unselect_all()
        self.refresh()