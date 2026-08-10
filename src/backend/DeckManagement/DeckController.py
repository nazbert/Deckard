"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

---

The compatibility surface of the deck controller. Nothing is defined here:
every class, function and constant this module used to hold now lives in
src/backend/DeckManagement/deck_controller/, and the imports below re-export
them under the path importers have always used -- the plugin repos take
DeckController from here, the label editor takes KeyLabel, the test harness
takes the media writer and the private hooks it pins.

So a name appears below because something asks for it AT THIS PATH, not
because the package exposes it: __all__ is the compat contract, and the
package's own modules are the place to import from in new code. This file
stays import statements and __all__ -- code that belongs to a deck
controller belongs in the package.
"""
from src.backend.DeckManagement.deck_controller.background_media import (  # noqa: F401
    Background, BackgroundImage, BackgroundVideo,
)
from src.backend.DeckManagement.deck_controller.controller import CONTROLLER_CLASSES, DeckController  # noqa: F401
from src.backend.DeckManagement.deck_controller.gif_pipeline import (  # noqa: F401
    BOUNDED_TILE_VARIANT, GIF_BG_BUDGET_MB, GIF_KEY_BUDGET_MB, GifBackground,
    GifBudgetExceeded, GifTimeline, KeyGIF, _STRIP_GEOMETRY_MISSING,
    contained_size, cumulative_gif_delays, decode_gif_frames, frame_has_alpha,
    gif_frame_walk, gif_header_geometry, gif_key_budget_bytes,
    normalize_gif_delay, probe_gif_timeline, tile_video_size,
)
from src.backend.DeckManagement.deck_controller.inputs import (  # noqa: F401
    ControllerDial, ControllerDialState, ControllerInput, ControllerInputState,
    ControllerKey, ControllerKeyState, ControllerTouchScreen,
    ControllerTouchScreenState, StateT,
)
from src.backend.DeckManagement.deck_controller.label_engine import (  # noqa: F401
    BackgroundManager, LabelManager, LayoutManager, _BitmapRecorder,
    _RecordingTooLarge, _label_measure_draw,
)
from src.backend.DeckManagement.deck_controller.media_writer import (  # noqa: F401
    KEY_ENCODE_QUALITY, ClearAndCloseMsg, ClearMsg, MediaPlayerSetImageTask,
    MediaPlayerSetTouchscreenImageTask, MediaPlayerTask, MediaPlayerThread,
    ReleaseStashedInputsMsg, SetBrightnessMsg, _env_float,
    _install_fair_transport_lock, encode_native_key, encode_native_touchscreen,
)

# Three names that were never this module's to define: they belong to other
# modules entirely and are here only because callers reach them through this
# path. The unplug scenario takes Input from here, the label editor takes
# KeyLabel, and InputVideo was patched on this module's namespace until the
# inputs moved out from under it.
from src.backend.DeckManagement.InputIdentifier import Input  # noqa: F401
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel  # noqa: F401
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo  # noqa: F401

__all__ = [
    "BOUNDED_TILE_VARIANT", "Background", "BackgroundImage", "BackgroundManager",
    "BackgroundVideo", "CONTROLLER_CLASSES", "ClearAndCloseMsg", "ClearMsg",
    "ControllerDial", "ControllerDialState", "ControllerInput",
    "ControllerInputState", "ControllerKey", "ControllerKeyState",
    "ControllerTouchScreen", "ControllerTouchScreenState", "DeckController",
    "GIF_BG_BUDGET_MB", "GIF_KEY_BUDGET_MB", "GifBackground", "GifBudgetExceeded",
    "GifTimeline", "Input", "InputVideo", "KEY_ENCODE_QUALITY", "KeyGIF",
    "KeyLabel", "LabelManager", "LayoutManager", "MediaPlayerSetImageTask",
    "MediaPlayerSetTouchscreenImageTask", "MediaPlayerTask", "MediaPlayerThread",
    "ReleaseStashedInputsMsg", "SetBrightnessMsg", "StateT",
    "_BitmapRecorder", "_RecordingTooLarge", "_STRIP_GEOMETRY_MISSING",
    "_env_float", "_install_fair_transport_lock", "_label_measure_draw",
    "contained_size", "cumulative_gif_delays", "decode_gif_frames",
    "encode_native_key", "encode_native_touchscreen", "frame_has_alpha",
    "gif_frame_walk", "gif_header_geometry", "gif_key_budget_bytes",
    "normalize_gif_delay", "probe_gif_timeline", "tile_video_size",
]
