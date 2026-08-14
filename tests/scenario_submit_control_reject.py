"""
Unit-tier scenario for submit_control after a terminal ClearAndClose.

MediaPlayerThread.submit_control must silently reject a message once the
writer is stopped, because nothing drains the control queue again and the
queue would otherwise grow for the life of the process. This is unit tier, so
it runs in a subprocess where the unit tier is the only tier.
"""
import fixtures


def test_submit_control_rejected_after_stop() -> None:
    from src.backend.DeckManagement.DeckController import ClearAndCloseMsg, SetBrightnessMsg

    controller, media_player, _ = fixtures.make_stub_controller(serial="submit-reject-1")

    # A normal submission enqueues. The thread never starts at the unit
    # tier, so nothing drains this automatically.
    media_player.submit_control(SetBrightnessMsg(50))
    assert len(media_player.control_q) == 1, "fixture sanity: submit_control should enqueue before stop"
    media_player.control_q.clear()

    # Drive the terminal message through drain_control_queue directly. At
    # the unit tier the thread never starts, so the loop body is called by
    # hand.
    media_player.submit_control(ClearAndCloseMsg())
    still_running = media_player.drain_control_queue()
    assert still_running is False, "ClearAndCloseMsg must signal the caller to stop the loop"
    assert media_player._stop is True, "_exec_clear_and_close must set _stop itself (not just rely on stop())"

    # After the stop, a further submission must be rejected silently.
    # Nothing drains this queue again, so accepting more grows it without
    # bound.
    media_player.submit_control(SetBrightnessMsg(75))
    assert len(media_player.control_q) == 0, "submit_control after stop must be a no-op"

    print("PASS: submit_control rejects messages once the writer is stopped")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_submit_control_reject")
    test_submit_control_rejected_after_stop()
    print("PASS: scenario_submit_control_reject")


if __name__ == "__main__":
    main()
