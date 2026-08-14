"""
Single-slot task races must not lose frames.

The drain, the Clear, the write-cap putback and the two slot wipes all take
_slot_lock, so a producer assigning concurrently either wins or blocks.
"""

# A hooked touchscreen_task property fires a real producer inside each window,
# so every interleave is deterministic rather than left to the scheduler.
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time

from fixtures import start_watchdog


def hook_types(media_player):
    """Subclass the real class with a hooked touchscreen_task property and swap
    the instance's __class__.

    An armed hook fires on a read.
    """
    # It captures the value first, lets a producer thread run a real
    # add_touchscreen_task, then returns what it captured, which is the
    # producer-in-the-window interleave.
    # _read_hook fires once, and _read_hook_on_nth fires on the Nth read of
    # the slot, which is how a check targets the putback's own None check.
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
                        # Return the value captured before the producer
                        # ran, which is the check-then-act window. The
                        # caller's None check sees None, the producer
                        # assigns a newer frame, and an unlocked putback then
                        # clobbers it with the older deferred frame.
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
        # Give the producer a real chance to land inside the read-and-null
        # window. With the lock in place it blocks there instead.
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
    # Let a blocked producer land after the drain released the lock.
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

    add_key_frame(b"\x01" * 64)  # this frame predates the Clear
    clear_seq = media_player.next_submit_seq()

    produced = threading.Event()

    class HookedDict(dict):
        armed = [True]

        def get(self, key, default=None):
            value = super().get(key, default)
            if self.armed[0]:
                self.armed[0] = False

                def producer():
                    add_key_frame(b"\x99" * 64)  # newer, so it survives the Clear
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
    """The write cap defers an over-budget touchscreen frame back into the
    single slot while that slot is still None. The check and the set must be
    atomic, so a producer assigning a newer frame in between wins, and the
    older deferred frame never reaches the device."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="slotrace-3", has_touchscreen=True
    )
    touch = controller.inputs[Input.Touchscreen][0]
    media_player = hook_types(media_player)

    # Force the over-budget branch. A recent last write against the default
    # 20Hz cap defers the seeded frame rather than writing it, so the frame
    # flows into the putback where the race lives.
    media_player._last_touch_write = time.time()

    produced = threading.Event()

    def producer():
        # A newer frame lands between the putback's None check and its set.
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
        # Give the producer a real chance to land inside the check-and-set
        # window. It blocks on the slot lock the putback holds instead.
        time.sleep(0.25)

    # Seed the old frame the drain reads, nulls and then tries to defer.
    media_player.add_touchscreen_task(
        b"\x01" * 64,
        page=controller.active_page,
        config_gen=controller._page_load_generation,
        controller_touchscreen=touch,
        img_hash=1,
    )

    # Fire on the second slot read of the tick. The first is the drain's
    # null, and the second is the putback's None check.
    media_player.__dict__["_read_hook_on_nth"] = (2, on_putback_read)
    media_player.__dict__["_hook_thread"] = threading.current_thread()
    media_player.perform_media_player_tasks()

    if not produced.wait(timeout=5):
        print("FAIL(3): producer never completed (deadlock?)")
        return 1
    # Let a blocked producer land after the putback released the lock.
    time.sleep(0.1)

    survivor = media_player.__dict__.get("_ts_slot")
    if survivor is None or survivor.img_hash != 9999:
        print("FAIL(3): the newer frame produced in the putback check->set "
              "window was lost (clobbered by the older deferred frame)")
        return 1

    # The over-budget old frame must have been deferred rather than written,
    # so the rate limit holds.
    ts_writes = controller.deck.ops_by_name("set_touchscreen_image")
    if ts_writes:
        print(f"FAIL(3): the deferred over-budget frame was written to the "
              f"device ({len(ts_writes)} touchscreen write(s)) -- the write-cap "
              f"rate-limit was not preserved")
        return 1
    print("PASS: newer frame survives the write-cap putback; deferred frame "
          "not written (rate-limit preserved)")
    return 0


def check_slot_wipes() -> int:
    """clear_media_player_tasks and _exec_clear_and_close both wipe the
    single slot under _slot_lock, so a concurrent producer leaves a coherent
    slot, either wiped or holding a whole task. The producer here runs before
    the wipe, so each wipe wins and neither call deadlocks."""
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

    # clear_media_player_tasks acquires _page_gen_lock and then _slot_lock,
    # which is the one nested ordering. It must not deadlock and must wipe
    # the slot. The real DeckController method runs with the stub as self.
    seed()
    DeckController.clear_media_player_tasks(controller, gen=controller._page_load_generation)
    if media_player.touchscreen_task is not None:
        print("FAIL(4): clear_media_player_tasks did not wipe the slot")
        return 1

    # _exec_clear_and_close is the terminal wipe under _slot_lock.
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
