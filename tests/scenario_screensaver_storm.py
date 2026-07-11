"""
Integration scenario (docs/presenter-migration-plan.md §7 "Six-requester
transition storm", §4 M3): three threads hammer ScreenSaver's transition
entry points concurrently and repeatedly --

  * a timer-like thread calling show() directly (mirrors on_timer_end)
  * a USB-event-like thread calling on_key_change() (mirrors the reader
    thread, hides if currently showing)
  * a settings-like thread calling set_enable(False) then set_enable(True)
    (mirrors the GTK settings path, which hides if currently showing)

for 30 iterations each, all racing the same controller's screensaver. Pass
criteria (plan §7): no deadlock (bounded by a watchdog), `showing` consistent
with the last transition, final journal state coherent (page content OR
screensaver content, never a mix across keys, never a stuck blank), and no
exceptions in any thread.

The screensaver media is a static PNG (not a video) here on purpose: the
video-specific pre-resolution/race path is covered by
scenario_screensaver_bg_race.py; this scenario is about the *serialization*
of the transition itself, and keeping each iteration cheap is what makes 30
iterations x 3 threads finish well inside the watchdog.
"""
import os
import threading
import time

import fixtures
import globals as gl

WATCHDOG_SECONDS = 60
STORM_ITERATIONS = 30
JOIN_TIMEOUT = 45.0


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_screensaver_storm")

    page_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "storm_page.png"), color=(200, 20, 20))
    ss_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "storm_ss.png"), color=(20, 20, 200))
    # Pre-seed "Main" with a distinct background AND persisted screensaver
    # settings BEFORE the controller is constructed (seed_page() inside
    # make_headless_controller is idempotent and won't overwrite an existing
    # page file). The screensaver settings matter here specifically because
    # load_page() always calls load_screensaver(page), which reloads
    # ScreenSaver.media_path/enable/time_delay FROM THE PAGE on every single
    # load -- and hide()'s phase 3 IS a load_page() call. Without this, the
    # very first hide() during the storm would reset media_path to None (no
    # page ever configured one) and every show() after that would paint a
    # blank instead of ss_png -- not a concurrency bug, just this reload
    # contract (see fixtures.seed_page_with_background_and_screensaver).
    fixtures.seed_page_with_background_and_screensaver(
        "Main", page_png, ss_png, screensaver_time_delay=60
    )

    controller = fixtures.make_headless_controller(serial="storm-ss-1", page_name="Main")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    # --- Learn each state's paint signature in isolation, before storming. ---
    def settled():
        return all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count))

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
    ok = fixtures.wait_until(lambda: not controller.screen_saver.showing, timeout=5)
    assert ok, "fixture setup: screensaver never hid"

    # --- The storm. ---
    exceptions: list[tuple[str, BaseException]] = []
    exc_lock = threading.Lock()

    def record(tag: str, exc: BaseException) -> None:
        with exc_lock:
            exceptions.append((tag, exc))

    def timer_like() -> None:
        for _ in range(STORM_ITERATIONS):
            try:
                controller.screen_saver.show()
            except BaseException as e:
                record("show", e)
            time.sleep(0.011)

    def usb_event_like() -> None:
        for _ in range(STORM_ITERATIONS):
            try:
                controller.screen_saver.on_key_change()
            except BaseException as e:
                record("on_key_change", e)
            time.sleep(0.013)

    def settings_like() -> None:
        for _ in range(STORM_ITERATIONS):
            try:
                controller.screen_saver.set_enable(False)
                controller.screen_saver.set_enable(True)
            except BaseException as e:
                record("set_enable", e)
            time.sleep(0.017)

    threads = [
        threading.Thread(target=timer_like, name="StormTimer"),
        threading.Thread(target=usb_event_like, name="StormUSB"),
        threading.Thread(target=settings_like, name="StormSettings"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT)

    for t in threads:
        assert not t.is_alive(), f"{t.name} did not finish within {JOIN_TIMEOUT}s -- possible deadlock"
    assert not exceptions, f"exceptions occurred during the storm: {exceptions!r}"

    # --- Settle and check coherence. ---
    ok = fixtures.wait_until(settled, timeout=5)
    assert ok, "deck did not settle after the storm"
    # Quiescence window: if a straggler transition were still landing frames,
    # it would show up here as a late change to the last-write hash.
    time.sleep(0.3)

    final_showing = controller.screen_saver.showing
    expected_sig = ss_sig if final_showing else page_sig
    other_sig = page_sig if final_showing else ss_sig

    for k in range(key_count):
        last = deck.last_op_for(f"key:{k}")
        assert last is not None, f"key {k} was never painted"
        assert last[4] == expected_sig[k], (
            f"key {k}: final content does not match showing={final_showing} "
            f"(got {last[4]}, expected {expected_sig[k]}) -- incoherent final state"
        )
        assert last[4] != other_sig[k], (
            f"key {k}: final content matches the OTHER state's signature -- "
            f"a stale transition's frame survived as the last write"
        )

    fixtures.teardown(controller)
    print("PASS: scenario_screensaver_storm")


if __name__ == "__main__":
    main()
