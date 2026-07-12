"""
Integration scenario (docs/presenter-migration-plan.md §7, M1): as of M1,
DeckController.clear() is a seq-stamped ClearMsg submitted to the media
thread's control queue (plan §2.1) rather than a direct synchronous write --
so unlike pre-M1, the write no longer lands before clear() returns. This
checks the write-ordering contract of the blanking itself (every key, then
the touchscreen, nothing else interleaved) using wait_until instead of
asserting immediately after the call returns.

The terminal clear+close contract exercised via DeckManager.close_all()
(ClearAndCloseMsg, bounded join, thread actually exiting) has its own
dedicated scenario: scenario_shutdown_clearclose.py.
"""
import time

import fixtures


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_shutdown")
    controller = fixtures.make_headless_controller(serial="shutdown-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()
    is_touch = controller.deck.is_touch()

    # Let the initial page settle so the clear sequence isn't competing with
    # the boot paint.
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)

    deck.clear_journal()

    controller.clear()  # async as of M1: submits a seq-stamped ClearMsg

    expected_clear_ops = key_count + (1 if is_touch else 0)
    ok = fixtures.wait_until(lambda: len(deck.journal()) >= expected_clear_ops, timeout=3)
    assert ok, f"clear() writes never landed: {deck.journal()}"
    time.sleep(0.1)  # nothing else should follow; give stragglers a chance to (wrongly) land

    journal = deck.journal()
    assert len(journal) == expected_clear_ops, (
        f"expected exactly {expected_clear_ops} blank writes, got {len(journal)}: {journal}"
    )

    for k in range(key_count):
        assert journal[k][2] == "set_key_image" and journal[k][3] == f"key:{k}", (
            f"expected key {k}'s clear write at position {k}, got {journal[k]}"
        )
    if is_touch:
        assert journal[key_count][2] == "set_touchscreen_image", (
            f"expected the touchscreen clear write last among the clear ops, got {journal[key_count]}"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_shutdown")


if __name__ == "__main__":
    main()
