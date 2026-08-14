"""
Unit-tier scenario for the touchscreen background-video keep-check.

An InputVideo bakes the display saturation into its shared tile cache at
construction, and set_playback updates only fps and loop. The keep-check
therefore tracks the factor the strip video was built at and rebuilds when
that factor diverges, so the strip never serves frames baked at the old one.
"""
import os
import threading
import types

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image

import globals as gl
import src.backend.DeckManagement.deck_controller.inputs as inputs_mod
from src.backend.DeckManagement.DeckController import ControllerTouchScreenState

WATCHDOG_SECONDS = 30


class _SpyInputVideo:
    """Stands in for InputVideo at deck_controller.inputs module scope.

    Records the display saturation it would bake into its tile cache, read
    the way the real InputVideo.__init__ reads it, and tracks reuse through
    set_playback."""

    instances: list = []

    def __init__(self, controller_input, video_path, fps, loop, natural_speed=False):
        self.video_path = video_path
        self.fps = fps
        self.loop = loop
        self.natural_speed = natural_speed
        # The real InputVideo freezes the current factor into its shared
        # cache here and never revisits it.
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
    """Build the state through __new__ with only the attributes
    _get_background_video_frame reads. get_display_saturation stays live on
    saturation_holder, so a flip between calls reaches a fresh InputVideo."""
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


def check_keepcheck_reacquires_on_saturation() -> None:
    fixtures.install_stub_globals()
    # A path only. The spy InputVideo never opens it, but the method builds a
    # real one, so something must exist on disk.
    video_path = os.path.join(gl.DATA_PATH, "strip_bg.mp4")
    with open(video_path, "wb") as f:
        f.write(b"placeholder")

    saturation_holder = {"value": 1.0}
    state = _make_touch_state(saturation_holder)

    _SpyInputVideo.instances.clear()
    real_input_video = inputs_mod.InputVideo
    inputs_mod.InputVideo = _SpyInputVideo
    try:
        # The first composite at factor 1.0 constructs the strip video, which
        # bakes saturation 1.0 into its cache.
        state._get_background_video_frame(video_path, fps=30, loop=True)
        assert len(_SpyInputVideo.instances) == 1, "first call must construct one InputVideo"
        v1 = _SpyInputVideo.instances[0]
        assert v1.baked_saturation == 1.0, f"first video should bake 1.0, got {v1.baked_saturation}"

        # The same path with the saturation changed to 1.3, as a slider move
        # does. A repeat composite must not keep serving the 1.0-baked video.
        saturation_holder["value"] = 1.3
        state._get_background_video_frame(video_path, fps=30, loop=True)

        current = state.background_video
        # The strip video must now reflect factor 1.3, through a rebuild or
        # an in-place re-acquire. A reuse branch that compares the path alone
        # keeps the 1.0-baked video and calls set_playback only.
        assert current.baked_saturation == 1.3, (
            f"after a saturation change the reused strip video still bakes "
            f"{current.baked_saturation} (expected 1.3): the keep-check at "
            f"_get_background_video_frame compares only video_path, not the "
            f"display saturation, so the SD+ strip keeps playing frames "
            f"enhanced at the old factor while the keys are re-enhanced "
            f"(audit §5a, e314a086 :4230)"
        )
    finally:
        inputs_mod.InputVideo = real_input_video
        _SpyInputVideo.instances.clear()

    print("PASS: touchscreen bg-video keep-check re-acquires on a saturation change")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_saturation_keepcheck")
    check_keepcheck_reacquires_on_saturation()
    print("PASS: scenario_saturation_keepcheck")


if __name__ == "__main__":
    main()
