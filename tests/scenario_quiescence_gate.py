"""
The media loop's quiescence gate (issue #144), over a REAL MediaPlayerThread.

A background-video page is the worst case the gate exists for: it decodes,
composites and writes every key at 30 FPS forever, whether or not anyone is
looking. With the presence monitor reporting the user away, this scenario
pins that:

  (a) with the DEFAULT pause mode nothing gates, even with the screen locked
      -- the opt-in property, asserted at the DeckController seam,
  (b) gated: ZERO device writes and a ~2 Hz loop cadence (gated_ticks
      advancing, media_ticks advancing with it) -- no decode, no composite,
      no tick,
  (c) the deck stays FUNCTIONAL while gated: a control message (brightness)
      and an interactive paint (add_image_task) both still reach the device,
  (d) a page change landing while gated repaints the whole page -- including
      the transparent keys whose device paint update_all_inputs() delegates
      to the video loop -- and then quiets again (the page-generation watch;
      without it the deck would show the previous page's imagery on every
      transparent key for the entire away window),
  (e) restoring presence resumes animation within the 500ms acceptance bound.
"""
import os
import time

import fixtures
import globals as gl

from src.backend.DeckManagement.DeckController import SetBrightnessMsg  # noqa: E402
from src.backend.PresenceMonitor.PresenceMonitor import (  # noqa: E402
    MODE_SCREENSAVER,
    MODE_SYSTEM_IDLE,
    PresenceMonitor,
)

OBSERVE_S = 1.2


def key_writes(deck, since: int = 0) -> list:
    return [e for e in deck.ops_after(since) if e[2] == "set_key_image"]


def animation_writes(deck, since: int = 0) -> list:
    return [e for e in deck.ops_after(since)
            if e[2] in ("set_key_image", "set_touchscreen_image")]


def set_locked(monitor, locked: bool) -> None:
    """What LockScreenManager.lock() does: publish, then notify."""
    gl.screen_locked = locked
    monitor.on_lock_changed(locked)


def wait_for_playback(deck, label: str) -> None:
    assert fixtures.wait_until(
        lambda: len({e[4] for e in deck.journal() if e[2] == "set_key_image"}) >= 4,
        timeout=10,
    ), f"fixture sanity: {label} never produced 4 distinct key frames -- not playing"


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_quiescence_gate")

    media = os.path.join(gl.DATA_PATH, "media")
    video_a = fixtures.make_test_mp4(os.path.join(media, "gate_a.mp4"),
                                     n_frames=200, color=(64, 200))
    video_b = fixtures.make_test_mp4(os.path.join(media, "gate_b.mp4"),
                                     n_frames=200, color=(200, 40))
    path_a = fixtures.seed_page_with_background("QGateA", video_a, loop=True)
    path_b = fixtures.seed_page_with_background("QGateB", video_b, loop=True)

    controller = fixtures.make_headless_controller(serial="qgate-1")
    monitor = None
    try:
        deck = fixtures.raw_deck(controller)
        media_player = controller.media_player
        key_count = controller.deck.key_count()

        page_a = gl.page_manager.get_page(path_a, controller)
        page_b = gl.page_manager.get_page(path_b, controller)
        controller.load_page(page_a, allow_reload=True)
        wait_for_playback(deck, "page A")

        # (a) the default mode is inert -- this is what makes the whole
        # feature opt-in, and it must hold at the seam the media loop reads.
        assert controller.animations_gated() is False, (
            "nothing may gate before a presence monitor exists"
        )
        monitor = PresenceMonitor(mode=MODE_SCREENSAVER, idle_detector=False)
        gl.presence_monitor = monitor
        set_locked(monitor, True)
        assert controller.animations_gated() is False, (
            "the DEFAULT pause mode must not gate, even with the screen locked"
        )
        set_locked(monitor, False)
        monitor.stop()

        # Opt in, then lock: gating engages.
        monitor = PresenceMonitor(mode=MODE_SYSTEM_IDLE, minutes=1, idle_detector=False)
        gl.presence_monitor = monitor
        signature_a = {k: deck.last_op_for(f"key:{k}") for k in range(key_count)}
        assert all(signature_a.values()), "fixture sanity: not every key painted page A"

        set_locked(monitor, True)
        assert controller.animations_gated() is True
        assert fixtures.wait_until(lambda: media_player.gated_ticks > 0, timeout=3), (
            "the media loop never gated a tick"
        )
        # Let the page-generation watch's settle window finish before
        # measuring: entering the gate legitimately renders a few frames.
        time.sleep(0.4)

        # (b) gated: no animation reaches the device, and the loop idles.
        deck.clear_journal()
        ticks_before = media_player.media_ticks
        gated_before = media_player.gated_ticks
        time.sleep(OBSERVE_S)
        ticks = media_player.media_ticks - ticks_before
        gated = media_player.gated_ticks - gated_before
        stray = animation_writes(deck)
        assert not stray, (
            f"{len(stray)} animation write(s) reached the device while gated: "
            f"{[(e[2], e[3]) for e in stray[:5]]}"
        )
        assert gated >= 1, "no tick was counted as gated"
        assert gated == ticks, (
            f"{ticks - gated} of {ticks} ticks rendered while gated -- the gate "
            f"is leaking render passes"
        )
        assert ticks <= 2 * 2 * OBSERVE_S + 2, (
            f"gated cadence is {ticks / OBSERVE_S:.1f} Hz, expected ~2 Hz -- the "
            f"FPS selection is not honoring the gate (stale _cached_needs_ticks?)"
        )
        print(f"PASS: gated -- 0 animation writes, {ticks / OBSERVE_S:.1f} Hz, "
              f"{gated}/{ticks} ticks gated")

        # (c) the deck is paused, not dead: control ops and interactive
        # paints must still land.
        seq = deck.current_seq()
        media_player.submit_control(SetBrightnessMsg(value=42))
        assert fixtures.wait_until(
            lambda: any(e[2] == "set_brightness" for e in deck.ops_after(seq)),
            timeout=2,
        ), "a control message was starved by the gate"

        seq = deck.current_seq()
        media_player.add_image_task(0, fixtures.make_native_image(fill=7))
        assert fixtures.wait_until(
            lambda: any(e[3] == "key:0" for e in deck.ops_after(seq)), timeout=2
        ), "an interactive paint was starved by the gate"
        assert media_player.gated_ticks > gated_before, "still gated after those"
        print("PASS: brightness + interactive paints still land while gated")

        # (d) a page change while gated must paint the NEW page once --
        # transparent keys included -- and then go quiet again.
        deck.clear_journal()
        controller.load_page(page_b, allow_reload=True)
        assert fixtures.wait_until(
            lambda: all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count)),
            timeout=8,
        ), (
            "a page change made while gated never painted every key -- the "
            "page-generation watch is not running the un-gated pass (transparent "
            "keys on a video-bg page would keep showing the old page)"
        )
        time.sleep(0.6)
        per_key: dict = {}
        for entry in key_writes(deck):
            per_key[entry[3]] = per_key.get(entry[3], 0) + 1
        assert max(per_key.values()) <= 4, (
            f"the gated page change repainted keys {max(per_key.values())} times; "
            f"the watch should settle after a couple of frames: {sorted(per_key.items())}"
        )
        for k in range(key_count):
            after = deck.last_op_for(f"key:{k}")
            assert after[4] != signature_a[k][4], (
                f"key {k} still shows page A's frame after the gated page change"
            )

        settled = len(deck.journal())
        gated_before = media_player.gated_ticks
        time.sleep(OBSERVE_S)
        assert len(deck.journal()) == settled, (
            "the loop never re-gated after the page change -- it kept animating "
            "page B while the user is still away"
        )
        assert media_player.gated_ticks > gated_before
        print("PASS: a gated page change paints the new page once, then re-gates")

        # (e) presence restored -> animation back within the acceptance bound.
        deck.clear_journal()
        gl.screen_locked = False
        started = time.monotonic()
        monitor.on_lock_changed(False)
        assert fixtures.wait_until(
            lambda: bool(key_writes(deck)), timeout=0.5, interval=0.005
        ), "animation did not resume within 500ms of presence being restored"
        print(f"PASS: animation resumed {1000 * (time.monotonic() - started):.0f}ms "
              f"after presence restore")

        wait_for_playback(deck, "page B after resume")
        print("PASS: video playback continues after the resume")
    finally:
        if monitor is not None:
            monitor.stop()
        gl.presence_monitor = None
        gl.screen_locked = False
        fixtures.teardown(controller)

    print("\nALL PASS: scenario_quiescence_gate")


if __name__ == "__main__":
    main()
