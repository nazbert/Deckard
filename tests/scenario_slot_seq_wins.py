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

Detection: next_submit_seq is wrapped (instance attribute shadows the
method) to record every allocated seq and then sleep a per-call jitter.
On the OLD code the jitter sits in the allocate->assign window, making the
inversion near-certain across the stress below (a thread allocates, sleeps
2ms while a neighbor allocates a higher seq and assigns immediately, then
overwrites the slot with its older frame). On the fixed code the wrapper
runs under _slot_lock, so allocation and assignment cannot be separated and
the property holds structurally. After all producers join, the slot's
submit_seq must equal max(recorded).
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import random
import threading
import time

from fixtures import start_watchdog

N_THREADS = 8
ITERATIONS = 25
WATCHDOG_SECONDS = 60


def _wrap_seq_with_jitter(media_player, recorded: list, lock: threading.Lock):
    original = media_player.next_submit_seq

    def recording_next_submit_seq():
        seq = original()
        with lock:
            recorded.append(seq)
        # On pre-fix code this sleep sits between allocation and the locked
        # assignment -- the exact producer-vs-producer window. On fixed code
        # it runs inside _slot_lock and merely serializes the stress.
        time.sleep(random.uniform(0.0, 0.002))
        return seq

    media_player.next_submit_seq = recording_next_submit_seq


def check_touchscreen_slot() -> int:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="seqwins-ts", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]

    recorded: list[int] = []
    rec_lock = threading.Lock()
    _wrap_seq_with_jitter(media_player, recorded, rec_lock)

    def producer(thread_index: int):
        for i in range(ITERATIONS):
            media_player.add_touchscreen_task(
                bytes([thread_index]) * 64,
                page=controller.active_page,
                config_gen=controller._page_load_generation,
                controller_touchscreen=touch,
                img_hash=(thread_index, i),
            )

    threads = [threading.Thread(target=producer, args=(t,), daemon=True)
               for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = media_player.touchscreen_task
    assert final is not None, "slot empty after the stress"
    assert len(recorded) == N_THREADS * ITERATIONS
    if final.submit_seq != max(recorded):
        print(f"FAIL(ts): slot holds seq {final.submit_seq}, but seq "
              f"{max(recorded)} was allocated -- an older frame overwrote a "
              f"newer one (last-assigner-wins, issue #130)")
        return 1
    print("PASS: touchscreen slot ends with the highest allocated seq")
    return 0


def check_key_slot() -> int:
    controller, media_player, _ = fixtures.make_stub_controller(
        serial="seqwins-key", n_keys=1
    )

    recorded: list[int] = []
    rec_lock = threading.Lock()
    _wrap_seq_with_jitter(media_player, recorded, rec_lock)

    def producer(thread_index: int):
        for i in range(ITERATIONS):
            media_player.add_image_task(
                key_index=0,
                native_image=bytes([thread_index]) * 64,
                page=controller.active_page,
                config_gen=controller._page_load_generation,
                img_hash=(thread_index, i),
            )

    threads = [threading.Thread(target=producer, args=(t,), daemon=True)
               for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = media_player.image_tasks.get(0)
    assert final is not None, "key slot empty after the stress"
    assert len(recorded) == N_THREADS * ITERATIONS
    if final.submit_seq != max(recorded):
        print(f"FAIL(key): slot holds seq {final.submit_seq}, but seq "
              f"{max(recorded)} was allocated -- an older frame overwrote a "
              f"newer one (last-assigner-wins, issue #130)")
        return 1
    print("PASS: key slot ends with the highest allocated seq")
    return 0


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_slot_seq_wins")
    failures = check_touchscreen_slot() + check_key_slot()
    assert failures == 0, f"{failures} slot-seq check(s) failed (issue #130)"
    print("PASS: scenario_slot_seq_wins")


if __name__ == "__main__":
    main()
