"""
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Shared plumbing for the AssetManager's pack/asset chooser pages (#136).

Icon packs, wallpaper packs and SD+ bar wallpaper packs each shipped a
"pick a pack" page and a "pick an asset from that pack" page -- six classes
whose bodies were line-for-line copies apart from the widget classes they
instantiated and the pack manager they read. The search scoring alone was
duplicated three times, so a change to it had to be made three times.

Two bases live here:

  * ``GenericPackChooserPage``  -- the pack grid; drills into the leaf page.
  * ``GenericAssetChooserPage`` -- the recycler grid of a pack's assets, with
    the shared fuzzy search/sort.

Both build the same way: the DATA (pack discovery, disk I/O) is gathered on
the build worker thread, and every widget is constructed and attached inside
one ``run_on_main`` callback. GTK4 is main-thread-only; constructing these
trees on the worker was the off-main-GTK crash class (#136, same class as
#10). ``tests/scenario_asset_chooser_offmain.py`` is the tripwire.

The search helpers at the top are deliberately free of GTK and of ``self`` so
they can be pinned headless (``tests/scenario_asset_chooser_logic.py``).
"""
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

# Import python modules
import os
import threading

from rapidfuzz import fuzz
from loguru import logger as log

# Import own modules
from GtkHelper.GtkHelper import run_on_main
from src.windows.AssetManager.ChooserPage import ChooserPage
from src.windows.AssetManager.Preview import Preview

# Import globals
import globals as gl

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.AssetManager.AssetManager import AssetManager


# A candidate must score at least this against the query to stay in the grid.
SEARCH_SCORE_THRESHOLD = 50

def asset_display_name(item, attr: str = "path") -> str:
    """The string the search matches on: the asset's file name without its
    directory or extension (what the preview's label shows)."""
    return os.path.splitext(os.path.basename(getattr(item, attr)))[0]


def asset_matches_search(item, search: str, attr: str = "path") -> bool:
    """Filter predicate: an empty query keeps everything, otherwise the
    display name must score at least SEARCH_SCORE_THRESHOLD."""
    if search == "":
        return True
    score = fuzz.ratio(asset_display_name(item, attr).lower(), search.lower())
    return score >= SEARCH_SCORE_THRESHOLD


def compare_assets(item1, item2, search: str, attr: str = "path") -> int:
    """GTK sort comparator over two assets: -1 if item1 comes first, 1 if it
    comes last, 0 if they tie.

    Empty query -> plain (case-sensitive) alphabetical order by display name.
    Otherwise -> descending fuzzy score against the query; equal scores tie,
    which leaves their relative input order alone (``sorted`` is stable)."""
    name1 = asset_display_name(item1, attr)
    name2 = asset_display_name(item2, attr)

    if search == "":
        if name1 < name2:
            return -1
        if name1 > name2:
            return 1
        return 0

    score1 = fuzz.ratio(name1.lower(), search.lower())
    score2 = fuzz.ratio(name2.lower(), search.lower())

    if score1 > score2:
        return -1
    if score1 < score2:
        return 1
    return 0


class _ChooserBuildPage(ChooserPage):
    """Build bookkeeping and failure recovery shared by both chooser bases.

    `run_on_main` raises RuntimeError when the main loop does not service its
    idle within RUN_ON_MAIN_TIMEOUT_S (30 s) -- reachable app-wide whenever
    anything stalls the loop. Unhandled, `@log.catch` swallowed that raise
    and left the page permanently stuck: spinner spinning forever,
    build_finished never set, any deferred show_for_path stranded, no retry
    and no message (#136 review M2).
    """

    build_finished = False
    build_failed = False
    _build_running = False

    def start_build(self) -> bool:
        """Starts the build worker unless one is already in flight. Called
        from the main thread only (constructor, retry_build), so the flag
        needs no lock."""
        if self._build_running:
            return False
        self._build_running = True
        threading.Thread(target=self._run_build,
                         name=f"{type(self).__name__}.build").start()
        return True

    def _run_build(self) -> None:
        try:
            self.build()
        finally:
            self._build_running = False

    def retry_build(self) -> bool:
        """Rebuilds a page whose build failed -- AssetManager calls this when
        the window is reopened. No-op on a healthy page."""
        if not self.build_failed:
            return False
        self.build_failed = False
        self.set_loading(True)
        return self.start_build()

    def _handle_build_failure(self, error: BaseException) -> None:
        log.opt(exception=error).error(
            f"{type(self).__name__}: the main loop did not service this page's "
            f"build; showing an error -- it rebuilds when the window is reopened")
        self.build_finished = False
        self.build_failed = True
        self._reset_build_state()
        # Fire-and-forget: run_on_main is the very thing that just failed, and
        # waiting on it again would park this worker for another full timeout.
        GLib.idle_add(self._show_build_error)

    def _show_build_error(self) -> bool:
        """Turns ChooserPage's loading page into an error panel: a stopped
        spinner beats one that spins forever."""
        self.spinner.stop()
        self.loading_label.set_label(gl.lm.get("error"))
        self.set_visible_child_name("loading")
        return False  # one-shot idle

    def _reset_build_state(self) -> None:
        """Drops whatever the failed build would have consumed, so nothing is
        left pointing at a page that never got built."""


class GenericPackChooserPage(_ChooserBuildPage):
    """The pack grid of one asset type.

    Subclasses supply the two widget classes, the stack child to drill into,
    and where the packs come from."""

    # Gtk.Box subclass owning a `.flow_box`; ctor (chooser, **props).
    PACK_FLOW_BOX_CLASS: type = None
    # Preview subclass; ctor (chooser, pack), exposing `.pack`.
    PACK_PREVIEW_CLASS: type = None
    # Name of the stack child holding this type's asset chooser.
    LEAF_CHILD_NAME: str = None
    # Pack previews constructed per main-loop callback.
    PACK_APPEND_BATCH: int = 10

    # Bound by _build_ui on the main loop; readers must tolerate None (a
    # marshal that timed out never binds it -- see _handle_build_failure).
    pack_flow = None

    def __init__(self, stack, asset_manager: "AssetManager"):
        super().__init__()
        self.asset_manager = asset_manager
        self.stack = stack

        self.build_finished = False

        self.start_build()

    @log.catch
    def build(self):
        self.build_finished = False

        # Worker-thread work: pack discovery hits the disk, and so does the
        # thumbnail decode. A GdkPixbuf decode is file I/O, not GTK widgetry,
        # so it is safe here -- and it MUST be here: at ~17 ms per store
        # thumbnail (over 100 ms for an oversized one) decoding the whole grid
        # inside the main-loop callback froze the window for hundreds of
        # milliseconds to tens of seconds (#136 review M1).
        packs = list(self.get_packs().values())
        thumbnails = [Preview.decode_pixbuf(self.get_pack_thumbnail_path(pack))
                      for pack in packs]

        try:
            # Widgets are GTK, so they are built on the main loop (#136).
            run_on_main(self._build_ui)

            # One batch per main-loop callback: constructing every preview in
            # a single callback would hand the loop back only after the last
            # one, so a big pack set would still stutter even without decodes.
            for start in range(0, len(packs), self.PACK_APPEND_BATCH):
                stop = start + self.PACK_APPEND_BATCH
                run_on_main(self._append_packs, list(zip(packs[start:stop],
                                                         thumbnails[start:stop])))
        except Exception as e:
            self._handle_build_failure(e)
            return

        self.set_loading(False)

        self.build_finished = True
        self.on_build_finished()

    def _build_ui(self) -> None:
        """Runs on the main loop only."""
        self.type_box.set_visible(False)

        self.pack_flow = self.PACK_FLOW_BOX_CLASS(self, orientation=Gtk.Orientation.HORIZONTAL,
                                                  hexpand=True)
        self.scrolled_box.prepend(self.pack_flow)

        self.pack_flow.flow_box.connect("child-activated", self.on_child_activated)

    def _append_packs(self, batch: list) -> None:
        """Runs on the main loop only: one batch of (pack, pixbuf) pairs."""
        flow_box = self.pack_flow.flow_box
        for pack, pixbuf in batch:
            preview = self.PACK_PREVIEW_CLASS(self, pack, pixbuf=pixbuf)
            flow_box.append(preview)

    def get_pack_thumbnail_path(self, pack):
        """Where the pack's thumbnail lives. Called on the build worker."""
        return pack.get_thumbnail_path()

    def on_child_activated(self, flow_box, child):
        # Load the pack's assets, drill into the asset chooser, offer the way back.
        self.get_leaf_chooser().load_for_pack(child.pack)
        self.stack.set_visible_child_name(self.LEAF_CHILD_NAME)
        self.asset_manager.back_button.set_visible(True)

    # ----------------------------------------------------------------- #
    # Subclass hooks
    # ----------------------------------------------------------------- #

    def get_packs(self) -> dict:
        """{name: pack} for this asset type. Called on the build worker."""
        raise NotImplementedError

    def get_leaf_chooser(self) -> "GenericAssetChooserPage":
        """The sibling page that shows one pack's assets."""
        raise NotImplementedError

    def on_build_finished(self) -> None:
        """Called after build(), off the main thread. Only the icon stack
        gates deferred work on it."""


class GenericAssetChooserPage(_ChooserBuildPage):
    """The asset grid of one pack: a recycling DynamicFlowBox plus the shared
    fuzzy search/sort."""

    # DynamicFlowBox subclass; ctor (preview_class, chooser).
    FLOW_BOX_CLASS: type = None
    # Preview subclass; ctor takes no arguments (the flow box pools them).
    PREVIEW_CLASS: type = None
    # Attribute holding an asset's file path.
    ASSET_PATH_ATTR: str = "path"

    # Class-level defaults: the ChooserPage constructor wires the search entry
    # (and so on_search_changed) before __init__ gets to its own attributes.
    asset_flow = None
    _pending_pack = None

    def __init__(self, stack, asset_manager: "AssetManager"):
        super().__init__()
        self.asset_manager = asset_manager
        self.stack = stack

        self.selected_path: str = None

        # Bound by _build_ui on the main loop, i.e. not before this
        # constructor returns -- everything touching it tolerates None.
        self.asset_flow = None
        self._pending_pack = None

        self.build_finished = False

        self.start_build()

    @log.catch
    def build(self):
        self.build_finished = False
        self.set_loading(True)

        try:
            # The flow box builds a page's worth of previews in its
            # constructor, so the whole construction runs on the main loop.
            run_on_main(self._build_ui)
        except Exception as e:
            self._handle_build_failure(e)
            return

        self.set_loading(False)

        self.build_finished = True
        self.on_build_finished()

    def _build_ui(self) -> None:
        """Runs on the main loop only."""
        self.type_box.set_visible(False)

        self.asset_flow = self.FLOW_BOX_CLASS(self.PREVIEW_CLASS, self)
        self.asset_flow.set_factory(self.preview_factory)
        self.asset_flow.set_filter_func(self.filter_func)
        self.asset_flow.set_sort_func(self.sort_func)
        # The flow box brings its own ScrolledWindow and pagination; the
        # page's default scroller must go or it competes for the height.
        self.main_box.remove(self.scrolled_window)
        self.main_box.append(self.asset_flow)

        # Connect flow box select signal
        self.asset_flow.flow_box.connect("child-activated", self.on_child_activated)

        # A pack activated while this callback was still queued renders now
        # instead of being dropped (see load_for_pack).
        if self._pending_pack is not None:
            pack, self._pending_pack = self._pending_pack, None
            self.load_for_pack(pack)

    def load_for_pack(self, pack) -> None:
        if self.asset_flow is None:
            # Build still queued on the main loop -- remember the request
            # rather than dropping it (or raising AttributeError).
            self._pending_pack = pack
            return
        self.asset_flow.set_item_list(self.get_assets(pack))
        # The recycler owns its page size (it sized its preview pool to it).
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)

    def select_asset(self, path: str) -> None:
        """Pre-selects the asset at `path` once it is rendered."""
        self.selected_path = path

    def _reset_build_state(self) -> None:
        # There is no grid to render it into and no build left to consume it;
        # holding it would strand the request silently.
        self._pending_pack = None

    def on_child_activated(self, flow_box, child):
        asset = self.get_child_asset(child)
        self.asset_manager.deliver_selection(getattr(asset, self.ASSET_PATH_ATTR))

    def preview_factory(self, preview, asset):
        # Called from DynamicFlowBox._apply_range's main-loop callback.
        self.bind_preview(preview, asset)
        if self.selected_path == getattr(asset, self.ASSET_PATH_ATTR):
            self.asset_flow.flow_box.select_child(preview)

    def filter_func(self, item) -> bool:
        return asset_matches_search(item, self.search_entry.get_text(),
                                    self.ASSET_PATH_ATTR)

    def sort_func(self, item1, item2) -> int:
        return compare_assets(item1, item2, self.search_entry.get_text(),
                              self.ASSET_PATH_ATTR)

    def on_search_changed(self, entry):
        if self.asset_flow is None:
            # Nothing rendered yet (build still queued on the main loop);
            # the first render already reads the current search text.
            return
        self.asset_flow.show_range(0, self.asset_flow.N_ITEMS_PER_PAGE)

    # ----------------------------------------------------------------- #
    # Subclass hooks
    # ----------------------------------------------------------------- #

    def get_assets(self, pack) -> list:
        """The pack's assets, in the order the grid should receive them."""
        raise NotImplementedError

    def bind_preview(self, preview, asset) -> None:
        """Show `asset` in the recycled `preview`."""
        raise NotImplementedError

    def get_child_asset(self, child):
        """The asset an activated flow-box child is showing."""
        raise NotImplementedError

    def on_build_finished(self) -> None:
        """Called after build(), off the main thread. Only the icon stack
        gates deferred work on it."""
