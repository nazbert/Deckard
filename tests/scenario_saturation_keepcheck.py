"""
Unit-tier scenario for issue #132 (latent touchscreen bg-video keep-check
bug; surfaced as ask (5) of the #68 test-coverage audit): the per-touchscreen
background VIDEO reuse "keep-check" must invalidate on a display-saturation
change.

ControllerTouchScreenState._get_background_video_frame() (DeckController.py,
the `if video is None or video.video_path != path:` guard, ~:4632) decides
whether to REUSE the existing InputVideo or build a fresh one. It keys the
decision on the source path ONLY. But an InputVideo bakes the current
display-saturation into its shared tile-cache at CONSTRUCTION
(KeyVideo.py: mp4_tile_cache.acquire(..., get_display_saturation())), and
set_playback() only updates fps/loop -- never saturation. So when the
saturation factor changes while the same video stays configured as the strip
background, the reuse branch keeps serving frames baked at the OLD factor:
the SD+ strip video desaturates relative to the (correctly re-enhanced) keys.

This is the cross-commit latent defect the audit flags
(docs/deep-audit-2026-07-10.md §5a, commit 4b2a3dbd LOW / e314a086 :4230):
"the touchscreen bg-video reuse check compares only path, not saturation --
masked today because a slider change reloads the page." The masking makes it
unreachable in production RIGHT NOW (set_display_saturation ->
load_page(allow_reload=True) rebuilds the touch state, dropping the reused
video). But the invalidation logic itself is absent, so if the reload is ever
removed/deferred (e.g. the settings font-row debounce pattern applied here,
or a targeted repaint), the strip goes stale silently. This leg pins that
missing invalidation.

EXPECTED TO FAIL until the keep-check gains the saturation dimension
(issue #132): it asserts the DESIRED behaviour (a saturation change
rebuilds/re-acquires the strip video at the new factor). It is registered in
run_all.EXPECTED_FAIL_UNTIL_M1 as a documented latent-bug pin, not a passing
regression net -- when #132 is fixed this flips to a passing regression net
(drop the XFAIL entry then). Full diagnosis + unmasking conditions on #132;
origin context on issue #68.

Drives the REAL ControllerTouchScreenState._get_background_video_frame via
__new__ + the exact attributes it reads, with a spy InputVideo (patched at
DeckController module scope) that records the saturation each constructed
instance acquired.
"""
import os
import threading
import types

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image

import globals as gl
import src.backend.DeckManagement.DeckController as deckcontroller_mod
from src.backend.DeckManagement.DeckController import ControllerTouchScreenState

WATCHDOG_SECONDS = 30


class _SpyInputVideo:
    """Stands in for InputVideo at DeckController module scope. Records the
    display-saturation it would bake into its tile cache (read the same way
    the real InputVideo.__init__ does: controller_input.deck_controller
    .get_display_saturation()), and tracks reuse via set_playback()."""

    instances: list = []

    def __init__(self, controller_input, video_path, fps, loop, natural_speed=False):
        self.video_path = video_path
        self.fps = fps
        self.loop = loop
        self.natural_speed = natural_speed
        # This is the crux: the real InputVideo freezes the current factor
        # into its shared cache here and never revisits it.
        self.baked_saturation = controller_input.deck_controller.get_display_saturation()
        self.closed = False
        self.set_playback_calls: list = []
        self.frames_served = 0
        _SpyInputVideo.instances.append(self)

    def get_next_frame(self, now=None):
        self.frames_served += 1
        # A non-None frame so the method returns normally (no failure branch).
        return Image.new("RGBA", (800, 100), (10, 20, 30, 255))

    def set_playback(self, fps, loop):
        self.set_playback_calls.append((fps, loop))
        self.fps = fps
        self.loop = loop

    def close(self):
        self.closed = True


def _make_touch_state(saturation_holder) -> ControllerTouchScreenState:
    """__new__ + exactly the attributes _get_background_video_frame reads.
    controller_touch.deck_controller.get_display_saturation() is live: it
    reflects the current value in `saturation_holder` so flipping the factor
    between calls is visible to a freshly constructed InputVideo."""
    deck_controller = types.SimpleNamespace(
        get_display_saturation=lambda: saturation_holder["value"]
    )
    controller_touch = types.SimpleNamespace(deck_controller=deck_controller)

    state = ControllerTouchScreenState.__new__(ControllerTouchScreenState)
    state.controller_touch = controller_touch
    state.background_video = None
    state._background_video_failed = None
    state._background_video_lock = threading.Lock()
    return state


def check_keepcheck_reacquires_on_saturation_change() -> None:
    fixtures.install_stub_globals()
    # A path only: the spy InputVideo never opens it, but the method builds a
    # real one, so give it something on disk to be faithful.
    video_path = os.path.join(gl.DATA_PATH, "strip_bg.mp4")
    with open(video_path, "wb") as f:
        f.write(b"placeholder")

    saturation_holder = {"value": 1.0}
    state = _make_touch_state(saturation_holder)

    _SpyInputVideo.instances.clear()
    real_input_video = deckcontroller_mod.InputVideo
    deckcontroller_mod.InputVideo = _SpyInputVideo
    try:
        # 1) First composite at factor 1.0: constructs the strip video, which
        #    bakes saturation 1.0 into its cache.
        state._get_background_video_frame(video_path, fps=30, loop=True)
        assert len(_SpyInputVideo.instances) == 1, "first call must construct one InputVideo"
        v1 = _SpyInputVideo.instances[0]
        assert v1.baked_saturation == 1.0, f"first video should bake 1.0, got {v1.baked_saturation}"

        # 2) Same path, SATURATION CHANGED to 1.3 (the slider moved). A repeat
        #    composite must not keep serving the 1.0-baked video.
        saturation_holder["value"] = 1.3
        state._get_background_video_frame(video_path, fps=30, loop=True)

        current = state.background_video
        # DESIRED behaviour: the strip video now reflects factor 1.3, either by
        # a rebuild (a second _SpyInputVideo baking 1.3) or an in-place
        # re-acquire. Today the reuse branch keeps v1 (path unchanged) and only
        # calls set_playback (fps/loop), so current is still v1 @ 1.0.
        assert current.baked_saturation == 1.3, (
            f"after a saturation change the reused strip video still bakes "
            f"{current.baked_saturation} (expected 1.3): the keep-check at "
            f"_get_background_video_frame compares only video_path, not the "
            f"display saturation, so the SD+ strip keeps playing frames "
            f"enhanced at the old factor while the keys are re-enhanced "
            f"(audit §5a, e314a086 :4230)"
        )
    finally:
        deckcontroller_mod.InputVideo = real_input_video
        _SpyInputVideo.instances.clear()

    print("PASS: touchscreen bg-video keep-check re-acquires on a saturation change")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_saturation_keepcheck")
    check_keepcheck_reacquires_on_saturation_change()
    print("PASS: scenario_saturation_keepcheck")


if __name__ == "__main__":
    main()
