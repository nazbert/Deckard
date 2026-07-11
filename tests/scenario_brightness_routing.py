"""
Integration scenario (docs/presenter-migration-plan.md §7 "Owner assertion"
+ M1's set_brightness reroute): before M1, set_brightness() wrote directly
to the device from whatever thread called it (GTK/Timer/switch threads --
see the plan's inventory table, §1). After routing through
submit_control(SetBrightnessMsg(...)), the device write must land on the
media thread, and BetterDeck's owner-assertion tooling
(STREAMCONTROLLER_ASSERT_DEVICE_OWNER) must record zero violations across
the whole scenario (bootstrap clear, page load, and the brightness call
itself).

The env var must be set before the deck's BetterDeck wrapper is constructed
(DeckController.__init__ reads it once at construction, via BetterDeck.py)
-- set it before importing fixtures/constructing anything.
"""
import os
import threading

os.environ["STREAMCONTROLLER_ASSERT_DEVICE_OWNER"] = "1"

import fixtures


def main() -> None:
    controller = fixtures.make_headless_controller(serial="brightness-routing-1")
    deck = fixtures.raw_deck(controller)

    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    deck.clear_journal()

    # Call from a thread that is definitely not the media thread (mirrors a
    # GTK slider / DeckGroup UI callback).
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

    # BetterDeck's owner-assertion detector (log-only, never raises) must
    # show zero violations for the whole scenario -- including the bootstrap
    # clear and page-load writes, not just the brightness call under test.
    violations = controller.deck.owner_violations
    assert violations == [], f"owner violations recorded: {violations}"

    fixtures.teardown(controller)
    print("PASS: scenario_brightness_routing")


if __name__ == "__main__":
    main()
