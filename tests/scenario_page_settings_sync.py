"""
Regression test for auto-switch settings that do not persist.

PageManagerBackend.set_page_settings must refresh a cached page's in-memory
dict as well as the file. Page.save rewrites the whole file from self.dict,
so a stale cached Page erases the settings section at its next save.
"""
import json

import fixtures
import globals as gl
from src.backend.PageManagement import page_flush


def read_json(path: str):
    # Read through the barrier, like every reader of a page file. A settings
    # write and a Page.save are both page edits, marked on the flush seam and
    # written on its timer, so a raw read shows the page before them.
    page_flush.get().flush_path(path)
    with open(path) as f:
        return json.load(f)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_page_settings_sync")
    controller = fixtures.make_headless_controller(serial="pagesync-1")
    try:
        # A second page, cached for this controller but not active. The
        # fixture leaves controller.active_page on "Main".
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

        # The cached Page object must be refreshed as well.
        in_memory = cached_page.dict.get("settings", {}).get("auto-change", {})
        assert in_memory.get("wm-class") == "firefox", (
            f"cached Page.dict is stale after set_page_settings: {in_memory} -- "
            f"the next Page.save() will erase the auto-change settings"
        )

        # A routine save, from a plugin settings write or a key edit, then
        # keeps the settings section intact.
        cached_page.save()
        after_save = read_json(target_path).get("settings", {}).get("auto-change", {})
        assert after_save.get("enable") is True and after_save.get("wm-class") == "firefox", (
            f"Page.save() from the cached page erased the freshly saved "
            f"auto-change settings: {after_save}"
        )
        print("PASS: auto-change settings survive a save() from a cached page")

        # The same guarantee holds when the edited page is the active one.
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
