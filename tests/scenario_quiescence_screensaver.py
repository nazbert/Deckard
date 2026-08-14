"""
The quiescence gate never touches the deck screensaver.

The physical deck stays visible while the monitor is locked, so the
screensaver's animation is the intended content and animations_gated carries
a not screen_saver.showing term. On hide() the restored page must paint every
key once, transparent ones included, and only then go quiet.
"""
import os
import time

import fixtures
import globals as gl

from src.backend.PresenceMonitor.PresenceMonitor import (  # noqa: E402
    MODE_SYSTEM_IDLE,
    PresenceMonitor,
)

OBSERVE_S = 1.5


def distinct_key_frames(deck) -> set:
    return {e[4] for e in deck.journal() if e[2] == "set_key_image"}


def wait_until_quiet(deck, quiet_for: float = 0.5, timeout: float = 10.0) -> bool:
    """Waits until no device write has landed for quiet_for seconds.

    The settle window's length depends on how fast the page-load tasks drain,
    which is not a constant on a loaded machine. The invariant is that the
    loop goes quiet, not that it goes quiet in a fixed number of
    milliseconds."""
    deadline = time.monotonic() + timeout
    seen = len(deck.journal())
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = len(deck.journal())
        if now != seen:
            seen = now
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= quiet_for:
            return True
    return False


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_quiescence_screensaver")

    media = os.path.join(gl.DATA_PATH, "media")
    page_video = fixtures.make_test_mp4(os.path.join(media, "ss_page.mp4"),
                                        n_frames=200, color=(64, 200))
    saver_video = fixtures.make_test_mp4(os.path.join(media, "ss_saver.mp4"),
                                         n_frames=200, color=(200, 40))
    page_path = fixtures.seed_page_with_background_and_screensaver(
        "QSaver", page_video, saver_video, screensaver_time_delay=60,
    )

    controller = fixtures.make_headless_controller(serial="qsaver-1")
    monitor = None
    try:
        deck = fixtures.raw_deck(controller)
        media_player = controller.media_player
        key_count = controller.deck.key_count()

        page = gl.page_manager.get_page(page_path, controller)
        controller.load_page(page, allow_reload=True)
        assert fixtures.wait_until(lambda: len(distinct_key_frames(deck)) >= 4, timeout=10), (
            "fixture sanity: the page's background video never played"
        )
        assert controller.screen_saver.media_path == saver_video, (
            f"fixture sanity: screensaver media not loaded from the page "
            f"({controller.screen_saver.media_path})"
        )

        controller.screen_saver.show()
        assert controller.screen_saver.showing is True

        monitor = PresenceMonitor(mode=MODE_SYSTEM_IDLE, minutes=1, idle_detector=False)
        gl.presence_monitor = monitor
        gl.screen_locked = True
        monitor.on_lock_changed(True)
        assert monitor.is_quiescent() is True
        assert controller.animations_gated() is False, (
            "the gate must exempt a deck whose screensaver is showing"
        )

        # The screensaver keeps animating while the user is away.
        deck.clear_journal()
        gated_before = media_player.gated_ticks
        assert fixtures.wait_until(
            lambda: len(distinct_key_frames(deck)) >= 3, timeout=OBSERVE_S + 3.5
        ), (
            "the screensaver's video stopped writing frames while quiescent -- "
            "the gate is swallowing the content it is supposed to exempt"
        )
        assert media_player.gated_ticks == gated_before, (
            f"{media_player.gated_ticks - gated_before} ticks gated while the "
            f"screensaver was showing"
        )
        saver_frames = distinct_key_frames(deck)
        print(f"PASS: screensaver keeps animating while quiescent "
              f"({len(saver_frames)} distinct frames, 0 gated ticks)")

        # On hide() while still quiescent, the restored page must paint.
        saver_signature = {k: deck.last_op_for(f"key:{k}") for k in range(key_count)}
        assert all(saver_signature.values()), "fixture sanity: screensaver painted every key"
        deck.clear_journal()
        controller.screen_saver.hide()
        assert controller.screen_saver.showing is False

        assert fixtures.wait_until(
            lambda: all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count)),
            timeout=8,
        ), (
            "hiding the screensaver while quiescent never repainted every key -- "
            "the deck would sit on the screensaver's last frame for the whole "
            "away window"
        )
        assert wait_until_quiet(deck), (
            "the restored page never stopped writing -- the loop kept animating "
            "it while the user is still away"
        )
        per_key: dict = {}
        for entry in deck.journal():
            if entry[2] == "set_key_image":
                per_key[entry[3]] = per_key.get(entry[3], 0) + 1
        assert max(per_key.values()) <= 8, (
            f"the restore repainted keys {max(per_key.values())} times instead of "
            f"settling: {sorted(per_key.items())}"
        )
        for k in range(key_count):
            after = deck.last_op_for(f"key:{k}")
            assert after[4] != saver_signature[k][4], (
                f"key {k} still shows the SCREENSAVER's frame after hide()"
            )

        # Only then does the loop quiet down again.
        settled = len(deck.journal())
        gated_before = media_player.gated_ticks
        time.sleep(OBSERVE_S)
        assert len(deck.journal()) == settled, (
            "the restored page kept animating while the user is still away"
        )
        assert media_player.gated_ticks > gated_before, (
            "the loop never re-gated after the screensaver was hidden"
        )
        print("PASS: hide() while quiescent paints the restored page once, then re-gates")
    finally:
        if monitor is not None:
            monitor.stop()
        gl.presence_monitor = None
        gl.screen_locked = False
        fixtures.teardown(controller)

    print("\nALL PASS: scenario_quiescence_screensaver")


if __name__ == "__main__":
    main()
