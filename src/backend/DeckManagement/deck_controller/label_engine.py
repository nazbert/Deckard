"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

The composition engine turns a page's and an action's declared styling into
the pixels of one input. LabelManager owns the labels: defaults injection,
the epoch-stamped memos, the scroll state, and the blit recorder that keeps
a static label off FreeType on every frame. LayoutManager owns the
foreground, and BackgroundManager the colour behind both. Each one merges a
page layer with an action layer, caches the merge, and notifies the UI port.
This module imports nothing from its sibling modules in the package.
"""
import time
from copy import copy

from PIL import Image, ImageDraw, ImageOps
from loguru import logger as log

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
from src.backend.DeckManagement.Subclasses.KeyLayout import ImageLayout
from src.backend.DeckManagement.Subclasses.media_pipeline_profiler import media_prof
from src.backend import ui_port

import globals as gl

from typing import TYPE_CHECKING, cast
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput

    class ComposedKeyLabel(KeyLabel):
        """A KeyLabel that went through LabelManager.inject_defaults(). Every
        field carries a value, so the render path reads them with no None
        check. It exists under TYPE_CHECKING only and adds no class at
        runtime; inject_defaults() returns the object it filled in. The type
        carries the invariant from the one place that sets it to the eight
        places that rely on it.
        """
        text: str
        font_size: int
        font_name: str
        font_weight: int
        style: str
        color: list[int]
        outline_width: int
        outline_color: list[int]
        alignment: str

    class ComposedImageLayout(ImageLayout):
        """An ImageLayout after LayoutManager.inject_defaults(). Same
        contract and same TYPE_CHECKING-only reason as ComposedKeyLabel."""
        valign: float
        halign: float
        fill_mode: str
        size: float


# Shared text measurement for label layout and scroll detection. textbbox
# computes layout only and never touches the pixels, and it matches what the
# per-key render's own draw context reports. font.getbbox is single-line and
# counts '\n' toward the width, which triggers a phantom scroll.
_label_measure_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


class _RecordingTooLarge(Exception):
    """A label's glyph masks passed the retention budget during a recording.

    The caller drops the partial recording and pins the label to the direct
    per-frame draw. draw_bitmap raises it, so the abort also stops the
    rasterization, which is the expensive half."""


class _BitmapRecorder:
    """Capture the bitmap blits that ImageDraw.text() issues, instead of
    running them.

    ImageDraw.text() runs two steps: FreeType rasterizes the stroked glyph
    run into a coverage mask, which is expensive, then a blit puts that mask
    on the target with a solid ink, which is cheap. A static label re-runs
    both on every media tick, although only the pixels under it change. This
    object stands in for the draw core while text() runs and records the blit
    arguments, so a later frame replays the blit against the rasterized mask.
    It delegates everything else to the real core, including draw_ink(),
    which ImageDraw._getink() calls to resolve the fill colors, so the
    recorded ink matches a direct draw.

    max_ops and max_bytes are the hard retention bound. text() emits one blit
    per line per pass, so the retained bytes and the FreeType work both scale
    with the line count, and this recording runs on the sole device writer.
    Past either bound it raises _RecordingTooLarge at once."""
    __slots__ = ("_core", "ops", "_max_ops", "_max_bytes", "_bytes")

    def __init__(self, core, max_ops: int, max_bytes: int):
        self._core = core
        self.ops: list[tuple] = []
        self._max_ops = max_ops
        self._max_bytes = max_bytes
        self._bytes = 0

    def __getattr__(self, name):
        return getattr(self._core, name)

    def draw_bitmap(self, coord, mask, ink) -> int:
        # The mask is an 8-bit coverage ImagingCore, so 1 byte per pixel.
        self._bytes += mask.size[0] * mask.size[1]
        if len(self.ops) >= self._max_ops or self._bytes > self._max_bytes:
            raise _RecordingTooLarge(
                f"{len(self.ops) + 1} blits / {self._bytes} mask bytes past the "
                f"{self._max_ops}-op / {self._max_bytes}-byte budget")
        self.ops.append((tuple(coord), mask, ink))
        return 0


class LabelManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input
        
        self.page_labels: dict[str, "KeyLabel"] = {}
        self.action_labels: dict[str, "KeyLabel"] = {}
        self.scroll_wait = 25
        # Monotonic stamp for the three latch-style memos below, whose stored
        # value carries no identity of its own. The media thread fills them on
        # the render path and the UI or plugin threads drop them on the edit
        # path, with no lock. A plain None latch loses that race and pins the
        # pre-edit value forever. The reader sees None, composes, the editor
        # invalidates, then the reader stores. So a builder reads the epoch
        # before it composes and publishes (epoch, value), and a reader accepts
        # the memo only while the stamp equals the current epoch. The counter
        # never decreases, so two collapsed increments still leave the epoch
        # past every stamp captured before the change.
        # _bbox_cache, _scroll_strips and _static_ops need no epoch. See
        # _bump_label_epoch().
        self._label_epoch: int = 0
        # (epoch, {position: text width}) for the labels that scroll, which
        # means wider than the key with rolling labels enabled. None means
        # recompute.
        # get_has_scroll_labels() derives from this.
        self._scroll_widths_cache: tuple[int, dict[str, int]] | None = None
        # (epoch, bool): whether any composed label has non-empty text.
        self._has_visible_labels_cache: tuple[int, bool] | None = None
        # {position: (cache key, strip image, ax, ay)}: the label text and
        # outline rasterized once onto a transparent strip. A scroll frame
        # composites a window of it instead of a draw.text, which costs
        # ~2.5ms per key.
        self._scroll_strips: dict[str, tuple] = {}
        # {position: (cache key, blit ops or None)}: the static label's glyph
        # masks, rasterized once and replayed per frame. A per-tick draw.text
        # with stroke costs ~820us per key, ~50% of the tick on a populated
        # animated page. None ops pins this position to the direct draw.
        self._static_ops: dict[str, tuple] = {}
        # {position: (cache key, (w, h))}: the textbbox measurement of the
        # composed label. The FreeType layout pass is the second-biggest
        # per-frame cost, after the raster.
        self._bbox_cache: dict[str, tuple] = {}
        # (epoch, {position: KeyLabel}): the merged page, action and default
        # labels. See get_composed_labels() for the invalidation contract.
        self._composed_labels_cache: tuple[int, dict[str, "ComposedKeyLabel"]] | None = None

        self.init_labels()
        # Rolling-label animation state per position: the scroll offset in
        # whole pixels, and the wall-clock deadline of the next advance. None
        # means fresh, and starts with the leading hold. Wall clock, not tick
        # count, so an event wake that pushes the loop past its nominal FPS
        # cannot change the scroll speed.
        self.frames: dict[str, dict] = {
            "top": {"position": 0, "next_step_at": None},
            "center": {"position": 0, "next_step_at": None},
            "bottom": {"position": 0, "next_step_at": None},
        }

    def init_labels(self):
        for position in ["top", "center", "bottom"]:
            self.page_labels[position] = KeyLabel(self.controller_input)
            self.action_labels[position] = KeyLabel(self.controller_input)
 
    def _bump_label_epoch(self) -> None:
        """Retire the latch-style label memos. Move the epoch, then drop
        them.

        Every site that changes what a composed label looks like must call
        this. A drop without an epoch move reopens the store-after-clear
        window. An epoch move without a drop leaves the pre-edit value
        reachable until the next publish. One method keeps the two together.

        _bbox_cache, _scroll_strips and _static_ops need no epoch and do not
        reset here. Each entry stores its own content key beside the value,
        and every reader re-checks that key on the hit path. The keys cover
        every input to the value: text, resolved font file, which encodes
        family, weight and style, and size for the bbox; plus colors,
        outline, alignment, anchor, absolute draw coordinates and target
        geometry for the blits and the strip. An equal key means equal
        pixels, so a store after a clear can only resurrect an entry that is
        still correct for the current label, or one whose key does not match
        and which the next reader rebuilds. The size caps below bound
        the retained bytes. The reset calls that do exist release memory
        early."""
        self._label_epoch += 1
        self._scroll_widths_cache = None
        self._has_visible_labels_cache = None
        self._composed_labels_cache = None

    def invalidate_scroll_caches(self) -> None:
        """Drop the derived label caches so the next render recomputes scroll
        detection and geometry. Any path that mutates a label's attributes in
        place, and not through set_page_label or set_action_label, must call
        this; Page.set_label_* pokes page_labels[pos] directly and is such a
        path. Without it get_scroll_label_widths() keeps the old overflow set
        and the render composites a stale strip: a shortened label scrolls
        forever, and a lengthened one never starts until a page reload. This
        is cheap. The widths and visible flags recompute lazily, and the
        strip, bbox and static dicts re-key on demand."""
        self._bump_label_epoch()
        self._bbox_cache.clear()
        self._scroll_strips.clear()
        self._static_ops.clear()

    def clear_labels(self):
        self.init_labels()
        self._bump_label_epoch()
        self._scroll_strips.clear()
        self._static_ops.clear()
        self._bbox_cache.clear()

    def set_page_label(self, position: str, label: "KeyLabel", update: bool = True):
        if label is None:
            label = self.page_labels[position]
            label.clear_values()
        else:
            self.page_labels[position] = label

        self._bump_label_epoch()
        self._static_ops.clear()
        if update:
            self.update_label(position)

    @staticmethod
    def _label_equals(a: "KeyLabel", b: "KeyLabel") -> bool:
        return (a.text == b.text and a.font_size == b.font_size
                and a.font_name == b.font_name and a.color == b.color
                and a.font_weight == b.font_weight and a.style == b.style
                and a.outline_width == b.outline_width
                and a.outline_color == b.outline_color
                and a.alignment == b.alignment)

    def set_action_label(self, position: str, label: "KeyLabel", update: bool = True):
        if label is None:
            label = self.action_labels[position]
            label.clear_values()
        else:
            old = self.action_labels.get(position)
            if old is not None and self._label_equals(old, label):
                return
            self.action_labels[position] = label

        self._bump_label_epoch()
        self._static_ops.clear()
        self.update_label_editor()
        if update:
            self.update_label(position)

    def update_label_editor(self):
        """Kept as the caller-facing name; the widget work belongs to the
        adapter. Page.set_label_* calls this on every label styling change,
        at 8 sites, and the trailing update_input repaint runs after it. So
        this must stay a plain forwarder that never raises.
        """
        ui_port.get().on_input_visuals_changed(
            self.controller_input.deck_controller, self.controller_input.identifier,
            self.controller_input.state, "labels")

    def get_use_page_label_properties(self, position: str) -> dict:
        if self.page_labels.get(position) is None:
            return {
                "text": False,
                "color": False,
                "font-family": False,
                "font-size": False,
                "font-weight": False,
                "font-style": False,
                "outline_width": False,
                "outline_color": False,
                "alignment": False,
            }
        return {
            "text": self.page_labels[position].text is not None,
            "color": self.page_labels[position].color is not None,
            "font-family": self.page_labels[position].font_name is not None,
            "font-size": self.page_labels[position].font_size is not None,
            "font-weight": self.page_labels[position].font_weight is not None,
            "font-style": self.page_labels[position].style is not None,
            "outline_width": self.page_labels[position].outline_width is not None,
            "outline_color": self.page_labels[position].outline_color is not None,
            "alignment": self.page_labels[position].alignment is not None,
        }

    def get_composed_label(self, position: str) -> "ComposedKeyLabel":
        use_page_label_properties = self.get_use_page_label_properties(position)
        
        label = copy(self.action_labels.get(position)) or KeyLabel(self.controller_input)

        # Set to page values
        page_label = self.page_labels.get(position)
        if page_label is not None:
            if use_page_label_properties["text"]:
                label.text = page_label.text
            if use_page_label_properties["color"]:
                label.color = page_label.color
            if use_page_label_properties["font-family"]:
                label.font_name = page_label.font_name
            if use_page_label_properties["font-size"]:
                label.font_size = page_label.font_size
            if use_page_label_properties["font-weight"]:
                label.font_weight = page_label.font_weight
            if use_page_label_properties["font-style"]:
                label.style = page_label.style
            if use_page_label_properties["outline_width"]:
                label.outline_width = page_label.outline_width
            if use_page_label_properties["outline_color"]:
                label.outline_color = page_label.outline_color
            if use_page_label_properties["alignment"]:
                label.alignment = page_label.alignment

        injected = self.inject_defaults(label)
        return self.fix_invalid(injected)
    
    def get_composed_labels(self) -> dict[str, "ComposedKeyLabel"]:
        """The merged page, action and default labels for all three
        positions, memoized.

        The merge costs three KeyLabel copies plus the nine settings reads of
        inject_defaults, about 60us per key per media tick. An animated
        background pays that on every frame, although the labels change only
        when something sets one.

        Every label mutation goes through set_page_label, set_action_label or
        clear_labels, or, on the in-place editor path, Page.set_label_*,
        which calls invalidate_scroll_caches(). All of them reach
        _bump_label_epoch(), which retires this memo even against a render
        that is halfway through a compose.

        A change to the app-wide font defaults is not a label mutation and
        needs no separate channel. All four Settings writers that touch
        gl.settings_manager.font_defaults call
        page_manager.reload_all_pages(), and a page reload runs
        create_n_states(), which replaces every input state object and every
        LabelManager with it. A font-defaults change destroys the object that
        holds this memo, and that covers dials and touchscreens too.
        get_scroll_label_widths() documents the weaker assumption for the
        rolling-labels toggle.

        The returned KeyLabels are shared, so treat them as read-only.
        get_composed_label() returns a fresh object per call for a caller
        that mutates one, such as the label editor."""
        memo = self._composed_labels_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        # Read the epoch before the compose and stamp with it. An
        # invalidation during the compose moves the epoch past this stamp, so
        # the store below publishes a value that every reader rejects.
        epoch = self._label_epoch
        labels = {
            position: self.get_composed_label(position)
            for position in ("top", "center", "bottom")
        }
        self._composed_labels_cache = (epoch, labels)
        # Return the dict built here, not the attribute, so a concurrent
        # publish cannot swap the result mid-call.
        return labels

    
    def inject_defaults(self, label: "KeyLabel") -> "ComposedKeyLabel":
        """Fill every unset field from the app-wide font defaults, in place.
        Returns the same object, retyped; no field is left for a reader to
        None-check. See ComposedKeyLabel."""
        if label.text is None:
            label.text = ""
        if label.color is None:
            # Use a list, not a tuple. The field declares list[int] and the
            # settings path yields a JSON list, so a tuple fallback makes the
            # runtime type depend on the branch that filled it.
            label.color = gl.settings_manager.font_defaults.get("font-color") or [255, 255, 255, 255]
        if label.font_name is None:
            label.font_name = gl.settings_manager.font_defaults.get("font-family") or gl.fallback_font
        if label.font_size is None:
            label.font_size = round(gl.settings_manager.font_defaults.get("font-size") or 15)
        if label.font_weight is None:
            label.font_weight = round(gl.settings_manager.font_defaults.get("font-weight") or 400)
        if label.style is None:
            label.style = gl.settings_manager.font_defaults.get("font-style") or "normal"
        if label.outline_width is None:
            label.outline_width = round(gl.settings_manager.font_defaults.get("outline-width") or 2)
        if label.outline_color is None:
            label.outline_color = gl.settings_manager.font_defaults.get("outline-color") or [0, 0, 0, 255]
        if label.alignment is None:
            label.alignment = gl.settings_manager.font_defaults.get("alignment") or "center"

        return cast("ComposedKeyLabel", label)
    
    def fix_invalid(self, label: "ComposedKeyLabel") -> "ComposedKeyLabel":
        if not isinstance(label.text, str):
            label.text = str(label.text)

        return label

    def update_label(self, position: str):
        self.controller_input.update()

    def get_available_width(self) -> int:
        return self.controller_input.get_image_size()[0]

    def get_has_visible_labels(self) -> bool:
        # add_labels_to_image draws a label only when its text is non-empty.
        # A stale False sends ControllerKey._tile_passthrough_ok down the
        # bare-tile path, so the labelled key renders as an empty tile until
        # the next invalidation. The epoch stamp stops that.
        memo = self._has_visible_labels_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        epoch = self._label_epoch
        labels = self.get_composed_labels()
        visible = any(label.text not in (None, "") for label in labels.values())
        self._has_visible_labels_cache = (epoch, visible)
        return visible

    def _measure_text(self, position: str, label: "ComposedKeyLabel") -> tuple[int, int]:
        """(w, h) of the composed label's rendered text block, cached per
        position. Scroll detection and the render path both measure here, so
        they cannot disagree about whether a label overflows."""
        font = label.get_font()
        key = (label.text, getattr(font, "path", None), getattr(font, "size", None))
        cached = self._bbox_cache.get(position)
        if cached is not None and cached[0] == key:
            return cached[1]
        _, _, w, h = _label_measure_draw.textbbox((0, 0), label.text, font=font)
        # textbbox declares floats because it adds a possibly fractional
        # origin. This call anchors at integer (0, 0) with an integer glyph
        # bbox, so the values are already ints and int() changes nothing.
        measured = (int(w), int(h))
        self._bbox_cache[position] = (key, measured)
        return measured

    def get_scroll_label_widths(self) -> dict[str, int]:
        """Text widths of the composed labels that scroll, which means
        rolling labels are enabled and the rendered text is wider than the
        input. This measures with the same multiline-aware textbbox the
        render path uses, so detection cannot flag a label that the render
        draws statically. That mismatch holds the media loop at full FPS on
        identical frames."""
        # A label edit reaches invalidate_scroll_caches(), through
        # set_page_label, set_action_label or the Page.set_label_* setters. A
        # rolling-labels toggle reaches reload_page(), which rebuilds these
        # managers. A rolling-labels change outside the Settings dialog, from
        # a direct settings.json edit or a plugin, does not reload the page,
        # and leaves this cache stale until the next label edit or page load.
        # That path is not a supported runtime toggle. The epoch stamp stops
        # a store after a concurrent edit from pinning the pre-edit set.
        memo = self._scroll_widths_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        epoch = self._label_epoch

        widths: dict[str, int] = {}
        rolling_labels_enabled = gl.settings_manager.app().rolling_labels
        if rolling_labels_enabled:
            available_width = self.get_available_width()
            labels = self.get_composed_labels()
            for position in labels:
                text = labels[position].text
                if text in (None, ""):
                    continue
                w, _ = self._measure_text(position, labels[position])
                if w > available_width:
                    widths[position] = w
        self._scroll_widths_cache = (epoch, widths)
        return widths

    def get_has_scroll_labels(self) -> bool:
        return len(self.get_scroll_label_widths()) > 0

    # The scroll cadence in wall time, not loop iterations. At the nominal
    # 30 FPS it advances 1px per two ticks and holds for scroll_wait ticks.
    # Wall clock keeps the speed stable when event wakes push the loop past
    # its nominal rate.
    _NOMINAL_TICK_RATE = 30.0
    SCROLL_STEP_SECONDS = 2.0 / _NOMINAL_TICK_RATE

    def _scroll_hold_start_seconds(self) -> float:
        return self.scroll_wait * 2.0 / self._NOMINAL_TICK_RATE

    def _scroll_hold_end_seconds(self) -> float:
        return self.scroll_wait / self._NOMINAL_TICK_RATE

    def tick_scroll_labels(self) -> bool:
        """Advance the rolling-label animation and report whether a visible
        scroll offset changed, which means a re-render. This is the only place
        that moves scroll state, because rendering is pure. The hold plateaus
        and the between-step ticks then cost integer and time math here,
        instead of a composite that the hash de-dup drops."""
        changed = False
        now = time.monotonic()
        available_width = self.get_available_width()
        for position, w in self.get_scroll_label_widths().items():
            frame = self.frames[position]
            # The sweep runs from x=start, 10px right of centered, down to
            # one pixel past x=stop, 10px left of centered. So overshoot is
            # start minus stop.
            overshoot = w - available_width + 20
            next_at = frame.get("next_step_at")
            if next_at is None:
                # A fresh label holds at the start position first.
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
                continue
            if now < next_at:
                continue
            if frame["position"] > overshoot:
                # The trailing hold elapsed. Snap back to the start and
                # hold.
                frame["position"] = 0
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
            else:
                frame["position"] += 1
                if frame["position"] > overshoot:
                    frame["next_step_at"] = now + self._scroll_hold_end_seconds()
                else:
                    frame["next_step_at"] += self.SCROLL_STEP_SECONDS
                    # Re-anchor instead of a burst to catch up after a loop
                    # stall from a page switch or a suspend.
                    if frame["next_step_at"] < now - 0.5:
                        frame["next_step_at"] = now
            changed = True
        return changed

    # A precomposed strip costs width x key height x 4 bytes, retained per
    # label position per state for the whole sweep. Strip width scales with
    # text length. A pasted 50k-character label retains ~95 MB and stalls the
    # sole-writer media thread for seconds. Past this width the label falls
    # back to the direct per-frame draw, which retains nothing and pays raster
    # CPU per frame. 4096 px is ~290 'm' glyphs at font 15, and caps the strip
    # near ~1.6 MB on a 100px-tall SD+ dial image.
    _MAX_STRIP_WIDTH = 4096
    # The width cap bounds one axis only. Strip height tracks the composed
    # text block, so a 2000-line label measures ~42000 px tall and costs a
    # ~700 MB strip. 4 MiB leaves ~2.5x headroom over the widest strip the
    # width cap allows, 4096 x 100 x 4 = 1.6 MB on an SD+ dial image, and it
    # rejects the tall case.
    _MAX_STRIP_BYTES = 4 * 1024 * 1024

    # The same bound for the recorded static-label blits. A glyph mask costs
    # 1 byte per pixel, not the 4 of an RGBA canvas, but it scales with the
    # line count the same way. The recording also costs FreeType time on the
    # sole device writer. Measured headless, 500 lines gave 1000 blits, 430 KB
    # and 0.22 s; 2000 lines gave 4000 blits, 2.0 MB and 1.06 s; 20k lines
    # gave 32 MB and a 5 s media-thread stall.
    #
    # 512 KiB per position is ~6x the largest readable label on an input. A
    # 200x200 dial image full of glyphs costs ~40k px per pass, ~80 KB for the
    # stroke and fill pair. It also covers a single-line label stretched to
    # the full 4096 px width cap, ~240 KB. Three positions give a 1.5 MiB
    # ceiling per input state, bounded on both axes. 512 ops is 256 lines
    # times the stroke and fill passes; a label that tall is past any key.
    #
    # Two checks apply it. _label_ops_budget_ok runs before the recording,
    # from measurements the render already has, and a hard abort inside the
    # recorder bounds the wall time.
    _MAX_LABEL_MASK_BYTES = 512 * 1024
    _MAX_LABEL_OPS = 512

    def _label_ops_budget_ok(self, label: "ComposedKeyLabel", w: int, h: int) -> bool:
        """Whether a recording of this label's blits is worth the retention
        and the media-thread stall, decided from measurements the caller
        already has, with no rasterization.

        text() masks each line separately and runs the whole thing twice when
        there is an outline, so the op count is 2 per line and the stroke
        padding costs per line, not once per block. That over-estimates the
        real mask total, about 2x on measured cases, because a mask is the
        glyph run's tight bbox and not the block rectangle. An over-estimate
        is the safe direction here, because _BitmapRecorder enforces the
        exact bound."""
        lines = label.text.count("\n") + 1
        if lines * 2 > self._MAX_LABEL_OPS:
            return False
        stroke = label.outline_width or 0
        estimated_bytes = (2 * (int(w) + 2 * stroke)
                           * (int(h) + lines * 2 * stroke))
        return estimated_bytes <= self._MAX_LABEL_MASK_BYTES

    def _composite_scroll_strip(self, image: Image.Image, position: str, label: "ComposedKeyLabel",
                                w: int, h: int, x_position: float, y_position: float) -> None:
        """Draw a scrolling label by compositing a window of its precomposed
        text strip at this tick's offset. The strip rasterizes once per
        (text, font, colors) and serves every frame of the sweep. A direct
        draw.text with stroke costs ~2.5ms per frame and the composite
        ~0.014ms, with identical pixels, because the target coordinates keep
        constant fractional parts across the sweep and bake them into the
        strip, so the paste offset is always a whole pixel.

        The composite matches a direct draw for opaque ink only. A
        semi-transparent fill or outline, which only the plugin set_label API
        or hand-edited page JSON can set, blends with straight-alpha OVER
        here and with PIL's coverage blend in draw.text, so a scrolling frame
        differs from the static draw. _draw_static_label caches one layer
        lower, the glyph masks rather than a composited strip, which is exact
        for any ink; it needs a fixed paste position, so it does not
        generalize back to the sweep."""
        font = label.get_font()
        outline_width = label.outline_width
        pad = outline_width + 6

        strip_width = int(w) + 2 * pad + 1
        strip_height = int(h) + 2 * pad + 1
        if strip_width > self._MAX_STRIP_WIDTH or \
                strip_width * strip_height * 4 > self._MAX_STRIP_BYTES:
            # Pathological label. Skip the strip cache and draw the text
            # directly at the scroll offset. This retains nothing and keeps
            # the pixels correct, at the cost of raster CPU per frame. The
            # byte test covers a many-line label, whose block grows
            # vertically and slips past the width cap.
            self._scroll_strips.pop(position, None)
            ImageDraw.Draw(image).text((x_position, y_position), text=label.text,
                                       font=font, anchor="mm", align=label.alignment,
                                       fill=tuple(label.color),
                                       stroke_width=outline_width,
                                       stroke_fill=tuple(label.outline_color))
            return

        ay_base = pad + h / 2
        dy = (y_position - ay_base) % 1.0
        key = (label.text, getattr(font, "path", None), label.font_size,
               tuple(label.color), outline_width, tuple(label.outline_color),
               label.alignment, w, h, dy)
        cached = self._scroll_strips.get(position)
        if cached is None or cached[0] != key:
            ax = pad + w / 2
            ay = ay_base + dy
            # Antialiased edge pixels blend toward the canvas color.
            # Pre-fill with the outermost ink color at alpha 0, so the strip
            # edges match a direct draw onto the key image.
            edge = tuple(label.outline_color[:3]) if outline_width > 0 else tuple(label.color[:3])
            strip = Image.new("RGBA", (int(w) + 2 * pad + 1, int(h) + 2 * pad + 1), edge + (0,))
            ImageDraw.Draw(strip).text((ax, ay), text=label.text, font=font,
                                       anchor="mm", align=label.alignment,
                                       fill=tuple(label.color),
                                       stroke_width=outline_width,
                                       stroke_fill=tuple(label.outline_color))
            cached = (key, strip, ax, ay)
            self._scroll_strips[position] = cached
        _, strip, ax, ay = cached

        px = round(x_position - ax)
        py = round(y_position - ay)
        # Crop to the visible window. In-place alpha_composite needs a
        # non-negative destination and does the correct straight-alpha OVER.
        # A paste with a mask under-writes the alpha on antialiased edges.
        crop_left, crop_top = max(0, -px), max(0, -py)
        crop_right = min(strip.width, image.width - px)
        crop_bottom = min(strip.height, image.height - py)
        if crop_right <= crop_left or crop_bottom <= crop_top:
            return
        if (crop_left, crop_top, crop_right, crop_bottom) != (0, 0, strip.width, strip.height):
            window = strip.crop((crop_left, crop_top, crop_right, crop_bottom))
        else:
            window = strip
        if image.mode == "RGBA":
            image.alpha_composite(window, (px + crop_left, py + crop_top))
        else:
            image.paste(window, (px + crop_left, py + crop_top), window)

    def _draw_static_label(self, image: Image.Image, draw: ImageDraw.ImageDraw,
                           position: str, label: "ComposedKeyLabel", w: int, h: int,
                           x_position: float, y_position: float, anchor: str) -> None:
        """Draw a non-scrolling label by replaying its cached glyph blits.

        A static label's pixels are a pure function of the text, the font,
        the colors, the outline, the alignment and the image geometry, and
        none of those change between media ticks. A per-frame draw.text()
        re-rasterizes the stroked glyph run at ~820us per key, ~50% of the
        whole tick on a populated page over an animated background. This
        records the rasterization once, through _BitmapRecorder standing in
        for the draw core, and a later frame replays only the mask blits at
        ~11us.

        The replay issues the identical C blits, with the identical masks,
        inks and absolute coordinates that draw.text() issues, in the same
        order. So this path is exact for any ink and needs no
        semi-transparent carve-out, unlike the scroll strip. A precomposed
        RGBA strip cannot be exact here. Where a partly covered fill pixel
        overdraws a partly covered stroke pixel, the strip collapses two
        coverage blends into one straight-alpha value, measured up to 19/255
        off a direct draw.

        It falls back to the direct draw, and retains no cache, for a label
        past the width cap or past the retention and op budget, and if PIL's
        text() stops routing through draw_bitmap.

        The non-RGB and non-RGBA guard keeps behavior parity, not a working
        fallback. The recorded ink resolves for the target mode, a palette
        index for "P" and a packed int for "RGB" and "RGBA", so a recording
        is valid only on the mode that took it. An "L" target raises, because
        a direct draw.text with an RGBA fill tuple onto an "L" image is a
        TypeError. In the app every label target is RGBA."""
        if image.mode not in ("RGB", "RGBA") or \
                int(w) + 2 * (label.outline_width + 6) + 1 > self._MAX_STRIP_WIDTH or \
                not self._label_ops_budget_ok(label, w, h):
            self._static_ops.pop(position, None)
            if media_prof:
                media_prof.count("label_ops_fallback")
            draw.text((x_position, y_position), text=label.text, font=label.get_font(),
                      anchor=anchor, align=label.alignment, fill=tuple(label.color),
                      stroke_width=label.outline_width,
                      stroke_fill=tuple(label.outline_color))
            return

        font = label.get_font()
        # The image size and the measured text give the x, y and anchor. The
        # key holds them directly, so a resized deck image or a re-measured
        # label cannot replay blits at stale coordinates.
        key = (label.text, getattr(font, "path", None), label.font_size,
               tuple(label.color), label.outline_width, tuple(label.outline_color),
               label.alignment, anchor, x_position, y_position,
               image.size, image.mode, w, h)
        cached = self._static_ops.get(position)
        if cached is None or cached[0] != key:
            ops = self._record_label_blits(
                image, label, font, (x_position, y_position), anchor)
            # Memoize a failed recording as None under the same key. The
            # attempt costs hundreds of milliseconds, so it must not repeat
            # on every frame of an animated background.
            self._static_ops[position] = (key, ops)
            # Only a recording that produced replayable blits counts as a
            # cache miss. A failed one counts as a fallback below. One
            # counter for both makes a permanently-uncacheable label read as
            # a healthy warm cache in the profile.
            if media_prof and ops is not None:
                media_prof.count("label_ops_miss")
        else:
            ops = cached[1]
            if media_prof and ops is not None:
                media_prof.count("label_ops_hit")

        if ops is None:
            # No recording is available for this label. Draw it directly,
            # and do not attempt the recording again on every frame.
            if media_prof:
                media_prof.count("label_ops_fallback")
            draw.text((x_position, y_position), text=label.text, font=font,
                      anchor=anchor, align=label.alignment, fill=tuple(label.color),
                      stroke_width=label.outline_width,
                      stroke_fill=tuple(label.outline_color))
            return

        core = draw.draw
        for coord, mask, ink in ops:
            core.draw_bitmap(coord, mask, ink)

    def _record_label_blits(self, image: Image.Image, label: "ComposedKeyLabel", font,
                            xy: tuple, anchor: str) -> tuple | None:
        """Run draw.text() against a throwaway target whose draw core only
        records the mask blits, and return them. None means not recordable,
        so the caller draws directly.

        The probe target matches the real image's mode, because the ink
        resolves for the mode. It matches the real size because the probe is
        also the interception tripwire. A recording is safe to replay only if
        the recorder saw the whole draw, and a blank probe is the proof.
        Anything text() writes through another channel lands on this probe as
        residue, and only a full-size probe still shows it; PIL's
        embedded-color route pastes onto the target image directly and
        bypasses the draw core. A 1x1 probe records the identical ops,
        because text() derives the blit coordinates from xy, anchor and mask
        rather than from the canvas, but it clips every escaped write away
        and blinds this check.

        So the bar is non-empty ops and a blank probe. An empty ops list
        alone catches total loss only. A stroke pass that records while the
        fill pass escapes would cache an outline-only label forever.

        Residue detection is one-sided, because black ink on an "RGB" probe
        is invisible to getbbox(), so it can never reject a good recording,
        only miss a bad one. Every in-app label target is RGBA, where escaped
        ink moves the alpha channel off zero."""
        try:
            probe_image = Image.new(image.mode, image.size)
            probe = ImageDraw.Draw(probe_image)
            recorder = _BitmapRecorder(probe.draw, self._MAX_LABEL_OPS,
                                       self._MAX_LABEL_MASK_BYTES)
            probe.draw = recorder
            probe.text(xy, text=label.text, font=font, anchor=anchor,
                       align=label.alignment, fill=tuple(label.color),
                       stroke_width=label.outline_width,
                       stroke_fill=tuple(label.outline_color))
            ops = tuple(recorder.ops)
            residue = probe_image.getbbox()
        except _RecordingTooLarge as too_large:
            # Expected for a pathological label that passed the cheap
            # pre-check. Drop the partial recording here, and the caller pins
            # the label to the direct draw.
            log.info(f"Label blit recording exceeded the retention budget "
                     f"({too_large}); falling back to the per-frame draw for "
                     f"this label")
            return None
        except Exception:
            # Use log.opt(exception=True), not exc_info. loguru has no
            # exc_info keyword and treats it as a format argument, which
            # drops the traceback this fallback exists to surface.
            log.opt(exception=True).warning(
                "Label blit recording failed; falling back to the per-frame "
                "draw for this label")
            return None
        if not ops or residue is not None:
            # No blit at all for non-empty text, or pixels on the probe.
            # Both mean PIL took a path this recorder does not model, such as
            # embedded-color glyphs. A replay erases the label, or keeps only
            # the intercepted half.
            log.warning(
                f"Label blit recording did not intercept the whole draw "
                f"({len(ops)} ops, probe residue {residue}); falling back to "
                f"the per-frame draw for this label")
            return None
        return ops

    def add_labels_to_image(self, image: Image.Image) -> Image.Image:
        # image = image.rotate(self.deck.get_rotation()*-1)
        if not self.get_has_visible_labels():
            # Nothing to draw. Return the caller's own image, not a key-sized
            # RGBA copy per frame. ControllerKey.get_current_image knows the
            # result can be its input and skips the matching close() calls.
            # Every other caller passes it straight through.
            return image

        draw = ImageDraw.Draw(image)

        labels = self.get_composed_labels()
        scroll_widths = self.get_scroll_label_widths()
        for label in labels:
            text = labels[label].text
            if text in [None, ""]:
                continue

            alignment = labels[label].alignment

            w, h = self._measure_text(label, labels[label])

            # The static and scrolling paths share the vertical placement.
            if label == "top":
                y_position = h/2 + 3
            elif label == "bottom":
                y_position = image.height - h/2 - 3
            else:
                y_position = (image.height - 0) / 2

            if label in scroll_widths:
                # Rolling label. Composite the precomposed strip at this
                # tick's offset. Only tick_scroll_labels() advances the
                # scroll state, because rendering is pure, so a paint from a
                # key press or a page load cannot disturb the animation.
                start = image.width / 2 - (image.width - w) / 2 + 10
                x_position = start - self.frames[label]["position"]
                self._composite_scroll_strip(image, label, labels[label], w, h,
                                             x_position, y_position)
                continue

            # Set the x position from the alignment.
            padding = 3
            if alignment == "left":
                x_position = padding
                anchor_x = "l"
            elif alignment == "right":
                x_position = image.width - padding
                anchor_x = "r"
            else:  # center (default)
                x_position = image.width / 2
                anchor_x = "m"

            # The anchor is the x-anchor plus "m" for the vertical middle.
            anchor = anchor_x + "m"

            self._draw_static_label(image, draw, label, labels[label], w, h,
                                    x_position, y_position, anchor)

        del draw

        # The copy exists for ControllerKey.get_current_image. This method
        # draws in place, and that caller closes the buffer it passed in as
        # soon as the labelled result is a different object. The touchscreen
        # caller rebinds the name and never closes, so the copy is redundant
        # on that path. Only an input that carries a label reaches here.
        return image.copy()
        # return image.copy().rotate(self.deck.get_rotation())


class LayoutManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input

        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()

        # (token, layout key, resized image) for the resized foreground of a
        # static asset. It stays valid while the caller passes the same asset
        # object, the same backing source image and the same layout geometry;
        # an in-place re-decode swaps the source image. One tuple, so a
        # concurrent update swaps it atomically.
        self._fg_cache: tuple | None = None

    def clear(self):
        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()
        self._fg_cache = None

    def get_use_page_layout_properties(self) -> dict:
        return {
            "valign": self.page_layout.valign is not None,
            "halign": self.page_layout.halign is not None,
            "fill-mode": self.page_layout.fill_mode is not None,
            "size": self.page_layout.size is not None
        }
    
    def get_composed_layout(self) -> "ComposedImageLayout":
        use_page_layout_properties = self.get_use_page_layout_properties()
        
        layout = copy(self.action_layout) or ImageLayout()

        # Set to page values
        page_layout = self.page_layout
        if use_page_layout_properties["valign"]:
            layout.valign = page_layout.valign
        if use_page_layout_properties["halign"]:
            layout.halign = page_layout.halign
        if use_page_layout_properties["fill-mode"]:
            layout.fill_mode = page_layout.fill_mode
        if use_page_layout_properties["size"]:
            layout.size = page_layout.size

        return self.inject_defaults(layout)
    
    def inject_defaults(self, layout: ImageLayout) -> "ComposedImageLayout":
        """Fill every unset field, in place, and return the same object
        retyped. See ComposedImageLayout."""
        if layout.valign is None:
            layout.valign = 0
        if layout.halign is None:
            layout.halign = 0
        if layout.fill_mode is None:
            if isinstance(self.controller_input.identifier, Input.Key):
                layout.fill_mode = "cover"
            else:
                layout.fill_mode = "contain"
        if layout.size is None:
            layout.size = 1

        return cast("ComposedImageLayout", layout)
    
    def set_page_layout(self, layout: ImageLayout, update: bool = True):
        self.page_layout = layout

        if update:
            self.update()

    def set_action_layout(self, layout: ImageLayout, update: bool = True):
        self.action_layout = layout

        if update:
            self.update()

    def update(self):
        self.controller_input.update()
        ui_port.get().on_input_visuals_changed(
            self.controller_input.deck_controller, self.controller_input.identifier,
            self.controller_input.state, "layout")

    def add_image_to_background(self, image: Image.Image | None, background: Image.Image, cache_token=None) -> Image.Image:
        if image is None:
            return background
        layout = self.get_composed_layout()

        width, height = background.size
        image_size = (int(width * layout.size), int(height * layout.size))

        if 0 in image_size:
            return background.copy()

        # The resized foreground depends only on the source asset and the
        # layout, not on the background, which can animate. cache_token is the
        # asset object itself, and the identity check below holds it alive, so
        # it cannot collide the way a freed id() can.
        #
        # cache_token alone cannot key the resized foreground.
        # InputImage._ensure_fits_composed() re-decodes and swaps its backing
        # image in place, so the asset object stays identical while its pixels
        # change to a higher resolution. fg_key must also track which source
        # image it resized, or a composite after the swap gets the stale
        # low-res entry. A swap only grows the image today, so image_size
        # already differs across one, but a same-size re-decode would not
        # change image_size. id(image) states the dependency explicitly, and
        # while cache_token is alive it holds a strong reference to image, so
        # no other object can take this id.
        fg_key = (layout.fill_mode, layout.halign, layout.valign, image_size,
                  id(image), image.size)
        image_resized = None
        if cache_token is not None:
            cached = self._fg_cache
            if cached is not None and cached[0] is cache_token and cached[1] == fg_key:
                image_resized = cached[2]
                if media_prof:
                    media_prof.count("fg_cache_hit")

        if image_resized is None:
            if layout.fill_mode == "stretch":
                image_resized = image.resize(image_size, Image.Resampling.HAMMING)
            elif layout.fill_mode == "cover":
                image_resized = ImageOps.cover(image, image_size, Image.Resampling.HAMMING)
            else:
                image_resized = ImageOps.contain(image, image_size, Image.Resampling.HAMMING)
            if cache_token is not None:
                self._fg_cache = (cache_token, fg_key, image_resized)
                if media_prof:
                    media_prof.count("fg_cache_miss")

        halign = layout.halign
        valign = layout.valign

        left_margin = int((background.width - image_resized.width) * (halign + 1) / 2)
        top_margin = int((background.height - image_resized.height) * (valign + 1) / 2)

        final_image = background.copy()

        if image_resized.has_transparency_data:
            final_image.paste(image_resized, (left_margin, top_margin), image_resized)
        else:
            final_image.paste(image_resized, (left_margin, top_margin))

        return final_image
    

class BackgroundManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input
        
        self.action_color: list[int] | None = None
        self.page_color: list[int] | None = None

    def set_action_color(self, color: list[int], update: bool = True) -> None:
        self.action_color = color
        if isinstance(color, list) and len(color) == 3:
            self.action_color.append(255)

        if update:
            self.update()

    def set_page_color(self, color: list[int], update: bool = True, update_ui: bool = True) -> None:
        self.page_color = color
        if isinstance(color, list) and len(color) == 3:
            self.page_color.append(255)

        if update:
            self.update(ui=update_ui)

    def update(self, ui: bool = True):
        self.controller_input.update()
        if ui:
            ui_port.get().on_input_visuals_changed(
                self.controller_input.deck_controller, self.controller_input.identifier,
                self.controller_input.state, "background")

    def get_color_is_set(self, color: list[int] | None) -> bool:
        return color not in [None, [None]*3, [None]*4]

    def get_use_page_background(self) -> bool:
        return self.get_color_is_set(self.page_color)
    
    def get_composed_color(self) -> list[int]:
        # get_color_is_set reports False for None, so the is-not-None tests
        # are implied. They are explicit here so the returns are provably
        # non-None.
        page_color = self.page_color
        action_color = self.action_color
        if self.get_use_page_background() and page_color is not None and self.get_color_is_set(page_color):
            return page_color
        elif action_color is not None and self.get_color_is_set(action_color):
            return action_color
        else:
            return [0] * 4
