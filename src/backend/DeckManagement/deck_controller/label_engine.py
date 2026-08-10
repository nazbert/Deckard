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

---

The composition engine: everything that turns a page's and an action's
declared styling into the pixels of one input. LabelManager owns the label
half -- defaults injection, the epoch-stamped memos, scroll state and the
blit recorder that keeps a static label off FreeType on every frame --
LayoutManager the foreground half, and BackgroundManager the colour behind
both. They sit together because they are one family: each merges a page
layer with an action layer, each caches the merge, and each notifies the UI
port the same way.

A leaf of the deck_controller package: it imports nothing from its siblings.
The input that owns a manager is duck-typed attribute access, which is why
the only thing this module knows about it is the type-only import below.
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
        """A KeyLabel that has been through LabelManager.inject_defaults():
        every field is populated, so the render path can read them without a
        None check at each use.

        Declared under TYPE_CHECKING only -- it adds no class at runtime, and
        inject_defaults() keeps returning the very object it filled in. This
        exists so "composed" is a type the checker can carry from the one
        place the invariant is established to the eight places that rely on
        it, instead of eight narrowings that would each have to re-assert it.
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
        """An ImageLayout after LayoutManager.inject_defaults(). Same contract
        and same TYPE_CHECKING-only rationale as ComposedKeyLabel."""
        valign: float
        halign: float
        fill_mode: str
        size: float


# Shared, context-independent text measurement for label layout / scroll
# detection: textbbox only computes layout (it never touches the pixels), and
# it matches what the per-key render's own draw context would report --
# unlike font.getbbox, which is single-line and counts '\n' toward the width
# (the phantom-scroll trigger).
_label_measure_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


class _RecordingTooLarge(Exception):
    """A label's glyph masks blew the retention budget mid-recording.

    Not an error condition: the caller drops the partial recording and pins
    the label to the direct per-frame draw. Raised from inside draw_bitmap so
    the abort also stops the RASTERIZATION -- the expensive half -- rather
    than letting text() finish and discarding the result afterwards."""


class _BitmapRecorder:
    """Captures the bitmap blits ImageDraw.text() would issue, instead of
    running them.

    ImageDraw.text() is two steps: rasterize the (stroked) glyph run into a
    coverage mask via FreeType -- expensive -- and blit that mask onto the
    target with a solid ink -- cheap. A static label re-runs both on every
    media tick even though only the pixels UNDER it changed. Standing in for
    the draw core while text() runs records step 2's arguments, so later
    frames can replay the blit with the mask already rasterized.

    Everything else is delegated to the real core object -- notably
    draw_ink(), which ImageDraw._getink() calls to resolve the fill colors,
    so the recorded ink is exactly the one a direct draw would have used.

    max_ops/max_bytes are the hard retention bound (see
    LabelManager._MAX_LABEL_OPS / _MAX_LABEL_MASK_BYTES): text() emits one
    blit per line per pass, so both the retained bytes and the FreeType work
    scale with the line count, and this recording runs on the sole device
    writer. Exceeding either raises _RecordingTooLarge on the spot."""
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
        # Monotonic version stamp for the three LATCH-style memos below --
        # the ones whose stored value carries no identity of its own, so a
        # reader cannot tell fresh data from resurrected data.
        #
        # Those memos are filled lazily on the RENDER path (media thread) and
        # dropped on the EDIT path (UI/plugin threads), unlocked. A plain
        # None-latch loses the race: reader sees None, composes, editor
        # invalidates, reader stores -- and the pre-edit value is now pinned
        # FOREVER. Reproduced in review round 2 for _composed_labels_cache
        # (the old label text stays on screen) and it pre-exists on main for
        # _has_visible_labels_cache (a stale False makes
        # ControllerKey._tile_passthrough_ok paint a labelled key as a bare
        # tile, permanently).
        #
        # The fix is to stamp every publication with the epoch it was
        # computed UNDER: builders capture the epoch BEFORE composing and
        # publish (epoch, value); readers accept the memo only while its
        # stamp still equals the current epoch. A late store therefore
        # publishes a stamp the readers already reject. Two concurrent
        # increments can collapse into one -- harmless, because the counter
        # never decreases, so any change still leaves the epoch past every
        # stamp captured before it. No lock, no ordering assumption.
        #
        # _bbox_cache / _scroll_strips / _static_ops deliberately do NOT need
        # this; see _bump_label_epoch().
        self._label_epoch: int = 0
        # (epoch, {position -> text width}), for composed labels that are
        # wider than the key AND rolling labels are enabled -- i.e. the
        # labels that actually scroll. None = needs recompute (invalidated
        # with the label setters; a rolling-labels toggle lands via
        # reload_page, which rebuilds these managers).
        # get_has_scroll_labels() derives from this.
        self._scroll_widths_cache: tuple[int, dict[str, int]] | None = None
        # (epoch, bool): whether any composed label has non-empty text.
        self._has_visible_labels_cache: tuple[int, bool] | None = None
        # position -> (cache key, strip image, ax, ay): the label's text +
        # outline rasterized ONCE onto a transparent strip; scroll frames
        # composite a window of it instead of re-running draw.text
        # (the per-tick raster was ~2.5ms per key).
        self._scroll_strips: dict[str, tuple] = {}
        # position -> (cache key, blit ops | None): the STATIC label's glyph
        # masks, rasterized once and replayed per frame (the
        # per-tick draw.text with stroke was ~820us per key, ~50% of the tick
        # on a populated animated page). None ops = this position is pinned
        # to the direct draw (see _draw_static_label).
        self._static_ops: dict[str, tuple] = {}
        # position -> (cache key, (w, h)): textbbox measurement of the
        # composed label; the freetype layout pass is the second-biggest
        # per-frame cost after the raster itself.
        self._bbox_cache: dict[str, tuple] = {}
        # (epoch, {position -> KeyLabel}): the merged page+action+defaults
        # labels. See get_composed_labels() for the invalidation contract.
        self._composed_labels_cache: tuple[int, dict[str, "ComposedKeyLabel"]] | None = None

        self.init_labels()
        # Rolling-label animation state per position: the current scroll
        # offset in whole pixels, and the wall-clock deadline of the next
        # advance (None = fresh, starts with the leading hold). Wall-clock
        # (not tick-count) so the scroll speed doesn't change with the media
        # loop's actual iteration rate, which event wakes can push past FPS.
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
        """Retire the latch-style label memos: move the epoch, then drop
        them.

        Every site that changes what a composed label looks like must go
        through here -- dropping a memo without moving the epoch reopens the
        store-after-clear window (see _label_epoch), and moving the epoch
        without dropping the memo would leave the pre-edit value reachable
        until the next successful publish. One method so the two can never
        drift apart.

        _bbox_cache, _scroll_strips and _static_ops are NOT reset here and do
        NOT need the epoch: each entry stores its own CONTENT KEY alongside
        the value and every reader re-checks that key on the hit path. Their
        keys cover every input to what they hold -- text, resolved font file
        (which is what encodes family/weight/style) and size for the bbox,
        plus colors, outline, alignment, anchor, absolute draw coordinates
        and target geometry for the blits/strip -- so equal key implies equal
        pixels. A store that lands after a clear can therefore only
        resurrect an entry that is still CORRECT for the current label (the
        next reader hits it and skips a recompute) or one whose key no longer
        matches (the next reader misses and rebuilds). Neither is a
        correctness bug, and the retained bytes are bounded by the size caps
        below. The reset calls that do exist are eager memory release, not a
        correctness requirement."""
        self._label_epoch += 1
        self._scroll_widths_cache = None
        self._has_visible_labels_cache = None
        self._composed_labels_cache = None

    def invalidate_scroll_caches(self) -> None:
        """Drop the derived label caches so the next tick/render recomputes
        scroll detection and geometry. Any code path that mutates a label's
        attributes IN PLACE (i.e. not through set_page_label/set_action_label
        -- notably Page.set_label_* poking page_labels[pos].<attr> directly)
        must call this, or get_scroll_label_widths() keeps returning the old
        overflow set and the render composites a stale strip: a shortened
        label keeps scrolling forever and a lengthened one never starts until
        a page reload (when the label is edited through the editor). Cheap: the
        widths/visible flags are recomputed lazily and the strip/bbox/static
        dicts are re-keyed on demand."""
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
        """Kept as the caller-facing name; the widget work is the adapter's.

        Page.set_label_* calls this on every label styling change (8 sites in
        PageManagement/Page.py) and the trailing update_input repaint runs
        after it -- so this must stay a plain forwarder that never raises.
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
        """The merged page+action+defaults labels for all three positions,
        memoized.

        The merge itself is not free: three KeyLabel copies plus
        inject_defaults' nine settings reads measured ~60us per key per media
        tick, paid on every frame of an animated background even though the
        labels only change when something sets one.

        Invalidation: every label mutation goes through set_page_label /
        set_action_label / clear_labels, or -- for the in-place editor path --
        Page.set_label_*, which calls invalidate_scroll_caches(); all of them
        land on _bump_label_epoch(), which retires this memo even against a
        concurrent render already halfway through composing one.

        A change to the app-wide FONT DEFAULTS is not a label mutation and
        needs no separate channel: all four Settings writers that touch
        gl.settings_manager.font_defaults (the font row, the font-color row,
        the outline-color row and the outline-width row -- Settings.py:448,
        478, 502, 527) call page_manager.reload_all_pages(), and reloading a
        page runs create_n_states(), which REPLACES every input state object
        and with it every LabelManager. So a font-defaults change does not
        invalidate this memo, it destroys the object holding it -- a stronger
        guarantee than any clear_labels()-style reasoning, and one that
        covers dials and touchscreens too (verified on main, review round 2).
        get_scroll_label_widths() documents the weaker sibling assumption for
        the rolling-labels toggle.

        The returned KeyLabels are SHARED, so treat them as read-only.
        get_composed_label() still returns a fresh object per call for
        callers that want to mutate one (e.g. the label editor)."""
        memo = self._composed_labels_cache
        if memo is not None and memo[0] == self._label_epoch:
            return memo[1]
        # Stamp with the epoch read BEFORE composing: an invalidation that
        # lands during the compose moves the epoch past this stamp, so the
        # store below publishes something every reader rejects instead of
        # silently reinstating pre-edit labels.
        epoch = self._label_epoch
        labels = {
            position: self.get_composed_label(position)
            for position in ("top", "center", "bottom")
        }
        self._composed_labels_cache = (epoch, labels)
        # Return the dict we just built rather than re-reading the attribute,
        # so a concurrent publish cannot swap the result mid-call.
        return labels

    
    def inject_defaults(self, label: "KeyLabel") -> "ComposedKeyLabel":
        """Fills every unset field from the app-wide font defaults, in place.

        Returns the SAME object, retyped: after this runs there is no field
        left for a reader to None-check (see ComposedKeyLabel)."""
        if label.text is None:
            label.text = ""
        if label.color is None:
            # List, not tuple: the field is declared list[int] and the
            # settings path yields a JSON list, so a tuple fallback made the
            # attribute's runtime type depend on which branch filled it.
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
        # A label is drawn iff its text is non-empty (see add_labels_to_image).
        # Epoch-stamped: a stale False here is not just a missed repaint, it
        # sends ControllerKey._tile_passthrough_ok down the bare-tile fast
        # path, so the labelled key renders as an empty tile until something
        # else invalidates (see _label_epoch).
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
        position. Both scroll detection and the render path measure through
        here, so they can never disagree about whether a label overflows."""
        font = label.get_font()
        key = (label.text, getattr(font, "path", None), getattr(font, "size", None))
        cached = self._bbox_cache.get(position)
        if cached is not None and cached[0] == key:
            return cached[1]
        _, _, w, h = _label_measure_draw.textbbox((0, 0), label.text, font=font)
        # textbbox is typed to return floats (it adds the origin, which may be
        # fractional); this call anchors at integer (0, 0) with an integer
        # glyph bbox, so the values are already ints and int() is identity.
        measured = (int(w), int(h))
        self._bbox_cache[position] = (key, measured)
        return measured

    def get_scroll_label_widths(self) -> dict[str, int]:
        """Text widths of the composed labels that actually scroll: rolling
        labels enabled AND rendered text wider than the input. Measured with
        the same multiline-aware textbbox the render path uses, so detection
        can never flag a label the render would draw statically (that
        mismatch kept the media loop at full FPS re-rendering identical
        frames)."""
        # Cache invalidation: label edits go through invalidate_scroll_caches()
        # (set_page_label/set_action_label and the Page.set_label_* setters); a
        # rolling-labels TOGGLE lands via reload_page(), which rebuilds these
        # managers. A rolling-labels change made OUTSIDE the Settings dialog
        # (a direct settings.json edit, or a plugin writing app settings) does
        # NOT reload_page and so leaves this cache stale until the next label
        # edit or page load -- a pre-existing lifecycle assumption, acceptable
        # because that path isn't a supported runtime toggle.
        #
        # Epoch-stamped like the other latch memos: a store landing after a
        # concurrent edit would otherwise pin the pre-edit overflow set --
        # exactly the "a shortened label keeps scrolling forever" shape.
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

    # Original cadence, expressed in wall time instead of loop iterations
    # (at the nominal 30 FPS the old code advanced 1px per two ticks and
    # burned scroll_wait=25 ticks -- even ticks at the leading edge -- per
    # hold). Wall-clock keeps the speed stable when event wakes push the
    # loop past its nominal rate.
    _NOMINAL_TICK_RATE = 30.0
    SCROLL_STEP_SECONDS = 2.0 / _NOMINAL_TICK_RATE

    def _scroll_hold_start_seconds(self) -> float:
        return self.scroll_wait * 2.0 / self._NOMINAL_TICK_RATE

    def _scroll_hold_end_seconds(self) -> float:
        return self.scroll_wait / self._NOMINAL_TICK_RATE

    def tick_scroll_labels(self) -> bool:
        """Advances the rolling-label animation and reports whether any
        visible scroll offset changed (= a re-render is needed). This is the
        ONLY place scroll state moves -- rendering is pure -- so the hold
        plateaus and the between-step ticks cost integer/time math here
        instead of a full composite that the hash de-dup would throw away
        anyway."""
        changed = False
        now = time.monotonic()
        available_width = self.get_available_width()
        for position, w in self.get_scroll_label_widths().items():
            frame = self.frames[position]
            # The sweep runs from x=start (10px right of centered) down to
            # one pixel past x=stop (10px left of centered), like the
            # original: overshoot = start - stop.
            overshoot = w - available_width + 20
            next_at = frame.get("next_step_at")
            if next_at is None:
                # Fresh label: hold at the start position first.
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
                continue
            if now < next_at:
                continue
            if frame["position"] > overshoot:
                # Trailing hold elapsed: snap back to the start and hold.
                frame["position"] = 0
                frame["next_step_at"] = now + self._scroll_hold_start_seconds()
            else:
                frame["position"] += 1
                if frame["position"] > overshoot:
                    frame["next_step_at"] = now + self._scroll_hold_end_seconds()
                else:
                    frame["next_step_at"] += self.SCROLL_STEP_SECONDS
                    # Re-anchor instead of bursting to catch up if the loop
                    # stalled (page switch, suspend, ...).
                    if frame["next_step_at"] < now - 0.5:
                        frame["next_step_at"] = now
            changed = True
        return changed

    # A precomposed strip is width x keyheight x 4 bytes, retained per label
    # position per state for the whole sweep. Strip width scales with TEXT
    # length, so a pasted 50k-char label would retain ~95 MB and stall the
    # sole-writer media thread for seconds rasterizing it (review round 1).
    # Past this width we fall back to the pre-MR direct per-frame draw: only
    # pathological labels pay the per-frame raster CPU, and nothing is
    # retained. 4096 px is ~290 'm' glyphs at font 15 -- far past any legible
    # key label -- and caps the retained strip near ~1.6 MB even on a
    # 100px-tall SD+ dial image.
    _MAX_STRIP_WIDTH = 4096
    # ...but only on ONE axis. Strip HEIGHT tracks the composed text block,
    # so a multiline label re-opens the same hole vertically: a 2000-line
    # label measures ~42000 px tall, i.e. a ~700 MB strip (review round 2).
    # 4 MiB leaves ~2.5x headroom over the widest legitimate strip the width
    # cap already permits (4096 x 100 x 4 = 1.6 MB on an SD+ dial image) and
    # rejects the pathological-height case outright.
    _MAX_STRIP_BYTES = 4 * 1024 * 1024

    # The same bound for the recorded static-label blits. Retention there is
    # 1 byte/px of glyph mask rather than 4 byte/px of RGBA canvas, but it
    # scales with LINE COUNT the same way -- and unlike the strip, the
    # recording also costs FreeType time on the sole device writer: measured
    # headless, 500 lines = 1000 blits / 430 KB / 0.22 s, 2000 lines = 4000
    # blits / 2.0 MB / 1.06 s, and the reviewer's 20k-line probe = 32 MB and
    # a 5 s media-thread stall.
    #
    # 512 KiB per position is ~6x the largest label that can actually be READ
    # on an input: a 200x200 dial image covered edge to edge in glyphs is
    # ~40k px per pass, ~80 KB for the stroke+fill pair. It also covers a
    # single-line label stretched to the full 4096 px width cap (~240 KB).
    # Three positions -> a 1.5 MiB ceiling per input state, bounded on BOTH
    # axes. 512 ops is 256 lines x (stroke pass + fill pass); a label that
    # tall is already thousands of pixels past any key.
    #
    # Enforced twice: cheaply BEFORE recording from the measurements the
    # render already has (_label_ops_budget_ok), and as a hard abort inside
    # the recorder, which is what actually bounds the wall time.
    _MAX_LABEL_MASK_BYTES = 512 * 1024
    _MAX_LABEL_OPS = 512

    def _label_ops_budget_ok(self, label: "ComposedKeyLabel", w: int, h: int) -> bool:
        """Whether recording this label's blits is worth the retention and
        the media-thread stall, decided from measurements the caller already
        has (no rasterization).

        text() masks each LINE separately and runs the whole thing twice when
        there is an outline, so the op count is 2 per line and the stroke
        padding is paid per line rather than once for the block. That makes
        this an over-estimate of the real mask total (~2x on measured cases,
        because a mask is the glyph run's tight bbox, not the block
        rectangle) -- the safe direction for a pre-check, since the exact
        bound is enforced inside _BitmapRecorder."""
        lines = label.text.count("\n") + 1
        if lines * 2 > self._MAX_LABEL_OPS:
            return False
        stroke = label.outline_width or 0
        estimated_bytes = (2 * (int(w) + 2 * stroke)
                           * (int(h) + lines * 2 * stroke))
        return estimated_bytes <= self._MAX_LABEL_MASK_BYTES

    def _composite_scroll_strip(self, image: Image.Image, position: str, label: "ComposedKeyLabel",
                                w: int, h: int, x_position: float, y_position: float) -> None:
        """Draws a scrolling label by compositing a window of its precomposed
        text strip at this tick's offset. The strip is rasterized once per
        (text, font, colors) and reused for every frame of the sweep; a
        direct draw.text with stroke costs ~2.5ms per frame, the composite
        ~0.014ms, pixel-identical (the target coords' fractional parts are
        constant across the sweep and get baked into the strip, so the paste
        offset is always a whole pixel).

        NOTE: the composite matches a direct draw for opaque ink. Semi-
        transparent fill/outline (alpha < 255, reachable only via the plugin
        set_label API / hand-edited page JSON, not the color picker) blends
        with straight-alpha OVER here vs PIL's coverage blend in draw.text, so
        the scrolling frame differs slightly from the static draw for those.
        The static twin (_draw_static_label) caches one layer lower --
        the glyph masks rather than a composited strip -- which is exact for
        any ink, but needs a fixed paste position, so it does not generalize
        back to the sweep."""
        font = label.get_font()
        outline_width = label.outline_width
        pad = outline_width + 6

        strip_width = int(w) + 2 * pad + 1
        strip_height = int(h) + 2 * pad + 1
        if strip_width > self._MAX_STRIP_WIDTH or \
                strip_width * strip_height * 4 > self._MAX_STRIP_BYTES:
            # Pathological label: skip the strip cache entirely and draw the
            # text directly at the scroll offset (the pre-MR path). Bounded
            # memory (nothing retained), correct pixels; per-frame raster CPU
            # is the trade, acceptable for a label this long. The byte test
            # is what covers a many-LINE label, whose block grows vertically
            # and slips straight past the width cap.
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
            # Antialiased edge pixels blend toward the canvas color; pre-fill
            # with the outermost ink color (at alpha 0) so the strip's edges
            # match a direct draw onto the key image.
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
        # Crop to the visible window: in-place alpha_composite requires a
        # non-negative dest, and it does the correct straight-alpha OVER
        # (paste-with-mask would under-write the alpha channel on the
        # antialiased edges).
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
        """Draws a non-scrolling label by replaying its cached glyph blits.

        A static label's pixels are a pure function of (text, font, colors,
        outline, alignment, image geometry) -- none of which change between
        media ticks -- yet draw.text() re-rasterized the stroked glyph run
        every frame: ~820us per key, ~50% of the whole tick on a populated
        page over an animated background. The rasterization is
        recorded ONCE here (via _BitmapRecorder standing in for the draw
        core) and later frames replay only the mask blits, ~11us.

        Pixel-exact by construction, not by approximation: the replay issues
        the identical C blits, with the identical masks, inks and absolute
        coordinates that draw.text() itself would have issued, in the same
        order. That is why this path -- unlike the scroll strip, whose
        straight-alpha OVER only matches for opaque ink -- needs no
        semi-transparent-ink carve-out. (A precomposed RGBA strip cannot be
        exact here: where a partially covered stroke pixel is overdrawn by a
        partially covered fill pixel, the strip has to collapse two coverage
        blends into one straight-alpha value, which measured up to 19/255 off
        a direct draw.)

        Falls back to the direct draw -- today's behavior, no cache retained
        -- for a label past the width cap or past the retention/op budget,
        and if PIL's text() internals ever stop routing through draw_bitmap.

        The non-RGB(A) guard is not a working fallback, it is behavior
        PARITY. The recorded ink is resolved for the target's mode (a
        palette index for "P", a packed int for "RGB"/"RGBA"), so a
        recording is only valid on the mode it was taken on, and this branch
        keeps such targets on whatever main already did with them -- which
        for "L" is to RAISE: a direct draw.text with an RGBA fill tuple onto
        an "L" image is a TypeError there too. In-app every label target is
        RGBA."""
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
        # The geometry (x/y/anchor) is derived from the image size and the
        # measured text, but keying on it directly means a resized deck image
        # or a re-measured label can never replay blits at stale coordinates.
        key = (label.text, getattr(font, "path", None), label.font_size,
               tuple(label.color), label.outline_width, tuple(label.outline_color),
               label.alignment, anchor, x_position, y_position,
               image.size, image.mode, w, h)
        cached = self._static_ops.get(position)
        if cached is None or cached[0] != key:
            ops = self._record_label_blits(
                image, label, font, (x_position, y_position), anchor)
            # A failed recording is memoized as None under the SAME key: the
            # attempt can cost hundreds of milliseconds, so it must not be
            # retried on every frame of an animated background.
            self._static_ops[position] = (key, ops)
            # Only a recording that actually produced replayable blits is a
            # cache MISS; a failed one is a fallback, counted below. Lumping
            # them together made a permanently-uncacheable label read as a
            # healthy warm cache in the profile.
            if media_prof and ops is not None:
                media_prof.count("label_ops_miss")
        else:
            ops = cached[1]
            if media_prof and ops is not None:
                media_prof.count("label_ops_hit")

        if ops is None:
            # Recording is not available for this label (see above): keep
            # drawing it the old way, without re-attempting the recording
            # every frame.
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
        """Runs draw.text() against a throwaway target whose draw core only
        RECORDS the mask blits, and returns them (None = not recordable, draw
        directly instead).

        The probe target matches the real image's MODE because the ink is
        resolved for the mode. It matches the real SIZE because the probe
        doubles as the interception TRIPWIRE: a recording is only safe to
        replay if the recorder saw the WHOLE draw, and the proof of that is
        that the probe came out blank. Anything text() writes through a
        channel other than draw_bitmap -- PIL's embedded-color route pastes
        onto the target image directly, bypassing the draw core entirely --
        lands on this probe as residue, and a full-size probe is the only one
        that can still show it. (A 1x1 probe would record the identical ops,
        since text() derives the blit coordinates from xy/anchor/mask rather
        than from the canvas, but it would clip every escaped write away and
        blind this check.)

        So the bar is non-empty ops AND a blank probe. `not ops` alone only
        catches TOTAL loss; a stroke pass that records while the fill pass
        escapes would otherwise cache an outline-only label forever.

        Residue detection is one-sided -- black ink escaping onto an "RGB"
        probe is invisible to getbbox() -- so it can never reject a good
        recording, only miss a bad one. Every in-app label target is RGBA,
        where any escaped ink moves the alpha channel off zero."""
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
            # Expected for pathological labels that slipped past the cheap
            # pre-check; the partial recording is dropped here and the caller
            # pins the label to the direct draw.
            log.info(f"Label blit recording exceeded the retention budget "
                     f"({too_large}); falling back to the per-frame draw for "
                     f"this label")
            return None
        except Exception:
            # log.opt(exception=True), not exc_info=: loguru has no exc_info
            # kwarg and would treat it as a format argument, so the traceback
            # this fallback exists to surface was being dropped.
            log.opt(exception=True).warning(
                "Label blit recording failed; falling back to the per-frame "
                "draw for this label")
            return None
        if not ops or residue is not None:
            # Either no blit at all for non-empty text, or pixels on the probe
            # -- both mean PIL took a path this recorder does not model (e.g.
            # embedded-color glyphs). Replaying the recording would erase the
            # label, or keep only the half that was intercepted.
            log.warning(
                f"Label blit recording did not intercept the whole draw "
                f"({len(ops)} ops, probe residue {residue}); falling back to "
                f"the per-frame draw for this label")
            return None
        return ops

    def add_labels_to_image(self, image: Image.Image) -> Image.Image:
        # image = image.rotate(self.deck.get_rotation()*-1)
        if not self.get_has_visible_labels():
            # Nothing to draw: hand the caller back its own image rather than
            # a key-sized RGBA copy per frame. ControllerKey.get_current_image
            # knows the result can BE its input and skips the matching
            # close()es; every other caller returns it straight through.
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

            # Vertical placement is shared by the static and scrolling paths.
            if label == "top":
                y_position = h/2 + 3
            elif label == "bottom":
                y_position = image.height - h/2 - 3
            else:
                y_position = (image.height - 0) / 2

            if label in scroll_widths:
                # Rolling label: composite the precomposed strip at this
                # tick's offset. Scroll state advances in
                # tick_scroll_labels() only -- rendering is pure, so paints
                # from key presses / page loads can't perturb the animation.
                start = image.width / 2 - (image.width - w) / 2 + 10
                x_position = start - self.frames[label]["position"]
                self._composite_scroll_strip(image, label, labels[label], w, h,
                                             x_position, y_position)
                continue

            # Calculate x position based on alignment
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

            # Use appropriate anchor based on alignment (x-anchor + "m" for vertical middle)
            anchor = anchor_x + "m"

            self._draw_static_label(image, draw, label, labels[label], w, h,
                                    x_position, y_position, anchor)

        del draw

        # The copy stays for ControllerKey.get_current_image: this method
        # draws IN PLACE, and that caller closes the buffer it passed in as
        # soon as the labelled result is a different object (its
        # `key_image is not labeled_image` guard). The dial/touchscreen
        # caller (ControllerTouchScreenState.get_rendered_touch_image) just
        # rebinds the name and never closes, so the copy is redundant on that
        # path -- droppable, but only by giving the two callers separate
        # contracts, and this only runs for inputs that carry a label at all.
        return image.copy()
        # return image.copy().rotate(self.deck.get_rotation())


class LayoutManager:
    def __init__(self, controller_input: "ControllerInput"):
        self.controller_input = controller_input

        self.action_layout = ImageLayout()
        self.page_layout = ImageLayout()

        # (token, layout key, resized image): the resized foreground for a
        # static asset, valid while the caller passes the same asset object,
        # the same backing source image (an in-place re-decode swap changes
        # it -- see add_image_to_background's fg_key), and unchanged
        # layout/geometry. Single tuple so concurrent updates swap it
        # atomically.
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
        """Fills every unset field, in place; returns the same object retyped
        (see ComposedImageLayout)."""
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

        # The resized foreground depends only on the source asset and layout,
        # not on the (possibly animated) background. cache_token is the asset
        # object itself (the InputImage/InputVideo), pinned alive by the
        # `cached[0] is cache_token` identity check below -- a held reference
        # can't collide, unlike a freed id().
        #
        # cache_token alone is NOT enough to key the resized foreground:
        # InputImage._ensure_fits_composed() re-decodes and swaps its backing
        # `image` IN PLACE (B-03 -- the asset object stays identical while its
        # pixels change to a higher resolution). fg_key must therefore also
        # track WHICH source image was resized, or a post-swap composite would
        # be served the stale low-res entry cached from before the swap. Today
        # a swap only ever grows the image, so image_size (driven by
        # layout.size) already differs across a swap; but that coupling is
        # implicit -- a future same-size re-decode (e.g. a saturation change)
        # would not change image_size. id(image) closes that gap explicitly:
        # while cache_token is alive it holds a strong ref to `image`, so this
        # id cannot be reused by another object under us.
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

        # Create an image copy for the result
        final_image = background.copy()

        # Paste the resized foreground onto the composite image at the calculated position
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
        # The `is not None` tests are implied by get_color_is_set (which reports
        # False for None) -- spelled out so the returns are provably non-None.
        page_color = self.page_color
        action_color = self.action_color
        if self.get_use_page_background() and page_color is not None and self.get_color_is_set(page_color):
            return page_color
        elif action_color is not None and self.get_color_is_set(action_color):
            return action_color
        else:
            return [0] * 4
