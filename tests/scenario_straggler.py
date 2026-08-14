"""
Unit-tier scenario for the straggler drop at the present boundary.

A paint enqueued for generation G is dropped once a newer load_page bumps the
generation past G, and a paint against the current generation still lands.
This drives perform_media_player_tasks directly, so one call is one cycle.
"""
import fixtures


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_straggler")
    controller, media_player, deck_manager = fixtures.make_stub_controller()
    deck = controller.deck
    page = controller.active_page
    gen_g = controller._page_load_generation

    # Enqueue a frame for gen G, then bump the generation the way load_page
    # does, under _page_gen_lock and with no page switch. The straggler case
    # is the same page with a superseded content generation.
    stale_image = fixtures.make_native_image(fill=1)
    media_player.add_image_task(0, stale_image, page=page, config_gen=gen_g)

    new_gen = controller.bump_generation()
    assert new_gen == gen_g + 1, f"expected gen to bump to {gen_g + 1}, got {new_gen}"

    media_player.perform_media_player_tasks()  # one media cycle

    assert deck.last_op_for("key:0") is None, (
        f"stale-gen frame must be dropped, but journal has: {deck.journal()}"
    )

    # A frame enqueued against the now-current generation must land.
    fresh_image = fixtures.make_native_image(fill=2)
    media_player.add_image_task(0, fresh_image, page=page, config_gen=controller._page_load_generation)
    media_player.perform_media_player_tasks()

    landed = deck.last_op_for("key:0")
    assert landed is not None, "current-gen frame must land"
    assert landed[2] == "set_key_image"

    print("PASS: scenario_straggler")


if __name__ == "__main__":
    main()
