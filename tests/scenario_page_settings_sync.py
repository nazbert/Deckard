"""
Regression test for "auto-switch settings don't persist" (issue #113, also
the write-clobber half of #104).

PageManagerBackend.set_page_settings wrote the page file but only refreshed
the in-memory dict of pages ACTIVE on a controller. A cached-but-not-active
Page object (gl.page_manager.pages[controller][path]) kept its pre-edit
dict, and Page.save() rewrites the whole file from self.dict -- it fires
constantly (ActionCore.set_settings -> Page.set_action_settings -> save(),
key/dial/state edits, ...). The first save() from such a stale Page silently
erased the just-saved settings section (auto-change, screensaver, brightness
and background overrides) from disk: auto page switching worked right after
configuring it, then the settings vanished and the Page Editor showed
defaults on reopen. Deck disable/enable rebuilt the page cache, which is why
it looked like the only way to "apply" a regex edit (#104).

Deterministic repro: cache a page on a controller WITHOUT making it active,
write auto-change settings through the same path the Page Editor uses, then
trigger a plain Page.save() on the cached object -- the auto-change block
must survive in the file.
"""
import json
import os

import fixtures
import globals as gl


def read_json(path: str):
    with open(path) as f:
        return json.load(f)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_page_settings_sync")
    controller = fixtures.make_headless_controller(serial="pagesync-1")
    try:
        # A second page, cached for this controller but NOT active
        # (controller.active_page stays "Main" from the fixture).
        target_path = fixtures.seed_page("AutoTarget")
        cached_page = gl.page_manager.get_page(target_path, controller)
        assert controller.active_page.json_path != target_path, (
            "test premise broken: target page must not be the active page"
        )

        # The exact write path the Page Editor's AutoChangeGroup uses.
        gl.page_manager.overwrite_auto_change_settings(
            target_path, enable=True, wm_class="firefox", regex_title="",
            stay_on_page=True, decks=[controller.serial_number()],
        )
        on_disk = read_json(target_path).get("settings", {}).get("auto-change", {})
        assert on_disk.get("enable") is True and on_disk.get("wm-class") == "firefox", (
            f"auto-change settings never reached the file: {on_disk}"
        )

        # The cached Page object must have been refreshed too...
        in_memory = cached_page.dict.get("settings", {}).get("auto-change", {})
        assert in_memory.get("wm-class") == "firefox", (
            f"cached Page.dict is stale after set_page_settings: {in_memory} -- "
            f"the next Page.save() will erase the auto-change settings"
        )

        # ...so that a routine save() (plugin settings write, key edit, ...)
        # keeps the settings section intact instead of reverting it.
        cached_page.save()
        after_save = read_json(target_path).get("settings", {}).get("auto-change", {})
        assert after_save.get("enable") is True and after_save.get("wm-class") == "firefox", (
            f"Page.save() from the cached page erased the freshly saved "
            f"auto-change settings: {after_save}"
        )
        print("PASS: auto-change settings survive a save() from a cached page")

        # Same guarantee when the edited page IS the active one.
        active_path = controller.active_page.json_path
        gl.page_manager.overwrite_auto_change_settings(active_path, enable=True, wm_class="kitty")
        controller.active_page.save()
        active_after = read_json(active_path).get("settings", {}).get("auto-change", {})
        assert active_after.get("wm-class") == "kitty", (
            f"active page save() erased its own auto-change settings: {active_after}"
        )
        print("PASS: auto-change settings survive a save() from the active page")
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_page_settings_sync")


if __name__ == "__main__":
    main()
