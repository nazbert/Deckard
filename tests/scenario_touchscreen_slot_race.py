"""
Scenario: single-slot task races must not lose frames (issue #8 / B-08).

Two shapes against the REAL MediaPlayerThread methods:

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
    exact producer-in-the-window interleave."""
    base = type(media_player)

    class Hooked(base):
        @property
        def touchscreen_task(self):
            value = self.__dict__.get("_ts_slot")
            hook = self.__dict__.get("_read_hook")
            if hook is not None and threading.current_thread() is self.__dict__.get("_hook_thread"):
                self.__dict__["_read_hook"] = None
                hook()
            return value

        @touchscreen_task.setter
        def touchscreen_task(self, value):
            self.__dict__["_ts_slot"] = value

    media_player.__dict__["_ts_slot"] = media_player.__dict__.pop("touchscreen_task", None)
    media_player.__dict__["_read_hook"] = None
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


def main() -> int:
    start_watchdog(40, "touchscreen_slot_race")
    rc = check_drain_half()
    rc |= check_clear_half()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
