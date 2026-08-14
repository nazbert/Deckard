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

Compatibility surface of the deck controller. This module defines nothing.
The classes, functions and constants live in
src/backend/DeckManagement/deck_controller/, and the imports below re-export
them at the path importers use: the plugin repos take DeckController from
here, the label editor takes KeyLabel, the test harness takes the media
writer and the private hooks it pins.

The second group belongs to other modules. Upstream binds Input, KeyLabel,
MediaConfig and the rest at this exact path, and store plugins import
whatever upstream makes importable. Upstream parity is the rule for that
group: a name bound at this path upstream stays importable here.

__all__ is the compat contract. New code imports from the package modules.
No import here carries a lint suppression: __all__ marks these imports used,
so an import that arrives without an __all__ entry still reports as unused,
and the floor-import check catches an __all__ entry that arrives without an
import. Keep this file to import statements and __all__.
"""
from src.backend.DeckManagement.deck_controller.background_media import (
    Background, BackgroundImage, BackgroundVideo,
)
from src.backend.DeckManagement.deck_controller.controller import CONTROLLER_CLASSES, DeckController
from src.backend.DeckManagement.deck_controller.gif_pipeline import (
    BOUNDED_TILE_VARIANT, GIF_BG_BUDGET_MB, GIF_KEY_BUDGET_MB, GifBackground,
    GifBudgetExceeded, GifTimeline, KeyGIF, _STRIP_GEOMETRY_MISSING,
    contained_size, cumulative_gif_delays, decode_gif_frames, frame_has_alpha,
    gif_frame_walk, gif_header_geometry, gif_key_budget_bytes,
    normalize_gif_delay, probe_gif_timeline, tile_video_size,
)
from src.backend.DeckManagement.deck_controller.inputs import (
    ControllerDial, ControllerDialState, ControllerInput, ControllerInputState,
    ControllerKey, ControllerKeyState, ControllerTouchScreen,
    ControllerTouchScreenState, StateT,
)
from src.backend.DeckManagement.deck_controller.label_engine import (
    BackgroundManager, LabelManager, LayoutManager, _BitmapRecorder,
    _RecordingTooLarge, _label_measure_draw,
)
from src.backend.DeckManagement.deck_controller.media_writer import (
    KEY_ENCODE_QUALITY, ClearAndCloseMsg, ClearMsg, MediaPlayerSetImageTask,
    MediaPlayerSetTouchscreenImageTask, MediaPlayerTask, MediaPlayerThread,
    ReleaseStashedInputsMsg, SetBrightnessMsg, _env_float,
    _install_fair_transport_lock, encode_native_key, encode_native_touchscreen,
)

# Upstream parity, per the docstring: bound at this path there, so bound here.
from src.backend.DeckManagement.BetterDeck import BetterDeck
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from src.backend.DeckManagement.Media.MediaConfig import MediaConfig
from src.backend.DeckManagement.Subclasses.ActionPermissionManager import ActionPermissionManager
from src.backend.DeckManagement.Subclasses.FakeDeck import FakeDeck
from src.backend.DeckManagement.Subclasses.KeyImage import InputImage
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo
from src.backend.DeckManagement.Subclasses.ScreenSaver import ScreenSaver

__all__ = [
    "ActionPermissionManager", "BOUNDED_TILE_VARIANT", "Background",
    "BackgroundImage", "BackgroundManager", "BackgroundVideo", "BetterDeck",
    "CONTROLLER_CLASSES", "ClearAndCloseMsg", "ClearMsg", "ControllerDial",
    "ControllerDialState", "ControllerInput", "ControllerInputState",
    "ControllerKey", "ControllerKeyState", "ControllerTouchScreen",
    "ControllerTouchScreenState", "DeckController", "FakeDeck",
    "GIF_BG_BUDGET_MB", "GIF_KEY_BUDGET_MB", "GifBackground", "GifBudgetExceeded",
    "GifTimeline", "ImageLayout", "Input", "InputEvent", "InputIdentifier",
    "InputImage", "InputVideo", "KEY_ENCODE_QUALITY", "KeyGIF", "KeyLabel",
    "LabelManager", "LayoutManager", "MediaConfig", "MediaPlayerSetImageTask",
    "MediaPlayerSetTouchscreenImageTask", "MediaPlayerTask", "MediaPlayerThread",
    "ReleaseStashedInputsMsg", "ScreenSaver", "SetBrightnessMsg", "StateT",
    "_BitmapRecorder", "_RecordingTooLarge", "_STRIP_GEOMETRY_MISSING",
    "_env_float", "_install_fair_transport_lock", "_label_measure_draw",
    "contained_size", "cumulative_gif_delays", "decode_gif_frames",
    "encode_native_key", "encode_native_touchscreen", "frame_has_alpha",
    "gif_frame_walk", "gif_header_geometry", "gif_key_budget_bytes",
    "normalize_gif_delay", "probe_gif_timeline", "tile_video_size",
]
