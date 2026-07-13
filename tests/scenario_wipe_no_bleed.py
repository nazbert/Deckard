"""
Scenario (issue #70 graduation, half 1 of 2): the no-bleed contract.

Switching to a page whose key has NO action must CLEAR that slot -- the
framework must not leave the previous page's action-owned image on a key that
no action owns on the new page.

This is the currently-CORRECT half of the wipe-restore behavior (issue #131):
a cross-page load builds a *different* action object, so any future
stash-and-restore fix (gated on action identity) will not restore into it ->
no bleed. Committed as an always-on regression net so that fix can't regress
into bleeding the previous page's image.

The source-page image is established via a DETERMINISTIC SEAM -- once the page
load has settled (on_ready_finished), a direct set_media() on the now-stable
state reliably paints the key. This avoids the racy on_ready-paint-vs-state-
recreation timing (see scenario_wipe_restore.py / issue #131) so that the
no-bleed check is a real "image present -> must clear" transition and never
vacuous (an image that was never established would make "no bleed" trivially
true).

Drives the REAL DeckController/Page/ControllerKey/ActionCore machinery with a
LatchAction injected via a stub plugin_manager (fixtures helpers). Graduated
from the untracked diag_wipe_contract.py.
"""
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import start_watchdog, wait_until, teardown

from src.backend.DeckManagement.InputIdentifier import Input

WATCHDOG_SECONDS = 60
TRIALS = 5


def _get_action(page):
    """The single action object bound to the page's one key/state."""
    for by_ident in page.action_objects.values():
        for by_state in by_ident.values():
            for by_index in by_state.values():
                for action in by_index.values():
                    return action
    return None


def main() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "wipe_icon.png"), color=(0, 200, 0))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_wipe_no_bleed")

    controller = fixtures.make_headless_controller(serial="wipe-nobleed-1")
    try:
        key = controller.inputs[Input.Key][0]
        key_ident = key.identifier.json_identifier
        empty_page = gl.page_manager.get_page(
            fixtures.seed_empty_action_page("LatchEmptyNB", key_ident), controller)

        def active_image():
            return key.get_active_state().key_image

        bleeds = []
        for i in range(TRIALS):
            # Establish an action-owned image DETERMINISTICALLY: load the
            # action page, wait for it to settle, then force a fresh paint on
            # the stabilized state.
            action_page = gl.page_manager.get_page(
                fixtures.seed_action_page(f"LatchNB{i}", key_ident), controller)
            controller.load_page(action_page, allow_reload=True)
            settled = wait_until(
                lambda: _get_action(action_page) is not None
                and _get_action(action_page).on_ready_finished, timeout=5)
            assert settled, f"trial {i}: action page never settled (on_ready_finished)"

            action = _get_action(action_page)
            action.current_state = -1  # clear the latch so set_media paints
            action.set_media(media_path=icon_path, size=0.8)
            painted = wait_until(lambda: active_image() is not None, timeout=5)
            assert painted, (
                f"trial {i}: could not establish an image on the source page "
                "-- the no-bleed check that follows would be vacuous"
            )

            # Switch to a page whose same key has NO action: the slot must
            # clear. A cross-page load builds a different action object, so
            # nothing owns the old image -> it must not survive.
            controller.load_page(empty_page, allow_reload=True)
            cleared = wait_until(lambda: active_image() is None, timeout=5)
            if not cleared:
                bleeds.append(i)

        assert not bleeds, (
            f"empty-key page kept the previous page's image on {len(bleeds)}/"
            f"{TRIALS} trials ({bleeds}) -- switching to an action-less page "
            "must clear the slot, not bleed the prior page's action-owned image"
        )
        print("PASS: scenario_wipe_no_bleed")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
