"""
Integration scenario (#69): exercises the REAL close_all_controllers() free
function in DeckManager.py directly, over multiple FaultyFakeDeck controllers.

Before this, the M1 terminal-close protocol (submit ClearAndClose to every
open controller FIRST, then join each media thread with a bound) was only
ever run through StubDeckManager.close_all, which re-implemented it -- so no
scenario executed the production code. close_all_controllers() is now the
single implementation both DeckManager.close_all and the stub call; this
drives it straight.

Checks, with TWO controllers so the two-phase ordering actually matters:
  * every open controller's journal ends with its blank writes followed by a
    single close(), nothing landing after;
  * BOTH media threads exit within the bounded join;
  * a controller with no media_player thread is closed best-effort/directly.

(A controller whose deck is *already closed* being skipped is not covered
here: FakeDeck.is_open() is hardcoded True, so the fake can't yet represent a
closed device -- that's issue #59.)
"""
import time

import fixtures
import globals as gl

from src.backend.DeckManagement.DeckManager import close_all_controllers


def _settle_and_clear(controller, deck) -> None:
    """Wait for the boot paint to finish, then clear the journal so only the
    shutdown sequence is measured. The extra short sleep lets any brightness/
    trailing write that follows the last key paint land before we clear."""
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)
    deck.clear_journal()


def _assert_clean_close(deck, key_count: int, is_touch: bool) -> None:
    """The protocol close_all_controllers() guarantees per open controller:
    a full set of blank writes (every key, plus the touchscreen if present)
    lands, followed by exactly one close(), and nothing writes after close().
    Asserted on the journal SUFFIX (blanks-then-close) rather than an exact
    length so a stray trailing boot write can't make it brittle."""
    journal = deck.journal()
    expected_clear_ops = key_count + (1 if is_touch else 0)

    closes = [e for e in journal if e[2] == "close"]
    assert len(closes) == 1, f"exactly one close() per deck, got {len(closes)}: {journal}"
    assert journal[-1][2] == "close", "nothing may land after close()"

    # The blank writes are the `expected_clear_ops` entries immediately before
    # the close.
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

    # Let both boot paints settle, then clear the journals so we measure only
    # the shutdown sequence.
    _settle_and_clear(c1, d1)
    _settle_and_clear(c2, d2)

    kc1, kc2 = c1.deck.key_count(), c2.deck.key_count()
    t1, t2 = c1.deck.is_touch(), c2.deck.is_touch()

    # Drive the REAL free function directly (not via gl.deck_manager.close_all).
    close_all_controllers([c1, c2])

    assert fixtures.wait_until(
        lambda: not c1.media_player.is_alive() and not c2.media_player.is_alive(),
        timeout=3.0,
    ), "both media threads must exit within the bounded join"

    _assert_clean_close(d1, kc1, t1)
    _assert_clean_close(d2, kc2, t2)
    print("PASS: close_all_controllers() clears+closes every controller and joins every writer")

    # Stop the (non-daemon) tick threads so the process can exit.
    fixtures.teardown(c1)
    fixtures.teardown(c2)


def test_controller_without_media_player_closes_directly() -> None:
    """The media_player-is-None branch (controller that failed mid-
    construction): close_all_controllers must close its deck directly rather
    than submit a control message to a thread that doesn't exist."""
    c = fixtures.make_headless_controller(serial="close-all-3")
    d = fixtures.raw_deck(c)
    _settle_and_clear(c, d)

    # Simulate a controller whose writer thread never came up: stop and detach
    # the real media_player so the None branch is taken, then close directly.
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
    test_controller_without_media_player_closes_directly()
    print("ALL PASS: scenario_close_all_protocol")


if __name__ == "__main__":
    main()
