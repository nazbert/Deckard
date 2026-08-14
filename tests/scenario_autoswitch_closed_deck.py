"""Regression test for the auto-page-switch handler abort.

WindowGrabber.on_active_window_changed must skip a closed or disabled deck
and keep switching the rest. One bail aborts auto-switching everywhere.
"""

import fixtures
import globals as gl


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_autoswitch_closed_deck")
    controller_closed = fixtures.make_headless_controller(serial="closed-1")
    controller_open = fixtures.make_headless_controller(serial="open-2")
    try:
        from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
        from src.backend.WindowGrabber.Window import Window

        # Build a bare instance and skip __init__, so the harness probes and
        # starts no desktop-environment watcher.
        grabber = object.__new__(WindowGrabber)

        # The first controller in gl.deck_manager.deck_controller reports a
        # closed deck, which is the disabled-deck case.
        controller_closed.deck.is_open = lambda: False
        assert gl.deck_manager.deck_controller[0] is controller_closed

        # An auto-change page that matches the foreground window and targets
        # the open deck alone.
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

        # The no-match branch must also not abort on the closed deck. A second
        # event that matches nothing must leave the open deck alone, with its
        # active page on stay-on-page, and must not raise.
        grabber.on_active_window_changed(Window(wm_class="kitty", title="terminal"))
        assert controller_open.active_page.json_path == target_path, (
            "stay-on-page was not honored after a non-matching window change"
        )
        print("PASS: non-matching window change handled with a closed deck present")

        # Per-deck exception isolation. A deck that passes the loop-top
        # is_open() check but raises mid-body, in the narrow race where close()
        # flips is_open() after the check, must not abort auto-switching for
        # the decks after it, and must not let the exception escape.
        controller_boom = fixtures.make_headless_controller(serial="boom-3")
        try:
            def boom():
                raise RuntimeError("injected: deck torn down mid-switch")
            controller_boom.serial_number = boom

            # Order the raising deck before the open one.
            dc_list = gl.deck_manager.deck_controller
            dc_list.remove(controller_open)
            dc_list.append(controller_open)
            assert dc_list.index(controller_boom) < dc_list.index(controller_open)

            mail_path = fixtures.seed_page("MailPage")
            gl.page_manager.set_auto_change_settings(
                mail_path, enable=True, wm_class="thunderbird", regex_title=".*",
                stay_on_page=True, decks=[controller_open.serial_number()],
            )

            grabber.on_active_window_changed(Window(wm_class="thunderbird", title="Inbox"))

            assert controller_open.active_page.json_path == mail_path, (
                "a deck raising mid-switch prevented the next deck from "
                "auto-switching (missing per-deck exception isolation)"
            )
            print("PASS: one deck raising mid-switch doesn't block the others")
        finally:
            fixtures.teardown(controller_boom)
    finally:
        fixtures.teardown(controller_open)
        fixtures.teardown(controller_closed)

    print("PASS: scenario_autoswitch_closed_deck")


if __name__ == "__main__":
    main()
