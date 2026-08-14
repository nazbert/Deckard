"""
Integration scenario for screensaver entry and exit with a static image.

ScreenSaver.show must clear the deck before it repaints with the screensaver
media, and hide must repaint every key with the page's content. The blank
reference hash comes from the deterministic bootstrap clear, so no assertion
races show()'s asynchronous background repaint.
"""
import os
import time

import fixtures
import globals as gl


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_screensaver_entry")
    controller = fixtures.make_headless_controller(serial="ss-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    # DeckController.__init__ runs a bootstrap clear before load_default_page,
    # which paints a deterministic blank image. Capture its hash as the blank
    # reference before anything else changes it.
    blank_hash = next(e[4] for e in deck.journal() if e[3] == "key:0")

    # Let the default page's real content land before measuring the
    # screensaver transition.
    fixtures.wait_until(lambda: deck.last_op_for("key:0") is not None, timeout=3)
    time.sleep(0.1)
    pre_show_hash = deck.last_op_for("key:0")[4]
    assert pre_show_hash != blank_hash, "fixture sanity: default page content should not be blank"

    screensaver_png = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "screensaver.png"), color=(0, 200, 0)
    )
    controller.screen_saver.set_media_path(screensaver_png)
    controller.screen_saver.set_brightness(10)

    seq_before_show = deck.current_seq()
    controller.screen_saver.show()

    def repainted_with_screensaver_content():
        # Every key must carry post-show screensaver content before the
        # assertions below run. A bulk batch writes keys one at a time, so a
        # check of key:0 alone would race a mid-flight batch.
        for k in range(key_count):
            e = deck.last_op_for(f"key:{k}")
            if e is None or e[1] <= seq_before_show or e[4] in (blank_hash, pre_show_hash):
                return False
        return True

    ok = fixtures.wait_until(repainted_with_screensaver_content, timeout=5)
    assert ok, "screensaver content never repainted on every key after show()"

    # After show() started, key:0 must have been blanked, rather than moving
    # straight from the old content to the new.
    blank_landed = any(
        e[3] == "key:0" and e[1] > seq_before_show and e[4] == blank_hash
        for e in deck.journal()
    )
    assert blank_landed, "show() must clear (blank) before repainting with screensaver content"

    for k in range(key_count):
        final = deck.last_op_for(f"key:{k}")
        assert final is not None, f"key {k} was never painted during show()"
        assert final[4] not in (blank_hash, pre_show_hash), (
            f"key {k}: final state after show() should be screensaver content, "
            f"got hash {final[4]}"
        )

    # On hide() every key must repaint with the real page's content.
    seq_before_hide = deck.current_seq()
    controller.screen_saver.hide()

    def fully_repainted():
        return all(
            (e := deck.last_op_for(f"key:{k}")) is not None and e[1] > seq_before_hide
            for k in range(key_count)
        )

    ok = fixtures.wait_until(fully_repainted, timeout=5)
    assert ok, "not every key repainted after hide()"
    assert controller.screen_saver.showing is False

    fixtures.teardown(controller)
    print("PASS: scenario_screensaver_entry")


if __name__ == "__main__":
    main()
