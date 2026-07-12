"""
Scenario: single-slot task races must not lose frames (issue #8 / B-08).

Four shapes against the REAL MediaPlayerThread methods:

  1. Drain half: perform_media_player_tasks read the touchscreen slot then
     unconditionally nulled it. A producer assigning in between lost its
     frame -- and since the producer had already stamped _last_enqueued_hash,
     STATIC content never re-enqueued: the strip stayed stale forever.
     Made deterministic here with a hooked `touchscreen_task` property: the
     drain's read triggers a real add_touchscreen_task on a producer thread
     and waits, forcing the assignment into the read->null window. Post-fix
     the producer blocks on the slot lock and its frame survives the drain.

  2. Clear half: _exec_clear's per-key get-then-del could delete a NEWER
     image task whose submit_seq contractually survives the Clear. Same
     hook trick on the image_tasks read via a wrapping dict.

  3. Write-cap putback half: the !27 rate-cap re-queues an over-budget
     touchscreen frame into the single slot iff it is still None -- but the
     None-check-then-set ran UNLOCKED. A producer assigning a NEWER frame
     between the check and the set had it clobbered by the older deferred
     frame. This is the site most entangled with the merged !27 write-cap:
     the test also asserts the rate-limit itself is preserved (the deferred
     frame is NOT written to the device -- no write-flood). The hook fires on
     the putback's own `touchscreen_task is None` read (the 2nd read of the
     slot in the tick; the drain's null is the 1st).

  4. Slot-wipe halves: clear_media_player_tasks() (skip-superseded page load)
     and _exec_clear_and_close() (terminal teardown) both wipe the slot; a
     producer assigning concurrently must not race a torn view. Driven under
     the same lock; asserted to leave a coherent (wiped) slot.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from fixtures import start_watchdog


def hook_types(media_player):
    """Subclass the real class with a hooked touchscreen_task property and
    swap the instance's __class__. The hook (when armed) fires on a read:
    it captures the value FIRST, then lets a producer thread run a real
    add_touchscreen_task, then returns the originally-read value -- the
    exact producer-in-the-window interleave.

    `_read_hook` fires once and self-clears (the drain's single read).
    `_read_hook_on_nth` fires on the N-th read of the slot from the hook
    thread and self-clears -- lets a test target the putback's own
    `touchscreen_task is None` read (the 2nd read of the tick) without
    firing on the drain's earlier read."""
    base = type(media_player)

    class Hooked(base):
        @property
        def touchscreen_task(self):
            value = self.__dict__.get("_ts_slot")
            on_hook_thread = threading.current_thread() is self.__dict__.get("_hook_thread")
            if on_hook_thread:
                nth = self.__dict__.get("_read_hook_on_nth")
                if nth is not None:
                    count = self.__dict__.get("_read_count", 0) + 1
                    self.__dict__["_read_count"] = count
                    target_n, target_hook = nth
                    if count == target_n:
                        self.__dict__["_read_hook_on_nth"] = None
                        # Return the value captured BEFORE the producer ran
                        # (the exact check-then-act window): the caller's
                        # None-check sees None, the producer assigns a newer
                        # frame, then -- unlocked -- the putback's set clobbers
                        # it with the older deferred frame.
                        target_hook()
                        return value
                hook = self.__dict__.get("_read_hook")
                if hook is not None:
                    self.__dict__["_read_hook"] = None
                    hook()
            return value

        @touchscreen_task.setter
        def touchscreen_task(self, value):
            self.__dict__["_ts_slot"] = value

    media_player.__dict__["_ts_slot"] = media_player.__dict__.pop("touchscreen_task", None)
    media_player.__dict__["_read_hook"] = None
    media_player.__dict__["_read_hook_on_nth"] = None
    media_player.__dict__["_read_count"] = 0
    media_player.__dict__["_hook_thread"] = None
    media_player.__class__ = Hooked
    return media_player


def check_drain_half() -> int:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-1", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]
    media_player = hook_types(media_player)

    produced = threading.Event()

    def producer():
        media_player.add_touchscreen_task(
            b"\x42" * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            controller_touchscreen=touch,
            img_hash=4242,
        )
        produced.set()

    def on_drain_read():
        t = threading.Thread(target=producer, daemon=True)
        t.start()
        # Give the producer a real chance to land inside the read->null
        # window. Post-fix it blocks on the slot lock instead.
        time.sleep(0.25)

    # Seed an old frame so the drain has something to read.
    media_player.add_touchscreen_task(
        b"\x01" * 64,
        page=controller.active_page,
        config_gen=controller._page_load_generation,
        controller_touchscreen=touch,
        img_hash=1,
    )

    media_player.__dict__["_read_hook"] = on_drain_read
    media_player.__dict__["_hook_thread"] = threading.current_thread()
    media_player.perform_media_player_tasks()

    if not produced.wait(timeout=5):
        print("FAIL(1): producer never completed (deadlock?)")
        return 1
    # Let a post-fix blocked producer land after the drain released the lock.
    time.sleep(0.1)

    survivor = media_player.__dict__.get("_ts_slot")
    if survivor is None or survivor.img_hash != 4242:
        print("FAIL(1): the frame produced during the drain window was lost "
              "(slot nulled over it) -- a static strip would stay stale "
              "forever")
        return 1
    print("PASS: producer frame in the drain window survives the read->null")
    return 0


def check_clear_half() -> int:
    from src.backend.DeckManagement.InputIdentifier import Input
    from src.backend.DeckManagement.DeckController import ClearMsg

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-2", n_keys=3, has_touchscreen=True
    )
    key0 = controller.inputs[Input.Key][0]

    def add_key_frame(payload: bytes):
        media_player.add_image_task(
            0, payload,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            controller_key=key0,
            img_hash=hash(payload),
        )

    add_key_frame(b"\x01" * 64)  # predates the Clear
    clear_seq = media_player.next_submit_seq()

    produced = threading.Event()

    class HookedDict(dict):
        armed = [True]

        def get(self, key, default=None):
            value = super().get(key, default)
            if self.armed[0]:
                self.armed[0] = False

                def producer():
                    add_key_frame(b"\x99" * 64)  # newer: survives the Clear
                    produced.set()

                t = threading.Thread(target=producer, daemon=True)
                t.start()
                time.sleep(0.25)
            return value

    hooked = HookedDict(media_player.image_tasks)
    media_player.image_tasks = hooked

    media_player._exec_clear(ClearMsg(seq=clear_seq))

    if not produced.wait(timeout=5):
        print("FAIL(2): producer never completed (deadlock?)")
        return 1
    time.sleep(0.1)

    survivor = media_player.image_tasks.get(0)
    if survivor is None or survivor.img_hash != hash(b"\x99" * 64):
        print("FAIL(2): _exec_clear deleted a newer task whose submit_seq "
              "contractually survives the Clear")
        return 1
    print("PASS: newer image task survives a racing Clear")
    return 0


def check_writecap_putback() -> int:
    """The !27 write-cap defers an over-budget touchscreen frame back into the
    single slot iff it's still None. The None-check-then-set must be atomic:
    a producer assigning a NEWER frame in between must win, and the older
    deferred frame must NOT be written to the device (the rate-limit is
    preserved -- no write-flood, which is the whole point of !27)."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-3", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]
    media_player = hook_types(media_player)

    # Force the over-budget branch: a recent last-write with the default
    # 20Hz cap (min_gap 50ms) means the seeded frame is deferred, not
    # written -- it flows into the putback, where the race lives.
    media_player._last_touch_write = time.time()

    produced = threading.Event()

    def producer():
        # A NEWER frame lands between the putback's None-check and its set.
        media_player.add_touchscreen_task(
            b"\x99" * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            controller_touchscreen=touch,
            img_hash=9999,
        )
        produced.set()

    def on_putback_read():
        t = threading.Thread(target=producer, daemon=True)
        t.start()
        # Give the producer a real chance to land inside the check->set
        # window. Post-fix it blocks on the slot lock the putback holds.
        time.sleep(0.25)

    # Seed the OLD frame the drain will read+null and then try to defer.
    media_player.add_touchscreen_task(
        b"\x01" * 64,
        page=controller.active_page,
        config_gen=controller._page_load_generation,
        controller_touchscreen=touch,
        img_hash=1,
    )

    # Fire on the 2nd slot read of the tick: read #1 is the drain's null,
    # read #2 is the putback's `touchscreen_task is None`.
    media_player.__dict__["_read_hook_on_nth"] = (2, on_putback_read)
    media_player.__dict__["_hook_thread"] = threading.current_thread()
    media_player.perform_media_player_tasks()

    if not produced.wait(timeout=5):
        print("FAIL(3): producer never completed (deadlock?)")
        return 1
    # Let a post-fix blocked producer land after the putback released the lock.
    time.sleep(0.1)

    survivor = media_player.__dict__.get("_ts_slot")
    if survivor is None or survivor.img_hash != 9999:
        print("FAIL(3): the newer frame produced in the putback check->set "
              "window was lost (clobbered by the older deferred frame)")
        return 1

    # !27 interaction: the over-budget OLD frame must have been DEFERRED, not
    # written -- the rate-limit is preserved, no write-flood.
    ts_writes = controller.deck.ops_by_name("set_touchscreen_image")
    if ts_writes:
        print(f"FAIL(3): the deferred over-budget frame was written to the "
              f"device ({len(ts_writes)} touchscreen write(s)) -- the !27 "
              f"rate-limit was not preserved")
        return 1
    print("PASS: newer frame survives the write-cap putback; deferred frame "
          "not written (!27 rate-limit preserved)")
    return 0


def check_slot_wipes() -> int:
    """clear_media_player_tasks() (skip-superseded load) and
    _exec_clear_and_close() (terminal teardown) both wipe the single slot
    under _slot_lock. A producer assigning concurrently must leave a coherent
    slot -- either wiped or holding a whole task, never a torn view. Cheaper
    coverage: assert each wipe leaves the slot None (the producer here runs
    strictly before the wipe, so the wipe wins deterministically) and that
    neither deadlocks under the _page_gen_lock -> _slot_lock ordering."""
    from src.backend.DeckManagement.InputIdentifier import Input
    from src.backend.DeckManagement.DeckController import DeckController

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-4", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]

    def seed():
        media_player.add_touchscreen_task(
            b"\x01" * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            controller_touchscreen=touch,
            img_hash=1,
        )

    # clear_media_player_tasks: acquires _page_gen_lock THEN _slot_lock (the
    # one nested ordering) -- must not deadlock and must wipe the slot. Call
    # the REAL DeckController method with the stub as self (it duck-touches
    # only _page_gen_lock/_page_load_generation/media_player, all present).
    seed()
    DeckController.clear_media_player_tasks(controller, gen=controller._page_load_generation)
    if media_player.touchscreen_task is not None:
        print("FAIL(4): clear_media_player_tasks did not wipe the slot")
        return 1

    # _exec_clear_and_close: terminal wipe under _slot_lock.
    seed()
    media_player._exec_clear_and_close()
    if media_player.touchscreen_task is not None:
        print("FAIL(4): _exec_clear_and_close did not wipe the slot")
        return 1

    print("PASS: slot wipes (clear_media_player_tasks / _exec_clear_and_close) "
          "leave a coherent slot, no lock inversion")
    return 0


def main() -> int:
    start_watchdog(40, "touchscreen_slot_race")
    rc = check_drain_half()
    rc |= check_clear_half()
    rc |= check_writecap_putback()
    rc |= check_slot_wipes()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
