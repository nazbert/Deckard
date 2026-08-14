"""
Single-slot assignment must be highest-seq-wins.

add_touchscreen_task and add_image_task stamp the seq inside _slot_lock,
atomically with the assignment, so seq order is assignment order and the slot
always ends holding the maximum allocated seq. A seq-ordered sleep after
allocation makes an inversion deterministic rather than scheduler-dependent.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from fixtures import start_watchdog

N_THREADS = 6
ROUNDS = 12
# Base unit for the seq-ordered sleep. The earliest producer of a round sleeps
# (N_THREADS-1)*STEP and the latest sleeps 0, which forces the assign order
# without dragging the suite.
STEP = 0.001
WATCHDOG_SECONDS = 60


def _run_rounds(media_player, submit_fn, read_slot_seq, label: str) -> int:
    """Run ROUNDS rounds of N_THREADS concurrent single submissions.

    Each round installs a recorder that captures the allocated seqs and sleeps
    after allocation, longest for the earliest seq. The first round whose slot
    does not end on that round's max seq fails.

      submit_fn(thread_index) enqueues one frame through the add_* under test.
      read_slot_seq() returns the current slot's submit_seq, or None.
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
            # The earliest seq sleeps longest. With the sleep outside
            # _slot_lock it assigns last and overwrites the slot with the
            # oldest frame. Under the lock it cannot change assign order.
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
                  f"frame overwrote a newer one (last-assigner-wins)")
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
    assert failures == 0, f"{failures} slot-seq check(s) failed"
    print("PASS: scenario_slot_seq_wins")


if __name__ == "__main__":
    main()
