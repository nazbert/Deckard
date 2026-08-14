"""Integration scenario that drives close_all_controllers() directly.

With two controllers the two-phase ordering matters. Every open controller
ends with its blank writes and one close(), and both media threads exit.
"""
import time

import fixtures

from src.backend.DeckManagement.DeckManager import close_all_controllers


def _settle_and_clear(controller, deck) -> None:
    """Wait for the boot paint, then clear the journal.

    Only the shutdown sequence is then measured. The short sleep lets a
    trailing brightness write land before the clear.
    """
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)
    deck.clear_journal()


def _assert_clean_close(deck, key_count: int, is_touch: bool) -> None:
    """Check the guarantee close_all_controllers() makes per open controller.

    A full set of blank writes lands, then exactly one close(), and nothing
    writes after close(). Asserted on the journal suffix, so a stray trailing
    boot write cannot make it brittle.
    """
    journal = deck.journal()
    expected_clear_ops = key_count + (1 if is_touch else 0)

    closes = [e for e in journal if e[2] == "close"]
    assert len(closes) == 1, f"exactly one close() per deck, got {len(closes)}: {journal}"
    assert journal[-1][2] == "close", "nothing may land after close()"

    # The blank writes are the expected_clear_ops entries immediately before the
    # close.
    blanks = journal[-(expected_clear_ops + 1):-1]
    assert len(blanks) == expected_clear_ops, (
        f"expected {expected_clear_ops} blank writes before close, got {blanks} in {journal}"
    )
    for k in range(key_count):
        assert blanks[k][2] == "set_key_image" and blanks[k][3] == f"key:{k}", (
            f"expected key {k}'s blank write at position {k} of the blanks, got {blanks[k]}"
        )
    if is_touch:
        assert blanks[key_count][2] == "set_touchscreen_image", (
            f"expected the touchscreen blank write last among the blanks, got {blanks[key_count]}"
        )


def test_close_all_two_controllers() -> None:
    c1 = fixtures.make_headless_controller(serial="close-all-1")
    c2 = fixtures.make_headless_controller(serial="close-all-2")
    d1, d2 = fixtures.raw_deck(c1), fixtures.raw_deck(c2)

    # Let both boot paints settle, then clear the journals, so only the shutdown
    # sequence is measured.
    _settle_and_clear(c1, d1)
    _settle_and_clear(c2, d2)

    kc1, kc2 = c1.deck.key_count(), c2.deck.key_count()
    t1, t2 = c1.deck.is_touch(), c2.deck.is_touch()

    # Drive the real free function, not gl.deck_manager.close_all.
    close_all_controllers([c1, c2])

    assert fixtures.wait_until(
        lambda: not c1.media_player.is_alive() and not c2.media_player.is_alive(),
        timeout=3.0,
    ), "both media threads must exit within the bounded join"

    _assert_clean_close(d1, kc1, t1)
    _assert_clean_close(d2, kc2, t2)
    print("PASS: close_all_controllers() clears+closes every controller and joins every writer")

    # Stop the non-daemon tick threads, so the process can exit.
    fixtures.teardown(c1)
    fixtures.teardown(c2)


def test_controller_without_media_closes() -> None:
    """A controller that failed mid-construction has no media_player.

    close_all_controllers must close its deck directly instead of submitting a
    control message to a thread that does not exist.
    """
    c = fixtures.make_headless_controller(serial="close-all-3")
    d = fixtures.raw_deck(c)
    _settle_and_clear(c, d)

    # Model a controller whose writer thread never came up. Stop and detach the
    # real media_player, so the None branch runs, then close directly.
    c.media_player.stop(timeout=2.0)
    fixtures.wait_until(lambda: not c.media_player.is_alive(), timeout=3.0)
    d.clear_journal()
    c.media_player = None

    close_all_controllers([c])
    closes = [e for e in d.journal() if e[2] == "close"]
    assert len(closes) == 1, (
        f"a controller with no media_player must be closed directly (one close()), got {d.journal()}"
    )
    print("PASS: close_all_controllers() closes a media_player-less controller directly")

    fixtures.teardown(c)


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_close_all_protocol")
    test_close_all_two_controllers()
    test_controller_without_media_closes()
    print("ALL PASS: scenario_close_all_protocol")


if __name__ == "__main__":
    main()
