"""
Integration scenario (docs/presenter-migration-plan.md §7 "Shutdown during
active video (both quit paths)", M1): DeckManager.close_all() now submits a
terminal ClearAndClose control message per controller and joins each media
thread with a bounded (2s) timeout (plan §2.4), instead of writing clear+
close directly from the calling thread.

Checks:
  * the journal ends with the blank writes (every key + touchscreen)
    followed immediately by close(), nothing landing after;
  * the media thread actually exits within the bound;
  * delete() called afterwards returns fast -- media_player.stop()'s poll on
    an already-exited thread is a no-op, not a fresh wait.
"""
import time

import fixtures
import globals as gl


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_shutdown_clearclose")
    controller = fixtures.make_headless_controller(serial="shutdown-cc-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()
    is_touch = controller.deck.is_touch()

    # Let the boot paint settle before measuring the shutdown sequence.
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)
    deck.clear_journal()

    start = time.monotonic()
    gl.deck_manager.close_all()
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"close_all() took too long: {elapsed:.2f}s (bound is 2s + slack)"

    assert fixtures.wait_until(lambda: not controller.media_player.is_alive(), timeout=2.0), (
        "media thread did not exit within the bounded join"
    )

    journal = deck.journal()
    expected_clear_ops = key_count + (1 if is_touch else 0)
    assert len(journal) == expected_clear_ops + 1, (
        f"expected {expected_clear_ops} blank writes + 1 close, got {len(journal)}: {journal}"
    )

    clear_part = journal[:expected_clear_ops]
    close_part = journal[expected_clear_ops:]

    for k in range(key_count):
        assert clear_part[k][2] == "set_key_image" and clear_part[k][3] == f"key:{k}", (
            f"expected key {k}'s blank write at position {k}, got {clear_part[k]}"
        )
    if is_touch:
        assert clear_part[key_count][2] == "set_touchscreen_image", (
            f"expected the touchscreen blank write last among the blanks, got {clear_part[key_count]}"
        )

    assert len(close_part) == 1 and close_part[0][2] == "close", (
        f"expected the journal to end with exactly one close(), got {close_part}"
    )
    assert journal[-1][2] == "close", "nothing may land after close()"

    # delete() afterwards must return fast: media_player.stop() polls
    # `running`, which is already False (plan §2.4) -- not a fresh 2s wait.
    t0 = time.monotonic()
    controller.keep_actions_ticking = False
    controller.delete()
    delete_elapsed = time.monotonic() - t0
    # Liveness ceiling: media_player.stop() polls `running`, already False after
    # close_all() above, so delete() must NOT incur a fresh 2s stop wait -- it
    # returns fast (~ms). 1.5s stays cleanly below the 2s stop timeout (so it
    # still catches "it took a full fresh stop wait") while giving a loaded CI
    # runner 50% more headroom than the original 1.0s (#69 flake hardening).
    assert delete_elapsed < 1.5, f"delete() took too long after shutdown: {delete_elapsed:.2f}s"

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    tick_thread = getattr(controller, "tick_thread", None)
    if tick_thread is not None:
        tick_thread.join(timeout=2.0)

    print("PASS: scenario_shutdown_clearclose")


if __name__ == "__main__":
    main()
