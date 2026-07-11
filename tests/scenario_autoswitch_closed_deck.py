"""
Regression test for "auto-page-switch regex changes require deck
disable/enable" (issue #104) -- the handler-abort half.

WindowGrabber.on_active_window_changed used `return` where it needed to
skip: as soon as one closed/disabled deck was encountered on a regex match
(and again in the no-match restore branch), the WHOLE handler bailed --
aborting auto page switching for every remaining deck and page. One disabled
deck plus one enabled auto-change page whose regex matched the foreground
window was enough to kill auto-switching everywhere; re-enabling the deck
"fixed" it, which is exactly the reported symptom.

(The other half of #104 -- the saved regex being erased from disk by a stale
cached Page.save() -- is covered by scenario_page_settings_sync.)

Repro: two headless controllers, the FIRST one's deck reporting closed, an
auto-change page whose regex matches the foreground window and whose decks
list targets the SECOND controller. The window-change event must still
switch the second deck's page.
"""
import json

import fixtures
import globals as gl


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_autoswitch_closed_deck")
    controller_closed = fixtures.make_headless_controller(serial="closed-1")
    controller_open = fixtures.make_headless_controller(serial="open-2")
    try:
        from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
        from src.backend.WindowGrabber.Window import Window

        # Bare instance: skip __init__ so no desktop-environment integration
        # (kdotool/gnome/x11 watchers) is probed or started in the harness.
        grabber = object.__new__(WindowGrabber)

        # The first controller in gl.deck_manager.deck_controller reports a
        # closed deck -- the "disabled deck" case.
        controller_closed.deck.is_open = lambda: False
        assert gl.deck_manager.deck_controller[0] is controller_closed

        # Auto-change page matching the foreground window, targeting ONLY
        # the open deck.
        target_path = fixtures.seed_page("BrowserPage")
        gl.page_manager.set_auto_change_settings(
            target_path, enable=True, wm_class="firefox", regex_title=".*",
            stay_on_page=True, decks=[controller_open.serial_number()],
        )

        assert controller_open.active_page.json_path != target_path

        grabber.on_active_window_changed(Window(wm_class="firefox", title="Mozilla Firefox"))

        assert controller_open.active_page.json_path == target_path, (
            "the open deck never auto-switched: a closed deck earlier in the "
            "controller list aborted the whole window-change handler"
        )
        assert controller_open.page_auto_loaded is True
        print("PASS: auto-switch still fires for open decks behind a closed one")

        # No-match branch must also not abort on the closed deck: a second
        # event that matches nothing must leave the open deck alone (its
        # active page has stay-on-page) without raising.
        grabber.on_active_window_changed(Window(wm_class="kitty", title="terminal"))
        assert controller_open.active_page.json_path == target_path, (
            "stay-on-page was not honored after a non-matching window change"
        )
        print("PASS: non-matching window change handled with a closed deck present")
    finally:
        fixtures.teardown(controller_open)
        fixtures.teardown(controller_closed)

    print("PASS: scenario_autoswitch_closed_deck")


if __name__ == "__main__":
    main()
