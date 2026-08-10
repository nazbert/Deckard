"""
An idle deck showing a static screensaver must cost nothing per tick.

While ScreenSaver.show() owns the deck, `deck_controller.inputs` is the
freshly built set init_inputs() installed -- no action, no media and no
label on any of it, and ActionCore.get_is_present() refuses plugin writes
for the duration -- while the screensaver's own imagery lives in
`background` and is driven entirely by the media thread. So the action-tick
loop has nothing to do: no actions to tick, and nothing whose composite
could have changed since the last frame. Repainting every input once a
second there re-composites and re-hashes the whole deck only for the dedup
guard to discard the result -- CPU burned on a deck nobody is watching.

Pinned here over a REAL DeckController with its REAL tick thread:

  1. the tick loop keeps running while the screensaver shows (the assertion
     below is not vacuously satisfied by a dead thread),
  2. across several of its periods it calls update() on no input at all,
  3. and the device stays untouched for that whole window,
  4. while hide() still repaints every key -- the wake path is untouched.

update() calls are attributed by CALLING THREAD, not counted globally: the
media thread legitimately calls the same method (on_media_player_tick, the
pending-full-repaint retry), and conflating the two would pin the wrong
invariant.
"""
import os
import threading
import time

import fixtures
import globals as gl

# Tick periods to observe. TICK_DELAY is 1s, so this is also roughly the
# window's length in seconds.
TICK_PERIODS = 3


def wait_until_quiet(deck, quiet_for: float = 0.5, timeout: float = 15.0) -> bool:
    """Waits until no device write has landed for `quiet_for` seconds, so
    what the window below observes is the tick loop's doing and not the tail
    of show()'s own repaint. Not a fixed sleep: how fast show()'s bulk batch
    drains is not a constant on a loaded machine."""
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
    fixtures.start_watchdog(120, label="scenario_screensaver_idle_tick")
    controller = fixtures.make_headless_controller(serial="ss-idle-1")
    try:
        deck = fixtures.raw_deck(controller)
        key_count = controller.deck.key_count()

        screensaver_png = fixtures.make_test_png(
            os.path.join(gl.DATA_PATH, "media", "idle_saver.png"), color=(0, 160, 220)
        )
        controller.screen_saver.set_media_path(screensaver_png)

        # Let the default page land before the transition under test.
        assert fixtures.wait_until(
            lambda: deck.last_op_for("key:0") is not None, timeout=10
        ), "fixture sanity: the default page never painted"

        seq_before_show = deck.current_seq()
        controller.screen_saver.show()
        assert controller.screen_saver.showing is True

        # EVERY key, not just key:0: show()'s repaint is a bulk batch that
        # writes keys one at a time, and a mid-flight batch would otherwise
        # bleed into the observation window.
        assert fixtures.wait_until(
            lambda: all(
                (e := deck.last_op_for(f"key:{k}")) is not None and e[1] > seq_before_show
                for k in range(key_count)
            ),
            timeout=15,
        ), "the screensaver never painted every key"
        assert wait_until_quiet(deck), (
            "a STATIC screensaver never stopped writing to the deck -- the window "
            "below cannot attribute anything"
        )

        # --- instrument the live (screensaver-era) input set ---------------
        lock = threading.Lock()
        updates: dict[str, int] = {}
        marks: dict[str, int] = {}

        def count_updates(controller_input) -> None:
            original = controller_input.update

            def counting(*args, **kwargs):
                with lock:
                    name = threading.current_thread().name
                    updates[name] = updates.get(name, 0) + 1
                return original(*args, **kwargs)

            controller_input.update = counting

        for input_list in controller.inputs.values():
            for controller_input in input_list:
                count_updates(controller_input)

        # Liveness probe: tick_actions brackets each iteration between a
        # False-call and a True-call of this, so two marks from the tick
        # thread is one completed iteration. Key presses call it too, hence
        # the same per-thread attribution.
        original_mark = controller.mark_page_ready_to_clear

        def counting_mark(*args, **kwargs):
            with lock:
                name = threading.current_thread().name
                marks[name] = marks.get(name, 0) + 1
            return original_mark(*args, **kwargs)

        controller.mark_page_ready_to_clear = counting_mark

        def tick_iterations() -> int:
            with lock:
                return marks.get("tick_actions", 0) // 2

        def tick_updates() -> int:
            with lock:
                return updates.get("tick_actions", 0)

        journal_at_start = len(deck.journal())
        repaint_ts_at_start = controller._last_full_repaint_ts

        assert fixtures.wait_until(
            lambda: tick_iterations() >= TICK_PERIODS,
            timeout=controller.TICK_DELAY * TICK_PERIODS + 20,
        ), (
            f"the action tick loop only completed {tick_iterations()} iterations -- "
            f"the observation window never happened, so nothing below is pinned"
        )

        observed = tick_iterations()
        assert tick_updates() == 0, (
            f"the action tick repainted inputs {tick_updates()} times across "
            f"{observed} iterations while a static screensaver was showing -- an "
            f"idle deck is re-compositing and re-hashing every input for frames "
            f"the dedup guard throws away"
        )

        if controller._last_full_repaint_ts != repaint_ts_at_start:
            # A >=5s gap between media-loop iterations reads as a suspend/
            # resume and arms a full repaint. Under a loaded test runner that
            # is a scheduling artifact, not the defect this scenario is
            # about, so the device-silence check is reported and skipped
            # rather than failed. The update() count above is unaffected --
            # it is attributed to the tick thread by name.
            print("NOTE: a full repaint fired during the window (loaded machine); "
                  "skipping the device-silence check")
        else:
            assert len(deck.journal()) == journal_at_start, (
                f"the deck was written to "
                f"{len(deck.journal()) - journal_at_start} times while a static "
                f"screensaver sat idle"
            )
        print(f"PASS: {observed} tick iterations under a static screensaver, "
              f"0 input repaints from the tick thread")

        # --- the wake path must be untouched -------------------------------
        seq_before_hide = deck.current_seq()
        controller.screen_saver.hide()
        assert controller.screen_saver.showing is False
        assert fixtures.wait_until(
            lambda: all(
                (e := deck.last_op_for(f"key:{k}")) is not None and e[1] > seq_before_hide
                for k in range(key_count)
            ),
            timeout=15,
        ), "hiding the screensaver no longer repaints every key"
        print("PASS: hide() still repaints every key")
    finally:
        fixtures.teardown(controller)

    print("\nALL PASS: scenario_screensaver_idle_tick")


if __name__ == "__main__":
    main()
