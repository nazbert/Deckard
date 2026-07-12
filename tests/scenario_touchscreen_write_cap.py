"""
Regression test for issue #52 (item: uncapped touchscreen writes).

_video_write_hz used to gate ONLY background.video repaints. Dial-state
videos and scrolling labels re-render the shared touchscreen from the
media tick at loop FPS (30Hz), so the same HID-starvation vector the cap
was built for (a back-to-back write flood out-racing the 20Hz HID read
poll on the transport's single mutex -- see the field-verified
dial-input-starvation fix) survived via a different content type.

The fix rate-caps ALL touchscreen writes at the write point in
perform_media_player_tasks, sharing the _video_write_hz budget with
latest-wins semantics: an over-budget frame goes back into the single
task slot and the next iteration writes the freshest composite --
content is delayed by at most one budget window, never lost.

Unit tier: the media thread is never started; the scenario drives
perform_media_player_tasks() directly, so write counts are deterministic
up to wall-clock elapsed (asserted against the measured window with
margin, not a fixed count).
"""
import time

import fixtures
from faulty_fake_deck import _hash_bytes


def flood(media_player, controller, touch, n_frames: int, spacing_s: float) -> float:
    """Enqueues n_frames distinct touchscreen frames, calling the writer's
    drain after each, spaced ~spacing_s apart. Returns the elapsed time."""
    start = time.time()
    for i in range(n_frames):
        payload = bytes([i % 256]) * 64
        media_player.add_touchscreen_task(
            payload,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            controller_touchscreen=touch,
            img_hash=hash(payload),
        )
        media_player.perform_media_player_tasks()
        time.sleep(spacing_s)
    return time.time() - start


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_touchscreen_write_cap")
    from src.backend.DeckManagement.InputIdentifier import Input

    controller, media_player, _ = fixtures.make_stub_controller(
        serial="tscap-1", has_touchscreen=True
    )
    deck = controller.deck
    touch = controller.inputs[Input.Touchscreen][0]
    hz = media_player._video_write_hz
    assert hz == 20.0, f"fixture sanity: expected the default 20Hz budget, got {hz}"

    # --- 1. A tick-rate flood must be capped to the _video_write_hz budget. ---
    deck.clear_journal()
    elapsed = flood(media_player, controller, touch, n_frames=40, spacing_s=0.005)
    writes = deck.ops_by_name("set_touchscreen_image")
    budget = elapsed * hz + 3  # +1 leading edge, +2 sleep-jitter margin
    assert len(writes) >= 1, "at least one write must land (first frame is never deferred)"
    assert len(writes) <= budget, (
        f"{len(writes)} touchscreen writes in {elapsed:.3f}s exceeds the "
        f"{hz}Hz budget (allowed ~{budget:.1f}) -- the flood is uncapped, the "
        f"HID-starvation vector is open"
    )
    print(f"PASS: flood capped ({len(writes)} writes in {elapsed:.3f}s @ {hz}Hz budget)")

    # --- 2. Latest-wins: a deferred frame is delayed, never lost. ---
    final_payload = b"\xab" * 64
    media_player.add_touchscreen_task(
        final_payload,
        page=controller.active_page,
        config_gen=controller._page_load_generation,
        controller_touchscreen=touch,
        img_hash=hash(final_payload),
    )
    media_player.perform_media_player_tasks()  # may defer (inside the budget window)
    time.sleep(1.0 / hz + 0.02)
    media_player.perform_media_player_tasks()  # must land now
    last = deck.last_op_for("touchscreen")
    assert last is not None and last[4] == _hash_bytes(final_payload), (
        "the final deferred frame never landed -- latest-wins deferral lost content"
    )
    print("PASS: deferred frame landed after the budget window (nothing lost)")

    # --- 3. Budget 0 disables the cap (documented contract of the knob). ---
    media_player._video_write_hz = 0
    deck.clear_journal()
    flood(media_player, controller, touch, n_frames=10, spacing_s=0.0)
    writes = deck.ops_by_name("set_touchscreen_image")
    assert len(writes) == 10, (
        f"with the cap disabled (hz=0) every frame must be written, got {len(writes)}/10"
    )
    print("PASS: hz=0 disables the cap")

    print("PASS: scenario_touchscreen_write_cap")


if __name__ == "__main__":
    main()
