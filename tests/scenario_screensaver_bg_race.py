"""
Integration scenario (docs/presenter-migration-plan.md §7 "Background-load
vs screensaver race", §10 C-F6): a load_background() worker that is mid-write
-- already past its own generation check, actually holding
_background_load_lock while it composites -- must never have its write land
AFTER the screensaver's, even though the worker's check ran before
ScreenSaver.show() bumped the generation.

Deterministic injection point: DeckController.load_background() calls
Background.set_from_path() from *inside* its `with self._background_load_lock`
block, immediately after the generation check
(DeckController.py load_background ~1009-1022). This monkeypatches
Background.set_from_path to block on a threading.Event, which means the
worker parks *while still holding _background_load_lock* -- reproducing "a
load_background worker that is slow to decode" deterministically, without
needing a real slow codec or a timing coin-flip against the thread pool's
scheduling.

Why the gate has to sit inside set_from_path and not before load_background
is even called: gating the entry to load_background only delays *when the
gen check happens* -- if released after show() already bumped the
generation, the worker's own (untouched, pre-existing) gen check would
correctly no-op on its own, passing even a screensaver fix that took no lock
at all. The real C-F6 hazard is specifically the worker being mid-write
(lock held, check already passed against the *old* generation) while
show()'s own background swap races it -- which is what this reproduces:

  1. load_page(page) dispatches load_background onto the pool; it acquires
     _background_load_lock, passes its generation check (nothing has bumped
     the generation yet), and parks inside the gated set_from_path -- lock
     still held.
  2. screen_saver.show() runs concurrently, on its own thread: it bumps the
     generation and tries to swap in its own background under the SAME
     _background_load_lock (plan §4 M3) -- this must block until the worker
     above releases the lock.
  3. The gate is released: the worker finishes writing the (now-stale) page
     background and releases the lock.
  4. show()'s thread, unblocked, acquires the lock and applies the
     screensaver's background -- landing last, unconditionally (its own
     generation re-check is against its own just-bumped value, so it always
     passes) -- this ordering is what §4 M3's fix guarantees regardless of
     which side won the lock-acquisition race.

This scenario was validated against a deliberately-weakened build (screensaver
background swap with no lock/regeneration-check) to confirm it actually fails
without the fix, not just passes vacuously.
"""
import os
import threading
import time

import fixtures
import globals as gl
from src.backend.DeckManagement.DeckController import Background

WATCHDOG_SECONDS = 30


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_screensaver_bg_race")

    controller = fixtures.make_headless_controller(serial="bgrace-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    page_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "race_page.png"), color=(10, 200, 10))
    ss_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "race_ss.png"), color=(10, 10, 200))
    # The screensaver settings must be persisted ON THE PAGE, not just set via
    # ScreenSaver.set_media_path(): load_page() always calls
    # load_screensaver(page) (DeckController.py ~1042), which reloads
    # ScreenSaver.media_path/enable/time_delay FROM THE PAGE on every single
    # load_page() call -- including the load_page(page) calls below. A page
    # with no persisted screensaver settings would silently reset media_path
    # to None, and show() would then paint blank instead of ss_png (see
    # fixtures.seed_page_with_background_and_screensaver).
    page_path = fixtures.seed_page_with_background_and_screensaver(
        "RacePage", page_png, ss_png, screensaver_time_delay=60
    )
    page = gl.page_manager.get_page(page_path, controller)

    def settled():
        return all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count))

    # --- Learn each state's paint signature in isolation, before racing. ---
    controller.load_page(page, allow_reload=True)
    ok = fixtures.wait_until(settled, timeout=5)
    assert ok, "fixture setup: page content never painted"
    time.sleep(0.1)
    page_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}

    controller.screen_saver.show()
    ok = fixtures.wait_until(lambda: controller.screen_saver.showing and settled(), timeout=5)
    assert ok, "fixture setup: screensaver never showed"
    time.sleep(0.1)
    ss_sig = {k: deck.last_op_for(f"key:{k}")[4] for k in range(key_count)}

    for k in range(key_count):
        assert page_sig[k] != ss_sig[k], (
            f"key {k}: page and screensaver produced the same hash -- the "
            f"fixture isn't actually distinguishing the two states"
        )

    controller.screen_saver.hide()
    ok = fixtures.wait_until(lambda: not controller.screen_saver.showing and settled(), timeout=5)
    assert ok, "fixture setup: screensaver never hid"
    time.sleep(0.1)

    # --- The race. ---
    real_set_from_path = Background.set_from_path
    gate = threading.Event()
    worker_parked = threading.Event()

    def gated_set_from_path(self, *args, **kwargs):
        worker_parked.set()
        gate.wait(timeout=10)
        return real_set_from_path(self, *args, **kwargs)

    Background.set_from_path = gated_set_from_path
    try:
        deck.clear_journal()
        # Dispatches load_background onto the pool; it will acquire
        # _background_load_lock, pass its generation check (fresh, nothing
        # has bumped it yet), and park inside gated_set_from_path -- lock
        # still held.
        controller.load_page(page, allow_reload=True)
        ok = fixtures.wait_until(worker_parked.is_set, timeout=5)
        assert ok, "the load_background worker never reached the gate"

        # show() races the parked worker concurrently, on its own thread: if
        # it needs _background_load_lock to apply its own background (the
        # fix), it must block here until the gate below is released.
        show_done = threading.Event()

        def do_show():
            controller.screen_saver.show()
            show_done.set()

        t_show = threading.Thread(target=do_show, name="RaceShow")
        t_show.start()

        # Give show() a real chance to run all the way through IF it does
        # not actually need the lock (the pre-fix bug shape) -- this is what
        # makes the scenario discriminate: a weakened implementation applies
        # its background here, before the worker's stale write lands below.
        time.sleep(0.3)

        gate.set()  # release the parked worker -- it finishes writing page_sig content
        t_show.join(timeout=10)
        assert show_done.is_set(), "screen_saver.show() did not complete -- possible deadlock"

        ok = fixtures.wait_until(settled, timeout=5)
        assert ok, "deck did not settle after the race"
        time.sleep(0.3)  # quiescence window
    finally:
        Background.set_from_path = real_set_from_path

    assert controller.screen_saver.showing is True, "screensaver must still be showing after the race"

    for k in range(key_count):
        last = deck.last_op_for(f"key:{k}")
        assert last is not None, f"key {k} was never painted during the race"
        assert last[4] == ss_sig[k], (
            f"key {k}: final content does not match the screensaver's signature "
            f"(got {last[4]}, ss={ss_sig[k]}, page={page_sig[k]}) -- the stale "
            f"load_background worker overwrote the screensaver's background (C-F6)"
        )
        assert last[4] != page_sig[k], (
            f"key {k}: final content matches the stale page's signature -- "
            f"the delayed load_background worker won the race (C-F6 regression)"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_screensaver_bg_race")


if __name__ == "__main__":
    main()
