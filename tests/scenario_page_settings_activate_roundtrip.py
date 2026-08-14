"""
Persistence round-trip for a page edited, then activated, then saved.

set_page_settings must refresh the cached Page object, not only the pages
already active. Activation adopts the same cached object, so a stale dict
makes the first ordinary save erase the settings section. A real
DeckController over the FaultyFakeDeck drives the activation.
"""
import json

import fixtures
import globals as gl
from src.backend.PageManagement import page_flush


def read_settings(path: str) -> dict:
    # Read through the barrier, like every reader of a page file. A settings
    # write and a Page.save are both page edits, marked on the flush seam and
    # written on its timer, so a raw read shows the page before them.
    page_flush.get().flush_path(path)
    with open(path) as f:
        return json.load(f).get("settings", {})


def check_edit_survives_activation_then_save(controller) -> None:
    # A second page, cached for this controller but not active yet.
    target_path = fixtures.seed_page("ActivateTarget")
    cached_page = gl.page_manager.get_page(target_path, controller)
    assert controller.active_page is not None
    assert controller.active_page.json_path != target_path, (
        "test premise broken: target page must start non-active"
    )

    # Edit the non-active page's settings section, on the Page Editor path.
    new_settings = {
        "auto-change": {"enable": True, "wm-class": "firefox"},
        "brightness": {"value": 42},
        "background": {"show-on-background": True},
    }
    gl.page_manager.set_page_settings(target_path, new_settings)

    # The edit reached disk.
    on_disk = read_settings(target_path)
    assert on_disk.get("auto-change", {}).get("wm-class") == "firefox", (
        f"set_page_settings never wrote the settings: {on_disk}"
    )

    # Activate the page. get_page returns the same cached object and load_page
    # promotes it to active_page. Without a refresh in set_page_settings, the
    # active page now carries a stale dict.
    page_to_activate = gl.page_manager.get_page(target_path, controller)
    controller.load_page(page_to_activate)
    assert fixtures.wait_until(
        lambda: controller.active_page is not None
        and controller.active_page.json_path == target_path,
        timeout=5,
    ), "page never became active"

    # The active Page.dict must already carry the edit. Otherwise the save
    # below reverts the file.
    active_settings = controller.active_page.dict.get("settings", {})
    assert active_settings.get("auto-change", {}).get("wm-class") == "firefox", (
        f"the activated page carries a STALE settings dict: {active_settings} -- "
        f"the next save() will erase the freshly saved settings"
    )

    # A routine save, from a plugin set_settings or a key edit, must not
    # revert the settings section.
    controller.active_page.save()
    after = read_settings(target_path)
    assert after.get("auto-change", {}).get("wm-class") == "firefox", (
        f"save() after activation erased the auto-change settings: {after} "
        f"(revert-on-save)"
    )
    assert after.get("brightness", {}).get("value") == 42, (
        f"save() after activation erased the brightness override: {after}"
    )
    assert after.get("background", {}).get("show-on-background") is True, (
        f"save() after activation erased the background override: {after}"
    )
    print("PASS: a non-active edit survives activation + a subsequent save()")


def check_edit_activate_second_edit(controller) -> None:
    """A stricter round-trip. Edit while non-active, activate, edit again
    through the page-settings path, then save. Both keys must coexist,
    because a stranded stale baseline would swallow the second write."""
    target_path = fixtures.seed_page("ActivateTarget2")
    cached = gl.page_manager.get_page(target_path, controller)
    assert controller.active_page.json_path != target_path

    gl.page_manager.set_page_settings(target_path, {"a": {"first": 1}})
    controller.load_page(gl.page_manager.get_page(target_path, controller))
    assert fixtures.wait_until(
        lambda: controller.active_page.json_path == target_path, timeout=5
    )

    # Second edit while active, then a routine save.
    settings = dict(gl.page_manager.get_page_settings(target_path))
    settings["b"] = {"second": 2}
    gl.page_manager.set_page_settings(target_path, settings)
    controller.active_page.save()

    after = read_settings(target_path)
    assert after.get("a", {}).get("first") == 1 and after.get("b", {}).get("second") == 2, (
        f"a settings key was lost across activate + re-edit + save: {after}"
    )
    print("PASS: settings edits coexist across activation and re-edit")


def main() -> None:
    fixtures.start_watchdog(45, label="scenario_page_settings_activate_roundtrip")
    controller = fixtures.make_headless_controller(serial="activate-roundtrip-1")
    try:
        check_edit_survives_activation_then_save(controller)
        check_edit_activate_second_edit(controller)
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_page_settings_activate_roundtrip")


if __name__ == "__main__":
    main()
