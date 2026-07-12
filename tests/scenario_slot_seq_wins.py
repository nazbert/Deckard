"""
Scenario: single-slot assignment must be highest-seq-wins (issue #130).

add_touchscreen_task/add_image_task used to allocate next_submit_seq() and
construct the task OUTSIDE _slot_lock; only the slot assignment was locked.
Two concurrent producers -- the media-tick ControllerTouchScreen.update()
and a GTK/action-thread dial update() funnelling into the same strip (or two
writers of the same key slot) -- could therefore allocate seqs in one order
and reach the locked assignment in the opposite order: the single slot ended
up holding the LOWER-seq (older) frame and the newer frame was lost
(one-frame staleness for animated content; found in the !36 round-1 review,
out of #8's drain/clear scope).

The fix stamps the seq INSIDE _slot_lock, atomically with the assignment,
so seq order IS assignment order and the slot always ends with the maximum
allocated seq.

Detection is DETERMINISTIC (no reliance on the scheduler happening to hit a
narrow window). next_submit_seq is wrapped so that, after allocating a seq,
the producer sleeps for a duration that is LONGER the EARLIER its seq was in
the round: the lowest-seq producer sleeps the most, the highest the least.

  * PRE-FIX: that sleep sits in the allocate->assign window, OUTSIDE
    _slot_lock. So the highest-seq producer (shortest sleep) assigns first and
    the lowest-seq producer (longest sleep) assigns LAST, overwriting the slot
    with the OLDEST frame. Every round inverts (measured 200/200).
  * FIXED: the sleep runs UNDER _slot_lock (allocation is inside the lock), so
    producers assign in strict seq order regardless of sleep -- the slot always
    ends holding the max seq. Every round holds (measured 0/60 inversions).

We run several rounds of N_THREADS concurrent submissions and fail the first
round whose slot does not end holding that round's maximum allocated seq. The
inversion is structural on the pre-fix code, so a single round would already
catch it; the extra rounds are pure margin against scheduling variance.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from fixtures import start_watchdog

N_THREADS = 6
ROUNDS = 12
# Base unit for the seq-ordered sleep. The earliest producer of a round sleeps
# (N_THREADS-1)*STEP, the latest sleeps 0 -- enough separation to force the
# assign order deterministically without dragging the suite.
STEP = 0.001
WATCHDOG_SECONDS = 60


def _run_rounds(media_player, submit_fn, read_slot_seq, label: str) -> int:
    """Run ROUNDS rounds of N_THREADS concurrent single submissions. Each round
    installs a fresh recorder that captures that round's allocated seqs and
    sleeps AFTER allocation for a time that decreases with the seq's position
    in the round (earliest seq sleeps longest). Fails the first round whose
    slot does not end holding that round's max seq.

      submit_fn(thread_index) -- enqueues one frame via the add_* under test.
      read_slot_seq()         -- current slot's submit_seq (None if empty).
    """
    base_next = media_player.next_submit_seq

    for rnd in range(ROUNDS):
        recorded: list[int] = []
        rec_lock = threading.Lock()
        round_base: list[int] = []  # first seq allocated this round

        def recording_next_submit_seq():
            seq = base_next()
            with rec_lock:
                recorded.append(seq)
                if not round_base:
                    round_base.append(seq)
            position = seq - round_base[0]  # 0 for the earliest producer
            # Earliest seq sleeps longest, so on pre-fix code (sleep outside
            # _slot_lock) it assigns LAST and overwrites the slot with the
            # oldest frame. On fixed code this sleep is under the lock and
            # cannot change assignment order.
            time.sleep((N_THREADS - 1 - position) * STEP)
            return seq

        media_player.next_submit_seq = recording_next_submit_seq
        try:
            threads = [threading.Thread(target=submit_fn, args=(t,),
                                        daemon=True)
                       for t in range(N_THREADS)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=15)
            assert all(not th.is_alive() for th in threads), "producer hung"
        finally:
            media_player.next_submit_seq = base_next

        slot_seq = read_slot_seq()
        assert slot_seq is not None, f"{label} slot empty after round {rnd}"
        assert len(recorded) == N_THREADS, (
            f"{label}: expected {N_THREADS} allocations, got {len(recorded)}")
        if slot_seq != max(recorded):
            print(f"FAIL({label}): round {rnd} slot holds seq {slot_seq}, but "
                  f"seq {max(recorded)} was allocated this round -- an older "
                  f"frame overwrote a newer one (last-assigner-wins, #130)")
            return 1
    return 0


def check_touchscreen_slot() -> int:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="seqwins-ts", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]

    def submit(thread_index):
        media_player.add_touchscreen_task(
            bytes([thread_index]) * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            controller_touchscreen=touch,
            img_hash=(thread_index,),
        )

    def read_slot():
        t = media_player.touchscreen_task
        return t.submit_seq if t is not None else None

    rc = _run_rounds(media_player, submit, read_slot, "ts")
    if rc == 0:
        print("PASS: touchscreen slot ends every round with the highest seq")
    return rc


def check_key_slot() -> int:
    controller, media_player, _ = fixtures.make_stub_controller(
        serial="seqwins-key", n_keys=1
    )

    def submit(thread_index):
        media_player.add_image_task(
            key_index=0,
            native_image=bytes([thread_index]) * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            img_hash=(thread_index,),
        )

    def read_slot():
        t = media_player.image_tasks.get(0)
        return t.submit_seq if t is not None else None

    rc = _run_rounds(media_player, submit, read_slot, "key")
    if rc == 0:
        print("PASS: key slot ends every round with the highest seq")
    return rc


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_slot_seq_wins")
    failures = check_touchscreen_slot() + check_key_slot()
    assert failures == 0, f"{failures} slot-seq check(s) failed (issue #130)"
    print("PASS: scenario_slot_seq_wins")


if __name__ == "__main__":
    main()
