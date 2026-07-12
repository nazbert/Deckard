"""
Unit-tier scenario (docs/presenter-migration-plan.md §7 "Suspend/resume
repaint" + §4 M2): the media loop detects a wall-clock gap >=5s between
iterations -- the signature of a process suspend/resume cycle
(DetectResumeThread's proven technique, relocated into
MediaPlayerThread.check_resume_gap now that DetectResumeThread itself is
deleted, plan §9.1) -- and arms a pending full repaint: dedup hashes
nulled, every input's update() re-enqueued, fired by the loop on a 2s
cadence.

The crucial property (coordinator review of M2): a repaint whose writes
FAIL -- because the library's read thread is still reopening the handle --
must be re-armed and retried, or a fully static page (which generates no
further writes and therefore no failure->success edge) stays stale
forever. Failure itself re-arms the pending flag; the 2s cadence spaces
the retries.

Drives check_resume_gap()/_run_pending_repaint() directly (the unit-tier
seam -- mirrors drain_control_queue's rationale) instead of spinning
run(), so the scenario is deterministic.
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

    # --- (a) a >=5s gap arms a pending repaint; the loop hook fires it. ---
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

    # --- (b) rate limiting defers (never drops): gaps inside the 2s window
    # keep the flag armed; the repaint fires once the window opens. ---
    media_player._last_iter_ts = time.time() - 10.0
    media_player.check_resume_gap()
    assert not controller._run_pending_repaint(), "inside the 2s window the repaint must be deferred"
    assert controller._full_repaint_pending, "a deferred repaint must stay armed, not be dropped"
    assert controller.repaint_count == 1

    controller._last_full_repaint_ts = time.time() - 3.0  # open the rate window
    assert controller._run_pending_repaint(), "the deferred repaint must fire once the window opens"
    assert controller.repaint_count == 2

    # --- (c) the static-page recovery property: a repaint whose writes all
    # FAIL (handle not yet reopened) re-arms itself and retries until its
    # writes land. ---
    media_player.perform_media_player_tasks()   # drain (b)'s tasks so (c) starts clean
    deck.clear_journal()
    seed_hashes(controller)

    media_player._last_iter_ts = time.time() - 10.0
    media_player.check_resume_gap()
    controller._last_full_repaint_ts = 0.0      # window open
    assert controller._run_pending_repaint()
    deck.fail_next("set_", count=99)            # handle still closed: everything fails
    media_player.perform_media_player_tasks()   # repaint burst -> all writes raise
    assert controller._full_repaint_pending, (
        "a repaint whose writes failed must re-arm itself (static pages have "
        "no other recovery trigger)"
    )

    deck.clear_failures()                       # handle reopened: writes succeed again
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
