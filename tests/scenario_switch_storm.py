"""
Integration scenario (docs/presenter-migration-plan.md §7 "Switch storm
x200"): 200 alternating load_page() calls between two visually distinct
pages must settle with every key painted for the FINAL page and no
cross-page frame surviving as the last thing written to any key.

Two pages, each overwriting the deck background with a distinct solid-color
PNG, make cross-page bleed detectable by comparing per-key write hashes
rather than needing device pixel readback.
"""
import os
import time

import fixtures
import globals as gl


def _paint_signature(controller, deck, page, key_count: int) -> dict:
    """Loads `page` alone, waits for every key to repaint, and returns
    {key_index: last_write_hash} -- this page's distinguishing signature."""
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

    # Reset to a neutral (background-less) page before storming. Without
    # this, the storm's very last write could be a no-op: the media thread's
    # dedup guard (_last_enqueued_hash/_last_img_hash, DeckController.py
    # ~2444-2449) correctly skips re-painting a key with the image it
    # already shows, and the signature-learning pass above already painted
    # page B's exact content once. That's real, correct product behavior --
    # not something to defeat by re-checking with a fresh deck, since the
    # scenario should still observe genuine device writes for the storm
    # itself.
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
    # A further quiescence window: if a straggler were going to land late, it
    # would show up here as a change to the last-write hash.
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
