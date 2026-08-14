"""
Unit-tier scenario for the suspend and resume repaint.

MediaPlayerThread.check_resume_gap detects a wall-clock gap of 5s or more
between iterations and arms a pending full repaint, which nulls the dedup
hashes and re-enqueues every input. A repaint whose writes fail re-arms
itself, because a static page produces no other recovery trigger.
"""
import time

import fixtures
from src.backend.DeckManagement.InputIdentifier import Input


def seed_hashes(controller, value=123):
    for i in controller.inputs[Input.Key] + controller.inputs[Input.Touchscreen]:
        i._last_img_hash = value
        i._last_enqueued_hash = value


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_resume_repaint")
    controller, media_player, deck_manager = fixtures.make_stub_controller(n_keys=2, has_touchscreen=True)
    deck = controller.deck

    # Seed a non-None dedup hash on every input, the way a real paint would,
    # so the reset is observable.
    seed_hashes(controller)

    # A gap of 5s or more arms a pending repaint, and the loop hook fires it.
    media_player._last_iter_ts = time.time() - 10.0
    gap_detected = media_player.check_resume_gap()
    assert gap_detected, "a >=5s gap must be detected"
    assert controller._full_repaint_pending, "a detected gap must arm the pending repaint"
    assert controller.repaint_count == 0, "arming must not fire the repaint synchronously"

    fired = controller._run_pending_repaint()
    assert fired and controller.repaint_count == 1, "the loop hook must fire the armed repaint"

    for i in controller.inputs[Input.Key] + controller.inputs[Input.Touchscreen]:
        assert i._last_img_hash is None, "dedup hashes must be nulled by the resume repaint"
        assert i._last_enqueued_hash is None

    media_player.perform_media_player_tasks()  # flush the repaint's enqueued tasks

    written_keys = {e[3] for e in deck.ops_by_name("set_key_image")}
    assert written_keys == {"key:0", "key:1"}, (
        f"every key must be rewritten by the resume repaint, got {written_keys}"
    )
    assert len(deck.ops_by_name("set_touchscreen_image")) == 1, (
        "the touchscreen must also be rewritten by the resume repaint"
    )

    # Rate limiting defers rather than drops. A gap inside the 2s window
    # keeps the flag armed, and the repaint fires once the window opens.
    media_player._last_iter_ts = time.time() - 10.0
    media_player.check_resume_gap()
    assert not controller._run_pending_repaint(), "inside the 2s window the repaint must be deferred"
    assert controller._full_repaint_pending, "a deferred repaint must stay armed, not be dropped"
    assert controller.repaint_count == 1

    controller._last_full_repaint_ts = time.time() - 3.0  # open the rate window
    assert controller._run_pending_repaint(), "the deferred repaint must fire once the window opens"
    assert controller.repaint_count == 2

    # The static-page recovery property. A repaint whose writes all fail,
    # with the handle not yet reopened, re-arms itself and retries until its
    # writes land.
    media_player.perform_media_player_tasks()   # drain the earlier tasks first
    deck.clear_journal()
    seed_hashes(controller)

    media_player._last_iter_ts = time.time() - 10.0
    media_player.check_resume_gap()
    controller._last_full_repaint_ts = 0.0      # window open
    assert controller._run_pending_repaint()
    deck.fail_next("set_", count=99)            # handle still closed, so everything fails
    media_player.perform_media_player_tasks()   # the repaint burst makes every write raise
    assert controller._full_repaint_pending, (
        "a repaint whose writes failed must re-arm itself (static pages have "
        "no other recovery trigger)"
    )

    deck.clear_failures()                       # handle reopened, so writes succeed
    controller._last_full_repaint_ts = 0.0      # advance past the 2s cadence
    assert controller._run_pending_repaint(), "the retry must fire"
    media_player.perform_media_player_tasks()
    written_keys = {e[3] for e in deck.ops_by_name("set_key_image")}
    assert written_keys == {"key:0", "key:1"}, (
        f"the retried repaint must rewrite every key, got {written_keys}"
    )
    assert not controller._full_repaint_pending, "a clean repaint must disarm the flag"

    print("PASS: scenario_resume_repaint")


if __name__ == "__main__":
    main()
