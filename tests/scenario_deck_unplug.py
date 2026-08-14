"""Scenario for deck unplug and replug mid-render.

FaultyFakeDeck models the closed and unplugged states, so this pins the
lifecycle seam, writer survival, close on an unplugged deck, and a load race.
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


def test_lifecycle_seam() -> int:
    """is_open() and connected() report real state.

    In strict mode a write past close() or unplug raises TransportError.
    """
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
    # connected() stays True on a plain close, because the cable is still in.
    if not deck.connected():
        print("FAIL(a): a plain close() must not flip connected()")
        return 1
    # A write after close raises under the strict default and does not journal.
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

    # A second deck. Unplug flips connected() and fails writes.
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

    # Lenient mode lets a post-close write journal silently.
    deck3 = FaultyFakeDeck(serial_number="unplug-seam-3")
    deck3.set_strict_lifecycle(False)
    deck3.close()
    deck3.set_key_image(0, b"\x04" * 16)  # must not raise
    if deck3.last_op_for("key:0") is None:
        print("FAIL(a): lenient mode must let a post-close write journal")
        return 1

    print("PASS: lifecycle seam -- open/connected reflect state; strict writes "
          "past close()/unplug raise; lenient opt-out works")
    return 0


def test_unplug_mid_render_survives() -> int:
    """Yanking the deck mid-render must not kill the sole writer.

    The TransportError handler of the write task swallows the failed write and
    arms the pending repaint through _on_write_result(False). Part b1 drives
    the tick by hand for determinism; part b2 checks the live loop survives.
    """
    from src.backend.DeckManagement.DeckController import Input

    # Part b1 drives the writer by hand.
    controller = make_headless_controller(serial="unplug-live")
    try:
        deck = raw_deck(controller)
        media_player = controller.media_player

        # Land a real paint first, so the journal has a pre-unplug baseline,
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
        # The task run() attempts set_key_image on the dead transport. The
        # handler must swallow the TransportError, so this call must not raise.
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

    # Part b2. The live writer must survive the same interleave.
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
        # Enqueue a paint the live loop drains and attempts against the dead
        # transport.
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
    """close() on an already-unplugged deck must still tear the controller down.

    The blank-frame writes fail harmlessly and the fallback deck.close() is a
    lifecycle-exempt no-op. The controller deregisters and its threads exit.
    """
    controller = make_headless_controller(serial="unplug-close")
    deck = raw_deck(controller)

    if not wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3):
        print("SETUP-FAIL(c): initial paint never landed")
        fixtures.teardown(controller)
        return 1

    # Unplug before close, which is the mid-render removal case the backend reap
    # handles. close() must not hang and must not raise out.
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

    # Teardown completed. The media and tick threads exited, and the controller
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
    """A close concurrent with a page load must not deadlock or crash.

    close() bumps the generation, so the racing load aborts at its gen gate.
    Teardown completes whichever way the interleave went.
    """
    controller = make_headless_controller(serial="unplug-load")
    deck = raw_deck(controller)

    if not wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3):
        print("SETUP-FAIL(d): initial paint never landed")
        fixtures.teardown(controller)
        return 1

    # A second page to load, made visually distinct with its own background, so
    # the load does real work.
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
            # A crash out of load_page racing close is the failure this pins.
            # Record it rather than let it vanish on the daemon thread.
            run_load.error = e
        finally:
            load_returned.set()

    run_load.error = None

    loader = threading.Thread(target=run_load, name="unplug-loader", daemon=True)
    loader.start()
    load_started.wait(timeout=3)

    # Unplug mid-load, then close concurrently. The removal path then races the
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
    # One tier only. Install the integration globals up front. The bare
    # FaultyFakeDecks of the first leg need only
    # gl.settings_manager.get_deck_settings(), which the real SettingsManager
    # satisfies, and the controller legs need the full integration graph.
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
