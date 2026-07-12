"""
Integration scenario for issue #129: deleting a page that is some
controller's _screensaver_pending_page must clear the pending request.

remove_page() only handled controllers whose ACTIVE page was the deleted
one; a page stashed as screensaver-pending (load_page's deferral while the
screensaver owns the deck) was invisible to that check. The pending
reference was left dangling at a deleted file: hide() then loaded a page
whose json no longer exists, and the page's first save resurrected the
deleted file.

Uses the scenario_screensaver_page_change machinery: settle on page A,
raise the screensaver, request page B (recorded as pending), then DELETE
page B. The pending slot must clear, B's cache entry must drop, and on
dismiss the controller must stay on page A without resurrecting B's file.
"""
import os
import time

import fixtures
import globals as gl

WATCHDOG_SECONDS = 30


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_pending_page_delete")

    controller = fixtures.make_headless_controller(serial="ppd-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    a_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "ppd_a.png"), color=(10, 200, 10))
    b_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "ppd_b.png"), color=(200, 10, 10))
    ss_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "ppd_ss.png"), color=(10, 10, 200))

    a_path = fixtures.seed_page_with_background_and_screensaver(
        "PPD_A", a_png, ss_png, screensaver_time_delay=60)
    b_path = fixtures.seed_page_with_background("PPD_B", b_png)
    page_a = gl.page_manager.get_page(a_path, controller)
    page_b = gl.page_manager.get_page(b_path, controller)

    try:
        # Settle on page A, raise the screensaver, request B while it shows.
        controller.load_page(page_a, allow_reload=True)
        fixtures.wait_until(lambda: all(
            deck.last_op_for(f"key:{k}") is not None for k in range(key_count)), timeout=5)

        controller.screen_saver.show()
        ok = fixtures.wait_until(lambda: controller.screen_saver.showing, timeout=5)
        assert ok, "screensaver never showed"

        controller.load_page(page_b, allow_reload=True)
        assert controller._screensaver_pending_page is page_b, (
            "fixture: the page change during the screensaver must be pending"
        )

        # --- Delete the PENDING page. ---
        gl.page_manager.remove_page(b_path)

        assert not os.path.exists(b_path), "remove_page must delete the file"
        assert controller._screensaver_pending_page is None, (
            "deleting a controller's pending page left the request dangling "
            "at a deleted file (issue #129) -- hide() would load a page whose "
            "json is gone and the first save would resurrect it"
        )
        cached = gl.page_manager.pages.get(controller, {})
        assert b_path not in cached, (
            "the deleted pending page's cache entry must be dropped"
        )

        # --- Dismiss: the controller stays on page A, B is not resurrected.
        controller.screen_saver.hide()
        ok = fixtures.wait_until(lambda: not controller.screen_saver.showing, timeout=5)
        assert ok, "screensaver never hid"
        time.sleep(0.3)

        assert controller.active_page is page_a, (
            f"after deleting the pending page, dismiss must keep the current "
            f"page (active={controller.active_page})"
        )
        assert not os.path.exists(b_path), (
            "the deleted page's json was resurrected after the screensaver "
            "dismissed (a dangling pending page saved itself back to disk)"
        )
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_pending_page_delete")


if __name__ == "__main__":
    main()
