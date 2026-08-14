"""
Integration scenario for a concurrent screensaver transition storm.

Three threads hammer ScreenSaver's entry points at once, a timer-like show(), a
USB-event-like on_key_change() and a settings-like set_enable pair.
"""

# Nothing may deadlock or raise, and the final journal must agree with showing
# on every key, with no mix across keys and no stuck blank.
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
    # Pre-seed "Main" with a distinct background and persisted screensaver
    # settings before the controller exists. load_page always calls
    # load_screensaver(page), and hide() ends in a load_page, so without this
    # the first hide() resets media_path to None and later show() calls
    # paint blank.
    fixtures.seed_page_with_background_and_screensaver(
        "Main", page_png, ss_png, screensaver_time_delay=60
    )

    controller = fixtures.make_headless_controller(serial="storm-ss-1", page_name="Main")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    # Learn each state's paint signature in isolation, before the storm.
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

    # The storm.
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

    # Settle, then check coherence.
    ok = fixtures.wait_until(settled, timeout=5)
    assert ok, "deck did not settle after the storm"
    # A quiescence window. A straggler transition still landing frames shows
    # up here as a late change to the last-write hash.
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
