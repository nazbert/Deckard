"""
Scenario (issue #70 graduation, half 2 of 2): the no-blank contract.

Pins the FIXED wipe-without-restore bug (issue #131) as an always-on
regression net (formerly in EXPECTED_FAIL_UNTIL_M1 in run_all.py while the
bug was open).

The bug was: ControllerKey.load_from_input_dict -> create_n_states
unconditionally destroyed+recreated every state on each (re)load, closing
every state's key_image. The action-owned image is not persisted in the page
JSON (it is set at runtime via set_media, not written to "media.path"), so
the ONLY thing that could re-establish it after the wipe was
own_actions_update() -> the action's on_update(). A LatchAction that dedups
in on_update without resetting never repaints -> the key settled BLANK.

Why trials, not a single synchronous seam: the blank only manifests through
the REAL async load pipeline, where the action-executor thread's on_ready
paint races create_n_states running on the load thread. A purely synchronous
reproduction (forcing the paint, then calling load_from_input_dict directly)
does NOT lose the race and so does not exhibit the bug -- the defect is
inherently timing-dependent. The per-trial blank rate on current code is ~0.93
(measured), so a handful of trials pins it with overwhelming probability
(P(no blank in TRIALS) ~= 0.07**TRIALS); the assertion fires on the first
blank. Trials are bounded and each uses a wait_until seam (not a fixed sleep).

The fix (issue #131): stash-and-restore gated on action identity in
load_from_input_dict -- set_media stamps the painting action on the state
(media_owner_action); the load detaches owned media before create_n_states
and restores it iff that exact action object still drives the recreated
state (identity matches -> restore -> no blank); a cross-page load builds a
different action (identity mismatch -> close, no restore -> no bleed, see
scenario_wipe_no_bleed.py).

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
TRIALS = 8


def main() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "wipe_icon.png"), color=(0, 200, 0))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_wipe_restore")

    controller = fixtures.make_headless_controller(serial="wipe-restore-1")
    try:
        key = controller.inputs[Input.Key][0]
        key_ident = key.identifier.json_identifier

        def active_image():
            return key.get_active_state().key_image

        blanks = []
        for i in range(TRIALS):
            # A fresh page per trial whose key carries the LatchAction as its
            # image-control action. Loading it runs the action's on_ready
            # (which paints once via set_media) on the action-executor thread,
            # racing create_n_states' state wipe on the load thread.
            action_page = gl.page_manager.get_page(
                fixtures.seed_action_page(f"LatchR{i}", key_ident), controller)
            controller.load_page(action_page, allow_reload=True)
            # Wait (deterministic seam, not a fixed sleep) for either the
            # image to appear OR the load to fully settle -- on the FIXED
            # version the image is present after settling; on today's code it
            # never appears because the wiped image was never restored by the
            # deduping on_update.
            painted = wait_until(lambda: active_image() is not None, timeout=3)
            if not painted:
                blanks.append(i)

        # The pinned assertion: with the #131 identity-gated stash-and-restore
        # in place, no trial may settle blank.
        assert not blanks, (
            f"the action-control key settled BLANK on {len(blanks)}/{TRIALS} "
            f"loads ({blanks}) -- create_n_states wiped the action-owned image "
            "and the deduping on_update never restored it (issue #131)"
        )
        print("PASS: scenario_wipe_restore")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
