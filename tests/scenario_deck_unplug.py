"""
Scenario: deck unplug/replug mid-render (issue #59).

The hardware-verified backend-reap / close-teardown fixes (commit 08aae662
"opaque-key initial paint, bounded close teardown, close-vs-load bg leak"
and the M2 graduated write-error policy) had NO regression net: the stock
FakeDeck's is_open()/connected() are hard-wired True, so a post-close write
silently succeeded and an unplug was inexpressible. FaultyFakeDeck now models
closed/unplugged states (close() -> _open False; simulate_unplug() -> both
False; strict lifecycle makes subsequent writes raise TransportError). This
scenario pins the production contracts that depend on those states:

  (a) lifecycle seam: a fresh deck is open+connected; close() flips is_open()
      and makes a later write raise TransportError; simulate_unplug() flips
      connected() and fails writes -- and strict mode is opt-out.

  (b) unplug mid-render: on a LIVE media writer thread, yanking the deck must
      NOT kill the writer. The task-level TransportError handler swallows the
      failed write (graduated error policy, plan §9.1) and arms the pending
      full repaint via _on_write_result(False); the thread stays alive. This
      is the sole-writer resilience the backend-reap fix relies on.

  (c) close() an unplugged deck: teardown must still complete -- step 5's
      ClearAndClose blank-frame writes fail harmlessly, and the fallback
      deck.close() (lifecycle-exempt) is a safe no-op. The controller
      deregisters and its threads exit even though the transport is gone.

  (d) unplug racing a page load on the load pool: removal (close) concurrent
      with a load_page dispatched onto the deck's load_executor must not
      deadlock or crash -- close() invalidates the generation and the load
      aborts / lands harmlessly, teardown completes.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading

import globals as gl
from fixtures import (
    FaultyFakeDeck,
    make_headless_controller,
    make_test_png,
    raw_deck,
    start_watchdog,
    wait_until,
)

from StreamDeck.Transport.Transport import TransportError
from src.backend.DeckManagement.InputIdentifier import Input


def test_lifecycle_seam() -> int:
    """The enabling fixture change: is_open()/connected() reflect real state
    and strict-mode writes past close()/unplug raise TransportError."""
    deck = FaultyFakeDeck(serial_number="unplug-seam")

    if not deck.is_open() or not deck.connected():
        print("FAIL(a): a fresh deck must report open + connected")
        return 1

    # A write on the live deck lands.
    deck.set_key_image(0, b"\x01" * 16)
    if deck.last_op_for("key:0") is None:
        print("FAIL(a): a write on the open deck did not land on the journal")
        return 1

    # close() releases the handle.
    deck.close()
    if deck.is_open():
        print("FAIL(a): is_open() must read False after close()")
        return 1
    # connected() stays True on a plain close (handle released, cable still in).
    if not deck.connected():
        print("FAIL(a): a plain close() must not flip connected()")
        return 1
    # A write after close raises (strict default) and does NOT journal.
    seq_before = deck.current_seq()
    try:
        deck.set_key_image(0, b"\x02" * 16)
        print("FAIL(a): a write after close() must raise TransportError")
        return 1
    except TransportError:
        pass
    if deck.current_seq() != seq_before:
        print("FAIL(a): a rejected post-close write must not journal")
        return 1

    # A second deck: unplug flips connected() and fails writes.
    deck2 = FaultyFakeDeck(serial_number="unplug-seam-2")
    deck2.simulate_unplug()
    if deck2.connected() or deck2.is_open():
        print("FAIL(a): simulate_unplug() must flip both connected() and is_open()")
        return 1
    try:
        deck2.set_touchscreen_image(b"\x03" * 16)
        print("FAIL(a): a write after simulate_unplug() must raise TransportError")
        return 1
    except TransportError:
        pass

    # Opt-out: lenient mode restores the old silent-journal behaviour.
    deck3 = FaultyFakeDeck(serial_number="unplug-seam-3")
    deck3.set_strict_lifecycle(False)
    deck3.close()
    deck3.set_key_image(0, b"\x04" * 16)  # must NOT raise
    if deck3.last_op_for("key:0") is None:
        print("FAIL(a): lenient mode must let a post-close write journal")
        return 1

    print("PASS: lifecycle seam -- open/connected reflect state; strict writes "
          "past close()/unplug raise; lenient opt-out works")
    return 0


def test_unplug_mid_render_survives() -> int:
    """Yanking the deck mid-render must NOT kill the sole writer: the write
    task's TransportError handler swallows the failed write and arms the
    pending repaint via _on_write_result(False) (graduated error policy, plan
    §9.1). This is the sole-writer resilience the backend-reap fix depends on.

    Two deterministic parts drive a write against the DEAD transport through
    the real task path (add_image_task -> the task's run() -> deck.set_key_
    image), which bypasses update_all_inputs's get_alive() short-circuit --
    the exact interleave of a paint already enqueued when the cable is yanked.

      b1 (drive-by-hand): the writer is stopped and its tick driven by hand,
          so the assertions can't race the live loop. Proves the failed write
          is swallowed (no exception out of perform_media_player_tasks), arms
          _had_write_failure, and lands NOTHING on the journal.
      b2 (live loop): on a fresh controller with the live writer running, the
          same enqueue-after-unplug must leave the thread ALIVE.
    """
    from src.backend.DeckManagement.DeckController import Input

    # ---- b1: deterministic drive-by-hand ---------------------------------- #
    controller = make_headless_controller(serial="unplug-live")
    try:
        deck = raw_deck(controller)
        media_player = controller.media_player

        # Land a real paint first so the journal has a pre-unplug baseline,
        # then quiesce the live writer and drive it by hand.
        if not wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3):
            print("SETUP-FAIL(b1): the writer never landed an initial key paint")
            return 1
        media_player.stop(timeout=3)
        media_player.perform_media_player_tasks()  # drain any leftover load tasks

        # Yank the cable. A write submitted now must fail at the transport.
        deck.simulate_unplug()
        seq_at_unplug = deck.current_seq()
        controller._had_write_failure = False

        key0 = controller.inputs[Input.Key][0]
        media_player.add_image_task(
            0, b"\x55" * 64,
            page=controller.active_page,
            config_gen=controller._page_load_generation,
            controller_key=key0, img_hash=5555,
        )
        # The task's run() attempts set_key_image on the dead transport. The
        # handler must swallow the TransportError -- this call must NOT raise.
        try:
            media_player.perform_media_player_tasks()
        except Exception as e:
            print(f"FAIL(b1): the failed write escaped the task handler and "
                  f"propagated out of perform_media_player_tasks: {e!r} -- "
                  f"the live loop would have to catch it in its guard, and "
                  f"the recovery arming would be skipped")
            return 1

        if not controller._had_write_failure:
            print("FAIL(b1): the failed write did not arm _had_write_failure "
                  "-- the TransportError was not routed through "
                  "_on_write_result(False), so no repaint recovery is queued")
            return 1

        landed_after = deck.ops_after(seq_at_unplug)
        real_writes = [e for e in landed_after
                       if e[2] in ("set_key_image", "set_touchscreen_image",
                                   "set_brightness", "set_key_color")]
        if real_writes:
            print(f"FAIL(b1): {len(real_writes)} write(s) landed on the journal "
                  f"AFTER unplug -- the dead transport must reject every write")
            return 1
    finally:
        fixtures.teardown(controller)

    # ---- b2: the LIVE writer must survive the same interleave ------------- #
    controller2 = make_headless_controller(serial="unplug-live-2")
    try:
        deck2 = raw_deck(controller2)
        mp2 = controller2.media_player
        if not wait_until(lambda: deck2.last_op_for("key:0") is not None, timeout=3):
            print("SETUP-FAIL(b2): initial paint never landed")
            return 1
        if not mp2.is_alive():
            print("SETUP-FAIL(b2): live writer not alive before unplug")
            return 1

        deck2.simulate_unplug()
        key0b = controller2.inputs[Input.Key][0]
        # Enqueue a paint the live loop will drain and attempt against the
        # dead transport.
        mp2.add_image_task(
            0, b"\x66" * 64,
            page=controller2.active_page,
            config_gen=controller2._page_load_generation,
            controller_key=key0b, img_hash=6666,
        )
        # Give the live loop time to drain and fail the write, then confirm it
        # registered the failure and is still alive.
        if not wait_until(lambda: controller2._had_write_failure, timeout=5):
            print("FAIL(b2): the live writer never observed the failed write")
            return 1
        if not mp2.is_alive():
            print("FAIL(b2): the media writer thread DIED on the unplug's "
                  "TransportError -- the deck would freeze (no paints, no "
                  "Clear, close only via timeout): the sole-writer freeze")
            return 1

        print("PASS: unplug mid-render -- writer survives, failed write "
              "swallowed and armed for repaint, nothing reaches the dead "
              "transport")
        return 0
    finally:
        fixtures.teardown(controller2)


def test_close_unplugged_deck_completes() -> int:
    """close() on an already-unplugged deck must still tear the controller
    down: the blank-frame writes fail harmlessly, the fallback deck.close()
    is a lifecycle-exempt no-op, and the controller deregisters + its threads
    exit despite the gone transport."""
    controller = make_headless_controller(serial="unplug-close")
    deck = raw_deck(controller)

    if not wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3):
        print("SETUP-FAIL(c): initial paint never landed")
        fixtures.teardown(controller)
        return 1

    # Unplug BEFORE close: this is the mid-render-removal case the backend
    # reap handles. close() must not hang or raise out.
    deck.simulate_unplug()

    done = threading.Event()

    def run_close():
        controller.close(remove_media=True)
        done.set()

    closer = threading.Thread(target=run_close, name="unplug-closer", daemon=True)
    closer.start()

    if not done.wait(timeout=8):
        print("FAIL(c): close() on an unplugged deck never returned (the "
              "blank-frame writes or fallback close hung/looped)")
        return 1

    # Teardown actually completed: media + tick threads exited, controller
    # deregistered from the page cache.
    media_dead = wait_until(lambda: not controller.media_player.is_alive(), timeout=3)
    if not media_dead:
        print("FAIL(c): the media writer never exited after close() on an "
              "unplugged deck")
        return 1
    if controller in gl.page_manager.pages:
        print("FAIL(c): controller never deregistered from the page cache")
        return 1
    if controller.active_page is not None:
        print("FAIL(c): active_page not released after close()")
        return 1

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    print("PASS: close() on an unplugged deck completes teardown cleanly")
    return 0


def test_unplug_races_page_load() -> int:
    """Removal (close) concurrent with a page load dispatched onto the deck's
    load pool must not deadlock or crash. close() bumps the generation so the
    racing load aborts at its gen gate; teardown completes regardless of which
    won the interleave."""
    controller = make_headless_controller(serial="unplug-load")
    deck = raw_deck(controller)

    if not wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3):
        print("SETUP-FAIL(d): initial paint never landed")
        fixtures.teardown(controller)
        return 1

    # A second page to load, made visually distinct (its own background) so the
    # load does real work.
    media = make_test_png(f"{gl.DATA_PATH}/media/unplug_load.png", color=(0, 120, 200))
    second_path = fixtures.seed_page_with_background("UnplugLoad", media)
    second_page = gl.page_manager.get_page(second_path, controller)

    load_started = threading.Event()
    load_returned = threading.Event()

    def run_load():
        load_started.set()
        try:
            controller.load_page(second_page, allow_reload=True)
        except Exception as e:
            # A crash out of load_page racing close is the failure this pins;
            # record it rather than letting it vanish on the daemon thread.
            run_load.error = e
        finally:
            load_returned.set()

    run_load.error = None

    loader = threading.Thread(target=run_load, name="unplug-loader", daemon=True)
    loader.start()
    load_started.wait(timeout=3)

    # Unplug mid-load, then close concurrently -- the removal path racing the
    # in-flight load on the pool.
    deck.simulate_unplug()

    closed = threading.Event()

    def run_close():
        controller.close(remove_media=True)
        closed.set()

    closer = threading.Thread(target=run_close, name="unplug-load-closer", daemon=True)
    closer.start()

    if not closed.wait(timeout=8):
        print("FAIL(d): close() never returned while racing an in-flight page "
              "load on the load pool (deadlock)")
        return 1
    if not load_returned.wait(timeout=5):
        print("FAIL(d): the racing load_page never returned (wedged behind "
              "close()'s teardown)")
        return 1
    if run_load.error is not None:
        print(f"FAIL(d): load_page racing close crashed: {run_load.error!r}")
        return 1

    media_dead = wait_until(lambda: not controller.media_player.is_alive(), timeout=3)
    if not media_dead:
        print("FAIL(d): media writer never exited after the racing close")
        return 1
    if controller in gl.page_manager.pages:
        print("FAIL(d): controller never deregistered after the racing close")
        return 1

    if controller in gl.deck_manager.deck_controller:
        gl.deck_manager.deck_controller.remove(controller)
    print("PASS: unplug racing a page load -- close completes, load aborts "
          "harmlessly, no deadlock or crash")
    return 0


def main() -> int:
    start_watchdog(60, "deck_unplug")
    # ONE tier only (the issue-#69 order-dependence class): install the
    # integration globals up front. Leg (a)'s bare FaultyFakeDecks only need
    # gl.settings_manager.get_deck_settings() (FakeDeck.__init__), which the
    # real SettingsManager satisfies ({} for an unknown serial), and the
    # controller legs need the full integration graph anyway -- never mix the
    # stub tier into this process.
    fixtures._install_integration_globals()
    rc = test_lifecycle_seam()
    rc |= test_unplug_mid_render_survives()
    rc |= test_close_unplugged_deck_completes()
    rc |= test_unplug_races_page_load()
    if rc == 0:
        print("PASS: scenario_deck_unplug")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
