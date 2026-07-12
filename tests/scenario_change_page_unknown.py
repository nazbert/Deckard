"""
Regression test for gl#43: App.on_change_page crashed on an unknown page
name.

find_matching_page_path returns None for a name that matches no page, but
on_change_page compared os.path.abspath(None) against the active page's
path (TypeError) and called get_page(page_path=None, ...) BEFORE its
`if page_path is None` check -- so `--change-page SERIAL <bad-name>` threw
inside the Gio action handler and silently did nothing. on_change_state
always had the check in the right place; the fix mirrors it.

on_change_page only dereferences `self.deck_manager` and a
`data.unpack()`, so it is driven unbound against a real headless
controller without constructing the Adw.Application.
"""
import os

import fixtures
import globals as gl


class FakeVariant:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeAppSelf:
    def __init__(self, deck_manager):
        self.deck_manager = deck_manager


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_change_page_unknown")
    controller = fixtures.make_headless_controller(serial="chpg-1")
    try:
        from src.app import App

        fake_app = FakeAppSelf(gl.deck_manager)

        assert controller.active_page is not None, "headless controller should have loaded the seeded page"
        active_before = controller.active_page

        # --- Unknown page name: must not raise, must not change the page.
        # (Pre-fix: os.path.abspath(None) -> TypeError out of the handler.)
        App.on_change_page(fake_app, None, FakeVariant(("chpg-1", "no-such-page")))
        assert controller.active_page is active_before, (
            "unknown page name must leave the active page untouched"
        )

        # --- Unknown page with NO active page: pre-fix this reached
        # get_page(page_path=None) before the None-check; must also skip.
        controller.active_page = None
        App.on_change_page(fake_app, None, FakeVariant(("chpg-1", "no-such-page")))
        assert controller.active_page is None, "skip path must not load anything"
        controller.active_page = active_before

        # --- Happy path still works after the reorder.
        target_path = fixtures.seed_page("ChangeTarget")
        App.on_change_page(fake_app, None, FakeVariant(("chpg-1", "ChangeTarget")))
        assert fixtures.wait_until(
            lambda: controller.active_page is not None
            and os.path.abspath(controller.active_page.json_path) == os.path.abspath(target_path)
        ), "known page name must still load the page"

        # --- Same page again: no-op via the abspath compare, no raise.
        App.on_change_page(fake_app, None, FakeVariant(("chpg-1", "ChangeTarget")))

        # --- Non-matching serial: loop body never runs, must not raise.
        App.on_change_page(fake_app, None, FakeVariant(("other-serial", "ChangeTarget")))
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_change_page_unknown")


if __name__ == "__main__":
    main()
