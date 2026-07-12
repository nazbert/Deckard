"""
Scenario for issue #1 (B-01) — first leg of #61: the sole-writer media
thread must survive render-path exceptions.

Before the fix, run() had no guard around the loop body (the commented-out
@log.catch would have logged once and let the thread DIE); any uncaught
exception from a tick — screensaver input-swap KeyError, background-video
None race, composite failure — permanently froze the deck: no paints, no
brightness, no Clear, close() only via timeout.

Three legs, each over its own stub controller + REAL MediaPlayerThread
(fixtures constructs it unstarted):

  guard survival (the original leg):
    1. poisons one tick via _needs_key_ticks -> thread survives, and a paint
       submitted afterwards lands on the FaultyFakeDeck journal;
    2. poisons persistently -> the guard's local rate limiter emits at most
       one traceback record per 5s window (plus suppression summary), not
       one per ~4Hz retry;
    3. while STILL raising every tick, stop() joins cleanly (the guard's
       except path honors _stop -- a failing body must not strand close()).

  batch recovery (review round 1, MEDIUM 1): a non-TransportError from one
  key's device write mid-batch loses the batch's SIBLING frames too (the
  tick already popped image_tasks) -- the guard's except path must arm
  _schedule_full_repaint() so the surviving keys eventually repaint, not
  keep stale imagery silently forever.

  control drain (review round 1, MEDIUM 2): the control-queue drain must run
  FIRST in the tick, before anything that can raise into the guard -- a
  persistent pre-drain failure (here: a poisoned check_resume_gap) must not
  starve SetBrightnessMsg, and a terminal ClearAndCloseMsg must still blank
  + close the deck and stop the loop.

Vector regressions for (a) build-then-swap live in the existing screensaver
scenarios; this file owns the guard's contract.
"""
import fixtures  # must be first: isolates DATA_PATH before any src import

import threading
import time

from loguru import logger

from src.backend.DeckManagement.DeckController import (
    ClearAndCloseMsg,
    Input,
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

    # Poison the tick path: _needs_key_ticks is called every iteration's
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
        # 1. The poisoned ticks must fire AND the thread must survive them.
        assert wait_until(lambda: poison["count"] >= 2), "poisoned tick never ran"
        assert media_player.is_alive(), "writer thread died on a tick exception (the B-01 freeze)"
        assert wait_until(lambda: any("boom-tick" in r for r in records)), (
            "the tick exception must be logged with its message"
        )
        assert any('raise RuntimeError("boom-tick")' in r for r in records), (
            "the log record must carry the full traceback, not just the message"
        )

        # 2. Rate limiter: at ~4 retries/s a 1.2s window sees ~5 failures but
        # must log at most one full record per 5s window.
        records.clear()
        time.sleep(1.2)
        full_records = sum("boom-tick" in r for r in records)
        assert full_records <= 1, (
            f"rate limiter must cap traceback records at 1 per 5s window, got {full_records}"
        )
        assert media_player.is_alive(), "writer must still be alive under persistent failure"

        # 3. Writes resume once the failure clears: submit a paint, assert it
        # lands on the device journal.
        poison["active"] = False
        img = fixtures.make_native_image(fill=7)
        media_player.add_image_task(0, img, page=page, config_gen=gen)
        assert wait_until(lambda: deck.last_op_for("key:0") is not None), (
            "a paint submitted after the failure burst must land on the device"
        )
        assert deck.last_op_for("key:0")[2] == "set_key_image"

        # 4. stop() must join cleanly WHILE the body is raising every tick.
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
        # Belt-and-braces: never leave the writer running on a failed assert.
        poison["active"] = False
        media_player._stop = True
        media_player._wake_event.set()
        media_player.join(timeout=3)
        logger.remove(sink_id)

    print("  leg PASS: guard survival")


def leg_batch_recovery() -> None:
    """Review round 1, MEDIUM 1: a caught tick exception mid-batch must not
    silently strand the batch's sibling frames. perform_media_player_tasks
    pops image_tasks BEFORE running them, so when key 1's write raises a
    non-TransportError (only TransportError is handled at the task level),
    key 2's already-popped frame is gone -- the guard's except path must arm
    the pending full repaint so key 2 still paints."""
    controller, media_player, deck_manager = fixtures.make_stub_controller(n_keys=3)
    deck = controller.deck
    page = controller.active_page
    gen = controller._page_load_generation

    # Poison exactly ONE write to key 1 with a non-TransportError (the task
    # classes catch TransportError; anything else escapes into the guard).
    real_set_key_image = deck.set_key_image
    poison = {"armed": True, "hits": 0}

    def poisoned_set_key_image(key, image):
        if poison["armed"] and key == 1:
            poison["armed"] = False
            poison["hits"] += 1
            raise TypeError("boom-batch-key1")
        return real_set_key_image(key, image)

    deck.set_key_image = poisoned_set_key_image

    # Queue the whole multi-key batch BEFORE the loop starts so one tick
    # drains it as a single perform_media_player_tasks batch (0 -> 1 -> 2 in
    # dict insertion order: key 0 lands, key 1 raises, key 2 is dropped).
    for i in range(3):
        media_player.add_image_task(
            i, fixtures.make_native_image(fill=10 + i), page=page, config_gen=gen)

    media_player.start()
    try:
        assert wait_until(lambda: poison["hits"] >= 1), "poisoned key-1 write never ran"
        assert media_player.is_alive(), (
            "writer thread died on a mid-batch non-TransportError"
        )
        # The recovery contract: the guard scheduled a full repaint, and the
        # repaint's re-enqueue painted the dropped sibling. Nothing else can
        # repaint key 2 here -- its task was popped with the failed batch,
        # and the stub's inputs are quiet (no animation ticks).
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
    """Review round 1, MEDIUM 2: the control-queue drain must run before
    anything in the tick that can raise. A persistently failing pre-drain
    stage (poisoned check_resume_gap -- in the pre-fix order it ran ahead of
    the drain) must not starve SetBrightnessMsg, and the terminal
    ClearAndCloseMsg must still blank + close the deck and stop the loop."""
    controller, media_player, deck_manager = fixtures.make_stub_controller(n_keys=2)
    deck = controller.deck

    calls = {"count": 0}

    def poisoned_check_resume_gap(now=None):
        calls["count"] += 1
        raise RuntimeError("boom-pre-drain")

    media_player.check_resume_gap = poisoned_check_resume_gap

    media_player.start()
    try:
        # Every tick raises right after the drain; the poison must be live...
        assert wait_until(lambda: calls["count"] >= 1), "pre-drain poison never ran"
        assert media_player.is_alive(), "writer must survive the persistent tick failure"

        # ...and control messages must still execute (drain runs FIRST,
        # unconditionally -- before any stage that can raise into the guard).
        media_player.submit_control(SetBrightnessMsg(value=42))
        assert wait_until(lambda: deck.last_op_for("brightness") is not None, timeout=3.0), (
            "SetBrightnessMsg starved: a persistent pre-drain failure must not "
            "keep the control queue from draining (drain must run first)"
        )
        assert deck.last_op_for("brightness")[2] == "set_brightness"

        # The terminal message is the quit path: still under persistent
        # failure, ClearAndCloseMsg must blank + close the device and stop
        # the loop -- this is exactly the "deck not blanked/closed on quit"
        # starvation the review confirmed.
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
