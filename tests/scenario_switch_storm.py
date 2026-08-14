"""
Integration scenario for 200 alternating load_page calls between two pages.

The deck must settle with every key painted for the final page and no
cross-page frame left as the last write on any key. Each page carries a
distinct solid-color background, so a bleed shows up in the per-key hash.
"""
import os
import time

import fixtures
import globals as gl


def _paint_signature(controller, deck, page, key_count: int) -> dict:
    """Loads page alone, waits for every key to repaint, and returns the map
    of key index to last write hash, which is this page's signature."""
    deck.clear_journal()
    controller.load_page(page, allow_reload=True)

    def settled():
        return all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count))

    ok = fixtures.wait_until(settled, timeout=5)
    assert ok, f"page {page.get_name()} did not paint all {key_count} keys"
    time.sleep(0.1)
    return {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_switch_storm")
    controller = fixtures.make_headless_controller(serial="storm-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    red_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "red.png"), color=(255, 0, 0))
    blue_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "blue.png"), color=(0, 0, 255))

    page_a_path = fixtures.seed_page_with_background("PageA", red_png)
    page_b_path = fixtures.seed_page_with_background("PageB", blue_png)
    page_a = gl.page_manager.get_page(page_a_path, controller)
    page_b = gl.page_manager.get_page(page_b_path, controller)
    pages = [page_a, page_b]

    # Learn each page's paint signature in isolation before storming, so a
    # cross-page bleed after the storm is detectable by hash comparison.
    sig_a = _paint_signature(controller, deck, page_a, key_count)
    sig_b = _paint_signature(controller, deck, page_b, key_count)
    for k in range(key_count):
        assert sig_a[k] != sig_b[k], (
            f"key {k}: pages A/B produced the same hash ({sig_a[k]}) -- the "
            f"test fixture isn't actually distinguishing the two pages"
        )

    # Reset to a neutral, background-less page before the storm. Otherwise
    # the storm's last write can be a no-op, because the media thread's dedup
    # guard skips a key that already shows that image and the signature pass
    # above already painted page B's content once.
    neutral_path = fixtures.seed_page("Neutral")
    neutral_page = gl.page_manager.get_page(neutral_path, controller)
    _paint_signature(controller, deck, neutral_page, key_count)

    N = 200
    deck.clear_journal()
    for i in range(N):
        controller.load_page(pages[i % 2], allow_reload=True)

    final_page_is_a = (N - 1) % 2 == 0
    final_sig = sig_a if final_page_is_a else sig_b
    other_sig = sig_b if final_page_is_a else sig_a

    def settled():
        return all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count))

    ok = fixtures.wait_until(settled, timeout=15)
    assert ok, "switch storm did not settle within timeout"
    # A further quiescence window. A straggler landing late shows up here as
    # a change to the last-write hash.
    time.sleep(0.5)

    for k in range(key_count):
        last = deck.last_op_for(f"key:{k}")
        assert last is not None, f"key {k} was never painted during the storm"
        assert last[4] == final_sig[k], (
            f"key {k}: final journal state does not match the final page's "
            f"signature (got {last[4]}, expected {final_sig[k]}) -- possible "
            f"cross-page bleed"
        )
        assert last[4] != other_sig[k], (
            f"key {k}: final journal state matches the OTHER page's "
            f"signature -- cross-page frame survived the last switch"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_switch_storm")


if __name__ == "__main__":
    main()
