"""
Persistence round-trip for gl#64: edit a NON-active page's settings, ACTIVATE
that page, then save -- the edit must survive.

scenario_page_settings_sync.py already pins the "edit a cached non-active page
-> save it (still non-active) -> settings survive" leg (issue #113/#104). The
audit's #64 ask names a distinct sequence the sync scenario does NOT walk: the
page is *promoted to active* between the settings edit and the save. If
set_page_settings failed to refresh the cached Page object (the pre-f386da73
behavior, which only refreshed pages already active), activation adopts the
SAME stale cached Page object -- so the first ordinary Page.save() after
activation (a plugin set_settings, a key/state edit, anything) rewrites the
file from the stale dict and silently erases the just-saved settings section.

This is the direct regression net for the revert-on-save data loss.
Red-proved by reverting f386da73's set_page_settings body (only-refresh-active)
-- see the scenario report.

Runs on a REAL DeckController over the FaultyFakeDeck (no GTK), so activation
goes through the real controller.load_page path, not a stub.
"""
import json
import os

import fixtures
import globals as gl


def read_settings(path: str) -> dict:
    with open(path) as f:
        return json.load(f).get("settings", {})


def check_edit_survives_activation_then_save(controller) -> None:
    # A second page, cached for this controller but NOT active yet.
    target_path = fixtures.seed_page("ActivateTarget")
    cached_page = gl.page_manager.get_page(target_path, controller)
    assert controller.active_page is not None
    assert controller.active_page.json_path != target_path, (
        "test premise broken: target page must start non-active"
    )

    # Edit the NON-active page's settings section (the Page Editor path).
    new_settings = {
        "auto-change": {"enable": True, "wm-class": "firefox"},
        "brightness": {"value": 42},
        "background": {"show-on-background": True},
    }
    gl.page_manager.set_page_settings(target_path, new_settings)

    # Sanity: the edit reached disk.
    on_disk = read_settings(target_path)
    assert on_disk.get("auto-change", {}).get("wm-class") == "firefox", (
        f"set_page_settings never wrote the settings: {on_disk}"
    )

    # Now ACTIVATE the page. get_page returns the same cached object; load_page
    # promotes it to active_page. If set_page_settings did NOT refresh that
    # cached object, it is now the active page carrying a STALE dict.
    page_to_activate = gl.page_manager.get_page(target_path, controller)
    controller.load_page(page_to_activate)
    assert fixtures.wait_until(
        lambda: controller.active_page is not None
        and controller.active_page.json_path == target_path,
        timeout=5,
    ), "page never became active"

    # The cached/active Page.dict must already carry the edit (not a stale
    # pre-edit copy) -- otherwise the save below reverts the file.
    active_settings = controller.active_page.dict.get("settings", {})
    assert active_settings.get("auto-change", {}).get("wm-class") == "firefox", (
        f"the activated page carries a STALE settings dict: {active_settings} -- "
        f"the next save() will erase the freshly saved settings"
    )

    # A routine save() (what a plugin set_settings / key edit triggers) must
    # NOT revert the settings section.
    controller.active_page.save()
    after = read_settings(target_path)
    assert after.get("auto-change", {}).get("wm-class") == "firefox", (
        f"save() after activation erased the auto-change settings: {after} "
        f"(revert-on-save, gl#64/#28)"
    )
    assert after.get("brightness", {}).get("value") == 42, (
        f"save() after activation erased the brightness override: {after}"
    )
    assert after.get("background", {}).get("show-on-background") is True, (
        f"save() after activation erased the background override: {after}"
    )
    print("PASS: a non-active edit survives activation + a subsequent save()")


def check_edit_then_activate_then_second_edit(controller) -> None:
    """A stricter round-trip: edit while non-active, activate, edit AGAIN
    through the same page-settings path while now active, save. Both keys must
    coexist -- the activation must not have stranded a stale baseline that the
    second write merges on top of."""
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
        check_edit_then_activate_then_second_edit(controller)
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_page_settings_activate_roundtrip")


if __name__ == "__main__":
    main()
