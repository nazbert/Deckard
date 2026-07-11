"""
Integration scenario: a page change requested WHILE the screensaver is showing
must be deferred, not painted.

Bug: load_page() had no screensaver awareness, so switching pages while the
screensaver was up loaded the new page onto the deck's (screensaver-swapped)
inputs and painted it -- the new page's icons leaked onto the DEVICE (replacing
the screensaver) and into the app previews. The fix records the requested page
as PENDING (without touching active_page -- the media player gates the
screensaver's background-video animation on `background.video.page is
active_page`, so changing active_page would freeze the screensaver video) and
returns; the screensaver keeps showing, and hide() loads the pending page when
the screensaver is dismissed.

Distinct per-page backgrounds make "did page B land on the deck?" detectable by
comparing per-key write hashes, the same technique as scenario_switch_storm /
scenario_screensaver_bg_race.
"""
import os
import time

import fixtures
import globals as gl

WATCHDOG_SECONDS = 30


def _signature(controller, deck, page, key_count):
    deck.clear_journal()
    controller.load_page(page, allow_reload=True)
    ok = fixtures.wait_until(
        lambda: all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count)),
        timeout=5,
    )
    assert ok, f"page {page.get_name()} never painted all keys"
    time.sleep(0.1)
    return {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_screensaver_page_change")

    controller = fixtures.make_headless_controller(serial="sspc-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    a_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "pc_a.png"), color=(10, 200, 10))
    b_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "pc_b.png"), color=(200, 10, 10))
    ss_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "pc_ss.png"), color=(10, 10, 200))

    # Page A persists the screensaver settings (load_page reloads them from the
    # page every time -- see fixtures.seed_page_with_background_and_screensaver).
    a_path = fixtures.seed_page_with_background_and_screensaver("PC_A", a_png, ss_png, screensaver_time_delay=60)
    b_path = fixtures.seed_page_with_background("PC_B", b_png)
    page_a = gl.page_manager.get_page(a_path, controller)
    page_b = gl.page_manager.get_page(b_path, controller)

    # Learn page B's paint signature in isolation so a leak onto the deck is
    # detectable by hash.
    sig_b = _signature(controller, deck, page_b, key_count)

    # Settle on page A, then raise the screensaver.
    controller.load_page(page_a, allow_reload=True)
    fixtures.wait_until(lambda: all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count)), timeout=5)

    controller.screen_saver.show()
    ok = fixtures.wait_until(lambda: controller.screen_saver.showing and all(
        deck.last_op_for(f"key:{k}") is not None for k in range(key_count)), timeout=5)
    assert ok, "screensaver never showed"
    time.sleep(0.1)
    sig_ss = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}
    for k in range(key_count):
        assert sig_ss[k] != sig_b[k], f"fixture: screensaver and page B produced the same hash for key {k}"

    # --- The change: switch to page B WHILE the screensaver is showing. ---
    deck.clear_journal()
    controller.load_page(page_b, allow_reload=True)

    # It must be recorded as PENDING but NOT painted, and active_page must stay
    # on the screensaver's page: the media player gates the screensaver's
    # background-video animation on `background.video.page is active_page`
    # (MediaPlayerThread.run), so changing active_page here would freeze the
    # screensaver video (it would resume only on switching back). hide() loads
    # the pending page on dismiss.
    assert controller.active_page is page_a, (
        "a page change during the screensaver must NOT change active_page (that "
        "freezes the screensaver's background video)"
    )
    assert getattr(controller, "_screensaver_pending_page", None) is page_b, (
        "the requested page must be recorded as pending for hide() to load"
    )
    assert controller.screen_saver.showing, (
        "the screensaver must keep showing after a page change (not be dismissed)"
    )

    # Give any (wrongful) paint a chance to land, then assert page B did NOT
    # reach the deck -- the screensaver still owns every key.
    time.sleep(0.4)
    for k in range(key_count):
        last = deck.last_op_for(f"key:{k}")
        if last is not None:
            assert last[4] != sig_b[k], (
                f"key {k}: page B leaked onto the DEVICE while the screensaver was "
                f"showing (got page-B content) -- the screensaver was overwritten"
            )

    # --- On dismiss, the recorded page B must load. ---
    controller.screen_saver.hide()
    ok = fixtures.wait_until(lambda: not controller.screen_saver.showing and all(
        deck.last_op_for(f"key:{k}") is not None for k in range(key_count)), timeout=5)
    assert ok, "screensaver never hid"
    time.sleep(0.1)
    for k in range(key_count):
        assert deck.last_op_for(f"key:{k}")[4] == sig_b[k], (
            f"key {k}: page B (the page selected during the screensaver) did not "
            f"load when the screensaver was dismissed"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_screensaver_page_change")


if __name__ == "__main__":
    main()
