"""The set_brightness device write must land on the media thread.

submit_control(SetBrightnessMsg(...)) routes it there, and the owner
assertion of BetterDeck must record zero violations across the scenario.
"""
import os
import threading

os.environ["DECKARD_ASSERT_DEVICE_OWNER"] = "1"

import fixtures


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_brightness_routing")
    controller = fixtures.make_headless_controller(serial="brightness-routing-1")
    deck = fixtures.raw_deck(controller)

    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    deck.clear_journal()

    # Call from a thread that is not the media thread, which mirrors a GTK
    # slider or a DeckGroup UI callback.
    def _set():
        controller.set_brightness(42)

    caller = threading.Thread(target=_set, name="brightness-caller")
    caller.start()
    caller.join(timeout=2)
    assert not caller.is_alive(), "set_brightness() must return immediately (non-blocking submit)"

    landed = fixtures.wait_until(lambda: deck.ops_by_name("set_brightness") != [], timeout=3)
    assert landed, "brightness write never landed"

    media_thread_name = controller.media_player.name
    for entry in deck.ops_by_name("set_brightness"):
        assert entry[5] == media_thread_name, (
            f"set_brightness landed on thread {entry[5]!r}, expected the media "
            f"thread {media_thread_name!r}: {entry}"
        )

    # The owner-assertion detector of BetterDeck logs and never raises. It must
    # show zero violations for the whole scenario, including the bootstrap
    # clear and the page-load writes.
    violations = controller.deck.owner_violations
    assert violations == [], f"owner violations recorded: {violations}"

    fixtures.teardown(controller)
    print("PASS: scenario_brightness_routing")


if __name__ == "__main__":
    main()
