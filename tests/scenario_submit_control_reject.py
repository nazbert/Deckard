"""
Unit-tier scenario (bug 12): MediaPlayerThread.submit_control must silently
reject messages once the writer has been stopped by a terminal ClearAndClose --
nothing will ever drain the control queue again, so accepting more would grow
it unbounded for the rest of the process's life.

Split out of scenario_deck_close.py (#69 tier-mixing guard): that scenario is
integration-tier (make_headless_controller) but this one sub-test is unit-tier
(make_stub_controller). Mixing tiers in one process is now refused by the
install_stub_globals / _install_integration_globals guard, so this check lives
in its own subprocess where the unit tier is the only tier.
"""
import fixtures


def test_submit_control_rejected_after_stop() -> None:
    from src.backend.DeckManagement.DeckController import ClearAndCloseMsg, SetBrightnessMsg

    controller, media_player, _ = fixtures.make_stub_controller(serial="submit-reject-1")

    # Sanity: a normal submission enqueues (the thread is never started at
    # the unit tier -- see fixtures.make_stub_controller -- so nothing
    # drains this automatically).
    media_player.submit_control(SetBrightnessMsg(50))
    assert len(media_player.control_q) == 1, "fixture sanity: submit_control should enqueue before stop"
    media_player.control_q.clear()

    # Drive the terminal message through drain_control_queue directly (unit
    # tier convention: the thread is never started, so we call the loop body
    # by hand -- see fixtures.py's docstring and drain_control_queue's own).
    media_player.submit_control(ClearAndCloseMsg())
    still_running = media_player.drain_control_queue()
    assert still_running is False, "ClearAndCloseMsg must signal the caller to stop the loop"
    assert media_player._stop is True, "_exec_clear_and_close must set _stop itself (not just rely on stop())"

    # Post-stop: further submissions must be silently rejected (bug 12) --
    # nothing will ever drain this queue again, so accepting more would grow
    # it unbounded for the rest of the process's life.
    media_player.submit_control(SetBrightnessMsg(75))
    assert len(media_player.control_q) == 0, "submit_control after stop must be a no-op"

    print("PASS: submit_control rejects messages once the writer is stopped")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_submit_control_reject")
    test_submit_control_rejected_after_stop()
    print("PASS: scenario_submit_control_reject")


if __name__ == "__main__":
    main()
