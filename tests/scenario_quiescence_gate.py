"""
The media loop's quiescence gate, over a real MediaPlayerThread.

A background-video page decodes, composites and writes every key at 30 FPS
forever. With the user away the gate stops every animation write, still lands
control messages and interactive paints, repaints once on a page change, and
resumes animation within 500ms of a presence return.
"""
import itertools
import os
import threading
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


def _window_closed(media_player, for_s: float = 0.25) -> bool:
    """True once the settle window has stopped rendering ticks for for_s.

    gate_window_ticks is the loop's own count of ticks the window rendered
    instead of gating, so this reads the mechanism directly rather than
    inferring it from device writes a producer generates anyway."""
    seen = media_player.gate_window_ticks
    deadline = time.monotonic() + for_s
    while time.monotonic() < deadline:
        time.sleep(0.02)
        if media_player.gate_window_ticks != seen:
            return False
    return True


def set_locked(monitor, locked: bool) -> None:
    """What LockScreenManager.lock does. Publish, then notify."""
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

        # The default mode is inert, which is what makes the feature opt-in.
        # It must hold at the seam the media loop reads.
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

        # Opt in, then lock. Gating engages.
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
        # measuring. Entering the gate renders a few frames.
        time.sleep(0.4)

        # While gated no animation reaches the device and the loop idles.
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

        # The deck is paused, not dead. Control ops and interactive paints
        # must still land.
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

        # A full repaint armed while gated, from a resume or from the 2s
        # retry after write failures, bumps no generation. It goes through the
        # same update_all_inputs(), so it shares the transparent-key blind
        # spot and must open the render window too.
        deck.clear_journal()
        controller._schedule_full_repaint()
        assert fixtures.wait_until(
            lambda: all(deck.last_op_for(f"key:{k}") is not None for k in range(key_count)),
            timeout=8,
        ), (
            "a full repaint fired while gated never reached the transparent keys "
            "-- a machine waking from suspend while the user is away would leave "
            "them showing whatever survived the suspend"
        )
        assert wait_until_quiet(deck), "the repaint never settled"
        settled = len(deck.journal())
        gated_before = media_player.gated_ticks
        time.sleep(OBSERVE_S)
        assert len(deck.journal()) == settled and media_player.gated_ticks > gated_before, (
            "the loop never re-gated after the full repaint"
        )
        print("PASS: a full repaint while gated paints once, then re-gates")

        # A page change while gated must paint the new page once, transparent
        # keys included, and then go quiet again.
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
        assert wait_until_quiet(deck), (
            "the gated page change never stopped writing -- the loop is still "
            "animating page B while the user is away"
        )
        per_key: dict = {}
        for entry in key_writes(deck):
            per_key[entry[3]] = per_key.get(entry[3], 0) + 1
        assert max(per_key.values()) <= 8, (
            f"the gated page change repainted keys {max(per_key.values())} times; "
            f"the watch is meant to emit a short burst, not keep animating: "
            f"{sorted(per_key.items())}"
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

        # The render window is bounded in wall clock, not only in quiet
        # ticks. Its countdown re-arms whenever the task queues are non-empty,
        # which a producer at the loop's own rate holds forever. The
        # producer's own paints keep landing, because that traffic is
        # interactive by design and the gate never touches it.
        stop_producer = threading.Event()
        fills = itertools.count(11)

        def produce():
            while not stop_producer.is_set():
                # A fresh fill every frame. Identical payloads are
                # dedup-skipped at the enqueue point and would not keep the
                # queue non-empty, which is the mechanism under test.
                media_player.add_image_task(0, fixtures.make_native_image(fill=next(fills) % 251))
                stop_producer.wait(1 / media_player.FPS)

        producer = threading.Thread(target=produce, name="GateProducer", daemon=True)
        producer.start()
        try:
            window_before = media_player.gate_window_ticks
            controller.load_page(page_a, allow_reload=True)
            assert fixtures.wait_until(
                lambda: media_player.gate_window_ticks > window_before, timeout=3
            ), "the page change never opened the render window at all"

            # The window opens on the tick that observes the new generation
            # and must close GATE_WINDOW_MAX_S later. The timeout allows slack
            # for the observation and for a loaded machine.
            assert fixtures.wait_until(
                lambda: _window_closed(media_player, for_s=0.25), timeout=3
            ), (
                "the render window never closed while a full-rate producer kept "
                "the task queues non-empty -- the gate is doing nothing at all "
                "for the entire away window"
            )

            deck.clear_journal()
            window_before = media_player.gate_window_ticks
            gated_before = media_player.gated_ticks
            time.sleep(OBSERVE_S)
            assert media_player.gate_window_ticks == window_before, (
                "the render window re-opened under producer traffic"
            )
            assert media_player.gated_ticks > gated_before, (
                "the loop is not gating again after the window closed"
            )
            # Animation is off but the producer is not. Every write in that
            # observation is the producer's own key-0 paint.
            foreign = [e for e in animation_writes(deck) if e[3] != "key:0"]
            assert not foreign, (
                f"{len(foreign)} animation write(s) beyond the producer's own key "
                f"while gated: {[(e[2], e[3]) for e in foreign[:5]]}"
            )
            assert [e for e in deck.journal() if e[3] == "key:0"], (
                "the producer's paints stopped landing -- the deadline gated "
                "interactive traffic, which it must never do"
            )
        finally:
            stop_producer.set()
            producer.join(timeout=2)
        print("PASS: the render window closes on its deadline under a full-rate producer")

        # A restored presence brings animation back within the bound.
        deck.clear_journal()
        gl.screen_locked = False
        started = time.monotonic()
        monitor.on_lock_changed(False)
        assert fixtures.wait_until(
            lambda: bool(key_writes(deck)), timeout=0.5, interval=0.005
        ), "animation did not resume within 500ms of presence being restored"
        print(f"PASS: animation resumed {1000 * (time.monotonic() - started):.0f}ms "
              f"after presence restore")

        wait_for_playback(deck, "page A after resume")
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
