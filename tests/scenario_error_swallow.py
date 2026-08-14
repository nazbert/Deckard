"""A burst of write failures must be swallowed, and writes must resume.

No controller removal and no reconnect. Every failure arms the pending full
repaint, which _run_pending_repaint fires once per burst on a 2 s cadence.
"""
import fixtures


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_error_swallow")
    controller, media_player, deck_manager = fixtures.make_stub_controller(n_keys=3)
    deck = controller.deck
    page = controller.active_page
    gen = controller._page_load_generation

    deck.fail_next("set_key_image", 3)

    for i in range(3):
        img = fixtures.make_native_image(fill=i)
        media_player.add_image_task(0, img, page=page, config_gen=gen)
        media_player.perform_media_player_tasks()

    assert deck.last_op_for("key:0") is None, "a failed write must never journal"
    assert len(deck_manager.remove_calls) == 0, (
        "TransportErrors must never remove the controller -- removal comes "
        "solely from USB disconnect events now (plan §9.1)"
    )
    assert deck_manager.connect_calls == 0, "a swallowed TransportError must never reconnect"
    assert controller._had_write_failure, "the failure must be recorded"
    assert controller._full_repaint_pending, (
        "every write failure must arm the pending repaint (plan §4 M2)"
    )
    assert controller.repaint_count == 0, "arming must not fire a repaint synchronously"

    deck.clear_journal()

    # With the failures exhausted, the next write lands normally and clears the
    # failure flag, and the armed repaint fires through the loop hook.
    img = fixtures.make_native_image(fill=9)
    media_player.add_image_task(0, img, page=page, config_gen=gen)
    media_player.perform_media_player_tasks()

    landed = deck.last_op_for("key:0")
    assert landed is not None, "writes must resume once injected failures are exhausted"
    assert landed[2] == "set_key_image"
    assert not controller._had_write_failure, "a successful write must clear the failure flag"

    controller._last_full_repaint_ts = 0.0  # open the 2s window deterministically
    assert controller._run_pending_repaint(), "the armed recovery repaint must fire"
    assert controller.repaint_count == 1
    media_player.perform_media_player_tasks()  # flush the repaint's enqueued tasks

    written_keys = {e[3] for e in deck.ops_by_name("set_key_image")}
    assert written_keys == {f"key:{i}" for i in range(3)}, (
        f"every key must be rewritten by the recovery repaint, got {written_keys}"
    )

    # One failure burst gives one repaint, so nothing further is pending.
    assert not controller._full_repaint_pending
    assert not controller._run_pending_repaint(), "no second repaint for the same burst"
    assert controller.repaint_count == 1

    print("PASS: scenario_error_swallow")


if __name__ == "__main__":
    main()
