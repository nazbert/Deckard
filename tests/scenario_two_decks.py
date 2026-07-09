"""
Integration scenario (docs/presenter-migration-plan.md §7 "Two fake decks,
storm both"): two independent headless controllers, each over its own
FaultyFakeDeck, switched concurrently from separate threads. Each deck's
journal must reflect only its own controller's pages -- no shared state
(sequence counters, journals, dedup) leaking across controller instances.
"""
import os
import threading
import time

import fixtures
import globals as gl


def _settle_on(controller, deck, page, key_count):
    """Loads `page` alone, waits for every key to repaint, returns the
    per-key hash signature (see scenario_switch_storm's helper of the same
    shape)."""
    deck.clear_journal()
    controller.load_page(page, allow_reload=True)

    def settled():
        return all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count))

    ok = fixtures.wait_until(settled, timeout=5)
    assert ok, f"page {page.get_name()} did not paint all {key_count} keys"
    time.sleep(0.1)
    return {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}


def _storm(controller, pages, n: int) -> None:
    for i in range(n):
        controller.load_page(pages[i % 2], allow_reload=True)


def main() -> None:
    controller1 = fixtures.make_headless_controller(serial="two-decks-1")
    controller2 = fixtures.make_headless_controller(serial="two-decks-2")
    deck1 = fixtures.raw_deck(controller1)
    deck2 = fixtures.raw_deck(controller2)
    key_count1 = controller1.deck.key_count()
    key_count2 = controller2.deck.key_count()

    # Distinct color sets per controller so a cross-controller bleed would
    # be detectable even if content happened to collide within one deck.
    red = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "d1_red.png"), color=(255, 0, 0))
    green = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "d1_green.png"), color=(0, 255, 0))
    blue = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "d2_blue.png"), color=(0, 0, 255))
    yellow = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "d2_yellow.png"), color=(255, 255, 0))

    d1_page_a = gl.page_manager.get_page(fixtures.seed_page_with_background("D1PageA", red), controller1)
    d1_page_b = gl.page_manager.get_page(fixtures.seed_page_with_background("D1PageB", green), controller1)
    d2_page_a = gl.page_manager.get_page(fixtures.seed_page_with_background("D2PageA", blue), controller2)
    d2_page_b = gl.page_manager.get_page(fixtures.seed_page_with_background("D2PageB", yellow), controller2)

    sig1_a = _settle_on(controller1, deck1, d1_page_a, key_count1)
    sig1_b = _settle_on(controller1, deck1, d1_page_b, key_count1)
    sig2_a = _settle_on(controller2, deck2, d2_page_a, key_count2)
    sig2_b = _settle_on(controller2, deck2, d2_page_b, key_count2)

    # Cross-controller signatures must never collide (sanity: proves the two
    # decks are genuinely independent fixtures, not sharing a journal).
    for k in range(min(key_count1, key_count2)):
        others = {sig2_a[k], sig2_b[k]}
        assert sig1_a[k] not in others and sig1_b[k] not in others, (
            f"key {k}: controller1 and controller2 produced overlapping hashes"
        )

    # Reset each to a neutral page before storming (see scenario_switch_storm
    # for why: the dedup guard would otherwise no-op a repeat of already-
    # displayed content).
    d1_neutral = gl.page_manager.get_page(fixtures.seed_page("D1Neutral"), controller1)
    d2_neutral = gl.page_manager.get_page(fixtures.seed_page("D2Neutral"), controller2)
    _settle_on(controller1, deck1, d1_neutral, key_count1)
    _settle_on(controller2, deck2, d2_neutral, key_count2)

    deck1.clear_journal()
    deck2.clear_journal()

    N = 150
    t1 = threading.Thread(target=_storm, args=(controller1, [d1_page_a, d1_page_b], N), name="storm-1")
    t2 = threading.Thread(target=_storm, args=(controller2, [d2_page_a, d2_page_b], N), name="storm-2")
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "storm threads did not finish in time"

    final1 = sig1_a if (N - 1) % 2 == 0 else sig1_b
    final2 = sig2_a if (N - 1) % 2 == 0 else sig2_b

    def settled1():
        return all(deck1.last_op_for(f"key:{k}") is not None for k in range(key_count1))

    def settled2():
        return all(deck2.last_op_for(f"key:{k}") is not None for k in range(key_count2))

    assert fixtures.wait_until(settled1, timeout=15), "controller1 did not settle"
    assert fixtures.wait_until(settled2, timeout=15), "controller2 did not settle"
    time.sleep(0.5)

    for k in range(key_count1):
        last = deck1.last_op_for(f"key:{k}")
        assert last[4] == final1[k], f"deck1 key {k}: expected final content, got {last}"
        assert last[4] not in (sig2_a[k], sig2_b[k]) if k < key_count2 else True, (
            f"deck1 key {k}: journal shows deck2's content -- cross-deck bleed"
        )

    for k in range(key_count2):
        last = deck2.last_op_for(f"key:{k}")
        assert last[4] == final2[k], f"deck2 key {k}: expected final content, got {last}"
        assert last[4] not in (sig1_a[k], sig1_b[k]) if k < key_count1 else True, (
            f"deck2 key {k}: journal shows deck1's content -- cross-deck bleed"
        )

    # Journals are independent objects with independent sequence counters.
    assert deck1 is not deck2
    assert deck1.journal() is not deck2.journal()

    fixtures.teardown(controller1)
    fixtures.teardown(controller2)
    print("PASS: scenario_two_decks")


if __name__ == "__main__":
    main()
