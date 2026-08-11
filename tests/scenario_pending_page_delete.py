"""
Integration scenario: deleting a page that is some
controller's _screensaver_pending_page must clear the pending request.

remove_page() only handled controllers whose ACTIVE page was the deleted
one; a page stashed as screensaver-pending (load_page's deferral while the
screensaver owns the deck) was invisible to that check. The pending
reference was left dangling at a deleted file: hide() then loaded a page
whose json no longer exists, and the page's first save resurrected the
deleted file.

Uses the scenario_screensaver_page_change machinery: settle on page A,
raise the screensaver, request page B (recorded as pending), then DELETE
page B. The pending slot must clear, B's cache entry must drop, and on
dismiss the controller must stay on page A without resurrecting B's file.

Second half, same hazard through the other door: a page object that outlives
the delete of its page can mark that path again, and the mark stands under
the path rather than under the object. Creating a page of that name next
finds a fresh file and an outstanding write for it, and the read barrier --
which exists to make a read see edits the file has not got -- is exactly what
puts the deleted page back on top of the new one.
"""
import json
import os
import time

import fixtures
import globals as gl
from src.backend.PageManagement import page_flush

WATCHDOG_SECONDS = 30


class NoTimers:
    """A timer source that arms nothing.

    The straggler mark below has to still be outstanding when the page is
    re-created, which is the whole point of it; leaving that to a one-second
    timer would make the check a race with the machine's load rather than a
    statement about the barrier.
    """

    def schedule(self, delay_s, callback):
        return object()

    def cancel(self, handle):
        pass


def read_page(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def check_a_recreated_page_is_not_the_deleted_one(controller) -> None:
    """A straggler's write must not be resurrected onto a page created under
    the same name."""
    original_flush = page_flush.get()
    page_flush._flush = page_flush.PageFlush(scheduler=NoTimers())
    try:
        path = fixtures.seed_page("PPD_Recreated")
        straggler = gl.page_manager.get_page(path, controller)
        straggler.dict["from-the-deleted-page"] = True
        straggler.save()

        gl.page_manager.remove_page(path)
        assert not os.path.exists(path), "remove_page must delete the file"

        # The object survived the delete -- it is not this controller's
        # active page, so nothing tore it down -- and saves again. Whatever
        # holds such a reference (a widget, a screensaver-pending slot, a
        # plugin thread mid-save) marks the path, not the object.
        straggler.dict["marked-after-the-delete"] = True
        straggler.save()
        assert page_flush.get().pending_source(path) is not None, (
            "fixture: the straggler's save must leave a write outstanding for "
            "the deleted path, or this check proves nothing"
        )

        new_path = gl.page_manager.add_page(
            "PPD_Recreated", {"keys": {}, "dials": {}, "touchscreens": {}, "fresh": True})
        assert new_path == path

        on_disk = read_page(path)
        assert on_disk.get("fresh") is True and "from-the-deleted-page" not in on_disk, (
            f"creating a page reinstated the deleted page of the same name -- "
            f"the read barrier wrote a straggler's edits over the new file: "
            f"{on_disk}"
        )

        # ...and the page every deck reads was corrected too, so the next
        # edit of it writes the new page rather than putting the old one back.
        gl.page_manager.overwrite_brightness_settings(path, brightness=42)
        page_flush.get().flush_path(path)
        after_edit = read_page(path)
        assert "from-the-deleted-page" not in after_edit, (
            f"the first edit of the re-created page wrote the deleted page's "
            f"content back out: {after_edit}"
        )
        assert after_edit.get("settings", {}).get("brightness", {}).get("value") == 42, (
            f"the edit never reached the re-created page: {after_edit}"
        )
        print("PASS: a page created where a deleted one stood is the new page, "
              "not the old one")
    finally:
        page_flush._flush = original_flush


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_pending_page_delete")

    controller = fixtures.make_headless_controller(serial="ppd-1")
    deck = fixtures.raw_deck(controller)
    key_count = controller.deck.key_count()

    a_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "ppd_a.png"), color=(10, 200, 10))
    b_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "ppd_b.png"), color=(200, 10, 10))
    ss_png = fixtures.make_test_png(os.path.join(gl.DATA_PATH, "media", "ppd_ss.png"), color=(10, 10, 200))

    a_path = fixtures.seed_page_with_background_and_screensaver(
        "PPD_A", a_png, ss_png, screensaver_time_delay=60)
    b_path = fixtures.seed_page_with_background("PPD_B", b_png)
    page_a = gl.page_manager.get_page(a_path, controller)
    page_b = gl.page_manager.get_page(b_path, controller)

    try:
        # Settle on page A, raise the screensaver, request B while it shows.
        controller.load_page(page_a, allow_reload=True)
        fixtures.wait_until(lambda: all(
            deck.last_op_for(f"key:{k}") is not None for k in range(key_count)), timeout=5)

        controller.screen_saver.show()
        ok = fixtures.wait_until(lambda: controller.screen_saver.showing, timeout=5)
        assert ok, "screensaver never showed"

        controller.load_page(page_b, allow_reload=True)
        assert controller._screensaver_pending_page is page_b, (
            "fixture: the page change during the screensaver must be pending"
        )

        # --- Delete the PENDING page. ---
        gl.page_manager.remove_page(b_path)

        assert not os.path.exists(b_path), "remove_page must delete the file"
        assert controller._screensaver_pending_page is None, (
            "deleting a controller's pending page left the request dangling "
            "at a deleted file -- hide() would load a page whose "
            "json is gone and the first save would resurrect it"
        )
        cached = gl.page_manager.pages.get(controller, {})
        assert b_path not in cached, (
            "the deleted pending page's cache entry must be dropped"
        )

        # --- Dismiss: the controller stays on page A, B is not resurrected.
        controller.screen_saver.hide()
        ok = fixtures.wait_until(lambda: not controller.screen_saver.showing, timeout=5)
        assert ok, "screensaver never hid"
        time.sleep(0.3)

        assert controller.active_page is page_a, (
            f"after deleting the pending page, dismiss must keep the current "
            f"page (active={controller.active_page})"
        )
        assert not os.path.exists(b_path), (
            "the deleted page's json was resurrected after the screensaver "
            "dismissed (a dangling pending page saved itself back to disk)"
        )

        check_a_recreated_page_is_not_the_deleted_one(controller)
    finally:
        fixtures.teardown(controller)

    print("PASS: scenario_pending_page_delete")


if __name__ == "__main__":
    main()
