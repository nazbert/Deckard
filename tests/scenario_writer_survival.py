"""
Scenario for issue #1 (B-01) — first leg of #61: the sole-writer media
thread must survive render-path exceptions.

Before the fix, run() had no guard around the loop body (the commented-out
@log.catch would have logged once and let the thread DIE); any uncaught
exception from a tick — screensaver input-swap KeyError, background-video
None race, composite failure — permanently froze the deck: no paints, no
brightness, no Clear, close() only via timeout.

Runs the REAL MediaPlayerThread (fixtures constructs it unstarted) and:
  1. poisons one tick via _needs_key_ticks -> thread survives, and a paint
     submitted afterwards lands on the FaultyFakeDeck journal;
  2. poisons persistently -> the guard's local rate limiter emits at most
     one traceback record per 5s window (plus suppression summary), not one
     per ~4Hz retry;
  3. while STILL raising every tick, stop() joins cleanly (the guard's
     except path honors _stop -- a failing body must not strand close()).

Vector regressions for (a) build-then-swap live in the existing screensaver
scenarios; this file owns the guard's contract.
"""
import fixtures  # must be first: isolates DATA_PATH before any src import

import threading
import time

from loguru import logger

from src.backend.DeckManagement.DeckController import Input


def wait_until(pred, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def main() -> None:
    records: list[str] = []
    logger.add(lambda m: records.append(str(m)), level="TRACE")

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
    finally:
        # Belt-and-braces: never leave the writer running on a failed assert.
        poison["active"] = False
        media_player._stop = True
        media_player._wake_event.set()
        media_player.join(timeout=3)

    # The guarded loop must not have leaked threads.
    stray = [t.name for t in threading.enumerate()
             if t is not threading.current_thread() and t.is_alive() and t.daemon is False]
    assert not stray, f"non-daemon threads left running: {stray}"

    print("PASS: scenario_writer_survival")


if __name__ == "__main__":
    main()
