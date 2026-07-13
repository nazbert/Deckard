"""
Unit-tier scenario (docs/presenter-migration-plan.md §7 "Clear-vs-frames
ordering", M1): DeckController.clear() is now a seq-stamped ClearMsg
submitted to the media thread's control queue (plan §2.1) instead of a
direct synchronous write. This drives that queue directly (MediaPlayerThread
.submit_control/.drain_control_queue/.next_submit_seq -- the unit-tier seam,
tests/fixtures.py's StubDeckController is never started as a real thread) to
check the exact predicate:

  * a frame submitted (and drained) BEFORE a Clear was even requested may
    land before the blank -- that's just an earlier tick, not a violation;
  * the Clear's blank write always lands;
  * a frame submitted AFTER the Clear (same submit_seq counter, higher
    value) is never wiped by it, and paints after the blank;
  * a Clear submitted BEFORE any content at all (the screensaver-entry
    pattern -- clear-then-paint) must never leave the deck sitting on a
    blank once the content that follows actually lands.

Write latency is injected so the Clear and the surrounding submits can be
interleaved without a real background thread; the ordering assertions are on
the journal's seq numbers, not wall-clock timing.
"""
import fixtures
from src.backend.DeckManagement.DeckController import ClearMsg


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_clear_ordering")
    controller, media_player, deck_manager = fixtures.make_stub_controller()
    deck = controller.deck
    page = controller.active_page
    gen = controller._page_load_generation

    deck.set_write_latency(0.01)

    # --- Part 1: content, then Clear, then more content (same gen). ---
    pre_clear_img = fixtures.make_native_image(fill=1)
    media_player.add_image_task(0, pre_clear_img, page=page, config_gen=gen)
    media_player.perform_media_player_tasks()  # lands on an earlier "tick"

    pre_clear_landed = deck.last_op_for("key:0")
    assert pre_clear_landed is not None and pre_clear_landed[2] == "set_key_image", (
        "fixture sanity: the pre-clear frame must land before the Clear exists"
    )

    # Capture the seq the way DeckController.clear() does, then queue more
    # content for the same page/gen before draining the Clear.
    seq = media_player.next_submit_seq()
    media_player.submit_control(ClearMsg(seq=seq))
    post_clear_img = fixtures.make_native_image(fill=2)
    media_player.add_image_task(0, post_clear_img, page=page, config_gen=gen)

    still_running = media_player.drain_control_queue()
    assert still_running, "ClearMsg must not be terminal"

    blank_entry = deck.last_op_for("key:0")
    assert blank_entry is not None
    assert blank_entry[1] > pre_clear_landed[1], (
        "the blank must land (seq-after) the pre-clear frame -- pre-clear "
        "frames MAY precede the blank, never the reverse"
    )

    # The post-clear frame must NOT have been wiped -- it should still be
    # queued and paint on the next media cycle.
    assert 0 in media_player.image_tasks, "post-clear frame was wiped by the Clear"

    media_player.perform_media_player_tasks()

    final_entry = deck.last_op_for("key:0")
    assert final_entry is not None and final_entry[1] > blank_entry[1], (
        "the post-clear frame must land AFTER the blank"
    )
    assert final_entry[4] != blank_entry[4], (
        "the post-clear frame must actually repaint different content, not stay blank"
    )

    # --- Part 2: Clear submitted BEFORE any content exists at all
    # (screensaver-entry pattern: caller does clear-then-paint). Content
    # queued after must survive and the deck's final observed state for the
    # key must be the content, never stuck on blank. ---
    deck.clear_journal()

    seq2 = media_player.next_submit_seq()
    media_player.submit_control(ClearMsg(seq=seq2))
    content_img = fixtures.make_native_image(fill=3)
    media_player.add_image_task(0, content_img, page=page, config_gen=gen)

    still_running = media_player.drain_control_queue()
    assert still_running
    media_player.perform_media_player_tasks()

    key0_ops = [e for e in deck.journal() if e[3] == "key:0"]
    assert len(key0_ops) == 2, (
        f"expected exactly [blank, content] for key:0, got {key0_ops}"
    )
    assert key0_ops[0][4] != key0_ops[1][4], (
        "the content submitted after a pre-content Clear must not be wiped -- "
        "the deck must not be left permanently blank"
    )

    print("PASS: scenario_clear_ordering")


if __name__ == "__main__":
    main()
