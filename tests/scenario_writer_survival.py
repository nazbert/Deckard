"""
The sole-writer media thread must survive a render-path exception.

A guard around the loop body keeps the thread alive, rate-limits its
tracebacks, and still honors stop(). A failed device write mid-batch arms a
full repaint, so the surviving keys repaint.
"""

# The control-queue drain runs first in the tick, so a stage that keeps
# failing ahead of it starves no control message.
import fixtures  # must be first, to isolate DATA_PATH before any src import

import threading
import time

from loguru import logger

from src.backend.DeckManagement.DeckController import (
    ClearAndCloseMsg,
    SetBrightnessMsg,
)


def wait_until(pred, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def leg_guard_survival() -> None:
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="TRACE")

    controller, media_player, deck_manager = fixtures.make_stub_controller(n_keys=3)
    deck = controller.deck
    page = controller.active_page
    gen = controller._page_load_generation

    # Poison the tick path. _needs_key_ticks runs in every iteration's
    # animated-content check, inside the guarded body.
    poison = {"count": 0, "active": True}
    real_needs = media_player._needs_key_ticks

    def poisoned_needs():
        if poison["active"]:
            poison["count"] += 1
            raise RuntimeError("boom-tick")
        return real_needs()

    media_player._needs_key_ticks = poisoned_needs

    media_player.start()
    try:
        # 1. The poisoned ticks must fire and the thread must survive them.
        assert wait_until(lambda: poison["count"] >= 2), "poisoned tick never ran"
        assert media_player.is_alive(), "writer thread died on a tick exception (the B-01 freeze)"
        assert wait_until(lambda: any("boom-tick" in r for r in records)), (
            "the tick exception must be logged with its message"
        )
        assert any('raise RuntimeError("boom-tick")' in r for r in records), (
            "the log record must carry the full traceback, not just the message"
        )

        # 2. The rate limiter. At about 4 retries a second, a 1.2s window
        # sees about 5 failures and must log at most one record per 5s.
        records.clear()
        time.sleep(1.2)
        full_records = sum("boom-tick" in r for r in records)
        assert full_records <= 1, (
            f"rate limiter must cap traceback records at 1 per 5s window, got {full_records}"
        )
        assert media_player.is_alive(), "writer must still be alive under persistent failure"

        # 3. Writes resume once the failure clears. Submit a paint, assert it
        # lands on the device journal.
        poison["active"] = False
        img = fixtures.make_native_image(fill=7)
        media_player.add_image_task(0, img, page=page, config_gen=gen)
        assert wait_until(lambda: deck.last_op_for("key:0") is not None), (
            "a paint submitted after the failure burst must land on the device"
        )
        assert deck.last_op_for("key:0")[2] == "set_key_image"

        # 4. stop() must join cleanly while the body raises every tick.
        poison["active"] = True
        assert wait_until(lambda: poison["count"] >= 3), "poison did not re-engage"
        media_player.stop()
        media_player.join(timeout=5)
        assert not media_player.is_alive(), (
            "stop() must terminate the loop even when every tick raises "
            "(the guard's except path must honor _stop)"
        )
        assert not media_player.running, (
            "run() must leave running=False on exit (try/finally) -- a stale "
            "True makes every later stop() burn its full join timeout"
        )
    finally:
        # Never leave the writer running after a failed assert.
        poison["active"] = False
        media_player._stop = True
        media_player._wake_event.set()
        media_player.join(timeout=3)
        logger.remove(sink_id)

    print("  leg PASS: guard survival")


def leg_batch_recovery() -> None:
    """A caught tick exception mid-batch must not strand the batch's sibling
    frames.

    perform_media_player_tasks pops image_tasks before it runs them, so the
    guard's except path must arm the pending full repaint.
    """
    # Only a TransportError is handled at the task level, so when key 1's
    # write raises anything else, key 2's already-popped frame is gone.
    controller, media_player, deck_manager = fixtures.make_stub_controller(n_keys=3)
    deck = controller.deck
    page = controller.active_page
    gen = controller._page_load_generation

    # Poison exactly one write to key 1 with something other than a
    # TransportError, which the task classes catch. Anything else escapes
    # into the guard.
    real_set_key_image = deck.set_key_image
    poison = {"armed": True, "hits": 0}

    def poisoned_set_key_image(key, image):
        if poison["armed"] and key == 1:
            poison["armed"] = False
            poison["hits"] += 1
            raise TypeError("boom-batch-key1")
        return real_set_key_image(key, image)

    deck.set_key_image = poisoned_set_key_image

    # Queue the whole multi-key batch before the loop starts, so one tick
    # drains it as a single perform_media_player_tasks batch. In dict
    # insertion order key 0 lands, key 1 raises, and key 2 is dropped.
    for i in range(3):
        media_player.add_image_task(
            i, fixtures.make_native_image(fill=10 + i), page=page, config_gen=gen)

    media_player.start()
    try:
        assert wait_until(lambda: poison["hits"] >= 1), "poisoned key-1 write never ran"
        assert media_player.is_alive(), (
            "writer thread died on a mid-batch non-TransportError"
        )
        # The recovery contract. The guard scheduled a full repaint, and the
        # repaint's re-enqueue painted the dropped sibling. Nothing else can
        # repaint key 2 here, because its task was popped with the failed
        # batch and the stub's inputs run no animation tick.
        assert wait_until(lambda: deck.last_op_for("key:2") is not None, timeout=3.0), (
            "sibling frame dropped by the failed batch must be repainted via "
            "the guard's scheduled full repaint (except path must call "
            "_schedule_full_repaint)"
        )
        assert controller.repaint_count >= 1, (
            "the recovery must come from the pending-repaint mechanism"
        )
        assert deck.last_op_for("key:1") is not None, (
            "the failed key itself must also repaint once the fault clears"
        )
    finally:
        media_player._stop = True
        media_player._wake_event.set()
        media_player.join(timeout=3)

    assert not media_player.is_alive()
    print("  leg PASS: batch recovery (sibling frames repainted after a mid-batch exception)")


def leg_control_drain() -> None:
    """The control-queue drain must run before anything in the tick that can
    raise.

    A stage that keeps failing must not starve SetBrightnessMsg, and the
    terminal ClearAndCloseMsg must still blank and close the deck.
    """
    # A poisoned check_resume_gap stands in for that stage, because an order
    # that ran it ahead of the drain is what starves the control queue.
    controller, media_player, deck_manager = fixtures.make_stub_controller(n_keys=2)
    deck = controller.deck

    calls = {"count": 0}

    def poisoned_check_resume_gap(now=None):
        calls["count"] += 1
        raise RuntimeError("boom-pre-drain")

    media_player.check_resume_gap = poisoned_check_resume_gap

    media_player.start()
    try:
        # Every tick raises right after the drain, so the poison is live.
        assert wait_until(lambda: calls["count"] >= 1), "pre-drain poison never ran"
        assert media_player.is_alive(), "writer must survive the persistent tick failure"

        # A control message must still execute, because the drain runs first
        # and unconditionally, ahead of any stage that can raise.
        media_player.submit_control(SetBrightnessMsg(value=42))
        assert wait_until(lambda: deck.last_op_for("brightness") is not None, timeout=3.0), (
            "SetBrightnessMsg starved: a persistent pre-drain failure must not "
            "keep the control queue from draining (drain must run first)"
        )
        assert deck.last_op_for("brightness")[2] == "set_brightness"

        # The terminal message is the quit path. Under a persistent failure
        # ClearAndCloseMsg must still blank the device, close it and stop the
        # loop, or a quit leaves the deck lit and open.
        media_player.submit_control(ClearAndCloseMsg())
        assert wait_until(lambda: deck.last_op_for("device") is not None, timeout=3.0), (
            "ClearAndCloseMsg starved: the deck was never closed"
        )
        assert deck.last_op_for("device")[2] == "close"
        assert wait_until(lambda: not media_player.is_alive(), timeout=3.0), (
            "the loop must stop after the terminal message"
        )
    finally:
        media_player._stop = True
        media_player._wake_event.set()
        media_player.join(timeout=3)

    print("  leg PASS: control drain (brightness + terminal close land under persistent tick failure)")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_writer_survival")
    leg_guard_survival()
    leg_batch_recovery()
    leg_control_drain()

    # The guarded loop must not have leaked threads.
    stray = [t.name for t in threading.enumerate()
             if t is not threading.current_thread() and t.is_alive() and t.daemon is False]
    assert not stray, f"non-daemon threads left running: {stray}"

    print("PASS: scenario_writer_survival")


if __name__ == "__main__":
    main()
