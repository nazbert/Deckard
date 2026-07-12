"""
Regression test for "AssetManager window reuse leaks stale tab/drill-in and
stale search filter on reopen" (gl#48).

The AssetManager window is reused across opens (deliver_selection hides it,
P4.2). Before the fix, show_for_path reset nothing: the custom-asset branch
never switched the top tab back to "custom-assets" (only the icon branch did),
and no search entry was ever cleared -- so reopening for a custom asset after
drilling into a pack kept showing the old drilled grid, and a leftover filter
could hide the very asset being pre-selected.

Contract under test (src/windows/AssetManager/AssetManager.py):
  * show_for_path() calls _reset_session_state(), which backs EVERY pack stack
    out of its drilled-in chooser (set_visible_child_name("pack-chooser")) and
    clears EVERY non-empty search entry;
  * the custom-asset branch of AssetChooser.show_for_path sets the top-level
    visible child to "custom-assets";
  * an EMPTY search entry is left untouched (set_text fires search-changed,
    and CustomAssetChooser's handler dereferences widgets its background
    build() may not have attached on a truly fresh window).

Drives the REAL AssetManager window (all four choosers, real Gtk.Stacks, real
ChooserPage search entries). The pack managers / asset backend are stubbed to
hold no data so the threaded builds iterate empty and finish quickly; we pump
the GLib loop until every build reports finished before asserting.

Needs a display (it builds real Adw/Gtk widgets); prints SKIP and exits 0 when
GTK can't initialize, like a headless CI box.
"""
import os

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)
import globals as gl


def _pump(context, predicate, watchdog, budget=10.0):
    """Iterate the GLib main loop until predicate() is true (the threaded
    chooser builds marshal their final steps back via GLib.idle_add), or the
    budget elapses."""
    import time
    deadline = time.monotonic() + budget
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("chooser builds did not finish within budget")
        context.iteration(False)
        time.sleep(0.01)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_asset_manager_reopen")

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib

    if not Gtk.init_check():
        print("SKIP: no display available; scenario needs GTK")
        return
    Adw.init()

    # --- Stub the data providers: no packs, no custom assets. The window's
    #     structure (stacks + search entries) is what the reset walks; the
    #     content builds just need to iterate empty and finish. ---
    class EmptyPackManager:
        def get_icon_packs(self): return {}
        def get_wallpaper_packs(self): return {}

    class EmptyBackend:
        def get_all(self): return []
        def has_by_internal_path(self, path): return True  # route to custom-asset branch
        def remove_asset_by_id(self, asset_id): pass
        def add_custom_media_set_by_ui(self, *a, **k): pass

    class StubLM:
        def get(self, key, *a, **k): return key

    gl.icon_pack_manager = EmptyPackManager()
    gl.wallpaper_pack_manager = EmptyPackManager()
    gl.sd_plus_bar_wallpaper_pack_manager = EmptyPackManager()
    gl.asset_manager_backend = EmptyBackend()
    gl.lm = StubLM()
    gl.app = None

    # Real SettingsManager rooted at the isolated harness DATA_PATH (the
    # custom-asset build reads settings/ui/AssetManager.json on load_defaults).
    if getattr(gl, "settings_manager", None) is None:
        from src.backend.SettingsManager import SettingsManager
        gl.settings_manager = SettingsManager()

    from src.windows.AssetManager.AssetManager import AssetManager

    context = GLib.MainContext.default()
    main_window = Gtk.Window()  # transient-for parent stand-in
    am = AssetManager(main_window=main_window)
    chooser = am.asset_chooser

    # Wait for all four threaded content builds to finish so their search
    # entries and stacks are fully wired before we manipulate them.
    def builds_done():
        return (
            getattr(chooser.custom_asset_chooser, "build_finished", False)
            and chooser.icon_pack_chooser.get_is_build_finished()
        )
    _pump(context, builds_done, fixtures)

    # ------------------------------------------------------------------
    # 1. Simulate a previous session that drilled into every pack stack and
    #    left stale search filters + a non-custom top tab.
    # ------------------------------------------------------------------
    chooser.set_visible_child_name("icon-packs")
    chooser.icon_pack_chooser.set_visible_child_name("icon-chooser")
    chooser.wallpaper_pack_chooser.set_visible_child_name("wallpaper-chooser")
    chooser.sd_plus_bar_wallpaper_pack_chooser.set_visible_child_name("wallpaper-chooser")
    am.back_button.set_visible(True)

    chooser.icon_pack_chooser.pack_chooser.search_entry.set_text("stale-icon-filter")
    chooser.wallpaper_pack_chooser.pack_chooser.search_entry.set_text("stale-wp-filter")
    chooser.custom_asset_chooser.search_entry.set_text("stale-custom-filter")

    assert chooser.get_visible_child_name() != "custom-assets"
    assert chooser.icon_pack_chooser.get_visible_child_name() == "icon-chooser"

    # ------------------------------------------------------------------
    # 2. Reopen for a custom asset. show_for_path must reset navigation +
    #    filters and switch to the custom-assets tab.
    # ------------------------------------------------------------------
    am.show_for_path("/some/custom/asset.png")

    assert chooser.get_visible_child_name() == "custom-assets", (
        f"custom-asset reopen did not switch the tab: "
        f"{chooser.get_visible_child_name()!r} (a drilled-in pack grid would still show)"
    )
    print("PASS: custom-asset reopen switches to the custom-assets tab")

    assert chooser.icon_pack_chooser.get_visible_child_name() == "pack-chooser", \
        "icon pack stack still drilled into icon-chooser after reopen"
    assert chooser.wallpaper_pack_chooser.get_visible_child_name() == "pack-chooser", \
        "wallpaper pack stack still drilled in after reopen"
    assert chooser.sd_plus_bar_wallpaper_pack_chooser.get_visible_child_name() == "pack-chooser", \
        "sd+ bar wallpaper pack stack still drilled in after reopen"
    assert not am.back_button.get_visible(), "back button still visible after reopen"
    print("PASS: every pack stack backed out of its drilled-in chooser")

    assert chooser.icon_pack_chooser.pack_chooser.search_entry.get_text() == "", \
        "stale icon search filter survived reopen"
    assert chooser.wallpaper_pack_chooser.pack_chooser.search_entry.get_text() == "", \
        "stale wallpaper search filter survived reopen"
    assert chooser.custom_asset_chooser.search_entry.get_text() == "", \
        "stale custom-asset search filter survived reopen (could hide the pre-selected asset)"
    print("PASS: every stale search filter cleared on reopen")

    print("PASS: scenario_asset_manager_reopen")


if __name__ == "__main__":
    main()
