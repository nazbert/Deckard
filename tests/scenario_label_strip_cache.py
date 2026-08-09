"""
Regression test for the static-label blit cache.

Root cause: `LabelManager.add_labels_to_image` re-ran `ImageDraw.text` --
FreeType rasterization of the stroked glyph run -- for every STATIC label on
every media tick, even though a static label's pixels depend on nothing that
changes between frames. Measured headless on main @ 90ea72bc (8 keys with
icon + label over a per-frame-noise background video, 30 fps): 820us per key
per tick, 88% of the label stage, `c_labels` p50 0.90ms of a 13.9ms tick.
Scrolling labels already had a precomposed-strip fast path; static ones did
not.

The fix records the mask blits that `draw.text()` issues ONCE per composed
label and replays them per frame (~11us), so the rasterization happens on a
label change instead of on a frame.

Asserted here:
  (a) the cached path is pixel-EXACT against a direct `draw.text` -- every
      position, alignment and outline width, byte for byte (not a
      tolerance). Exactness is the whole point of replaying the blits rather
      than compositing a precomposed RGBA strip: a strip has to collapse a
      partially-covered stroke pixel overdrawn by a partially-covered fill
      pixel into ONE straight-alpha value, which measured up to 19/255 off a
      direct draw. That is also why -- unlike the scroll strip -- this path
      needs no semi-transparent-ink carve-out, which (b) pins.
  (b) semi-transparent fill/outline ink is exact too, and still cached.
  (c) a pathological label past the strip-width cap keeps the direct draw and
      retains nothing (the memory bound the scroll strip established).
  (d) label edits -- through the setters AND through Page.set_label_* in-place
      mutation -- rebuild the cache; a stale strip can never be served.
  (e) the draw-count contract: over dozens of media ticks of a static-labeled
      key on an animated background, the text is rasterized ZERO further
      times. Revert the cache and this fails by ~one raster per key per tick.
  (f) a label-less key draws nothing and gets its own image back (identity,
      no per-frame copy) -- and the composite that consumes it is still
      readable, i.e. get_current_image() does not close the buffer it
      returns.

Added in the review fix round:
  (g) the latch memos are epoch-stamped, so a lazily-built value that is
      stored AFTER a concurrent edit cannot be served: the reviewer's
      interleaving is injected deterministically for _composed_labels_cache
      (permanently stale text) and _has_visible_labels_cache (a stale False
      sends _tile_passthrough_ok down the bare-tile path, so a labelled key
      renders empty forever).
  (h) the retention/record-time bound holds on the HEIGHT axis: a many-line
      label that fits the width cap is still refused, renders as a direct
      draw, retains nothing, and never re-attempts the (multi-second)
      recording. The recorder's own hard abort is exercised directly.
  (i) the interception tripwire refuses a PARTIAL recording, not just a
      total one -- a swallowed-then-escaping pass is the shape PIL's
      embedded-color route produces -- and the refusal is sticky.
  (j) mutation-round gaps: both label setters drop the recorded blits,
      multiline labels are exact, the dial's own label stage survives the
      identity early-out, and neither the pressed-key shrink nor the
      warning-point marker writes through the identity return onto the
      shared background tile.
"""
import time

import fixtures
import globals as gl

from PIL import Image, ImageDraw

TEXT = "Vol"


def _make_controller(serial: str, rolling: bool = False, page_name: str = "Main"):
    """Rolling labels OFF by default: every label then takes the static path
    regardless of width, which is exactly the path under test."""
    fixtures._install_integration_globals()
    settings = gl.settings_manager.get_app_settings()
    settings.setdefault("general", {})["rolling-labels"] = rolling
    gl.settings_manager.save_app_settings(settings)

    controller = fixtures.make_headless_controller(serial=serial, page_name=page_name)
    time.sleep(1.0)
    fixtures.wait_until(lambda: not controller.media_player.image_tasks, timeout=5.0)
    return controller


def _set_label(key, position: str, **kwargs):
    from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
    kwargs.setdefault("font_size", 15)
    key.get_active_state().label_manager.set_page_label(
        position, KeyLabel(controller_input=key, **kwargs), update=False)


def _reference(lm, position: str, size: tuple, bg: tuple) -> Image.Image:
    """The pre-cache render of one label: a direct draw.text at the geometry
    add_labels_to_image computes. Deliberately re-derived here rather than
    called through the code under test."""
    label = lm.get_composed_label(position)
    w, h = lm._measure_text(position, label)
    if position == "top":
        y = h / 2 + 3
    elif position == "bottom":
        y = size[1] - h / 2 - 3
    else:
        y = size[1] / 2
    if label.alignment == "left":
        x, anchor = 3, "lm"
    elif label.alignment == "right":
        x, anchor = size[0] - 3, "rm"
    else:
        x, anchor = size[0] / 2, "mm"

    image = Image.new("RGBA", size, bg)
    ImageDraw.Draw(image).text((x, y), text=label.text, font=label.get_font(),
                               anchor=anchor, align=label.alignment,
                               fill=tuple(label.color),
                               stroke_width=label.outline_width,
                               stroke_fill=tuple(label.outline_color))
    return image


def _render(lm, size: tuple, bg: tuple) -> Image.Image:
    return lm.add_labels_to_image(Image.new("RGBA", size, bg))


def check_pixel_parity() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-a")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        bg = (30, 60, 90, 255)

        checked = 0
        for position in ("top", "center", "bottom"):
            for alignment in ("left", "center", "right"):
                for outline_width in (0, 2, 4):
                    lm.clear_labels()
                    _set_label(key, position, text=TEXT, color=[255, 255, 255, 255],
                               outline_width=outline_width,
                               outline_color=[0, 0, 0, 255], alignment=alignment)
                    assert not lm.get_has_scroll_labels(), "premise: label must be static"

                    reference = _reference(lm, position, size, bg)
                    # First render populates the cache, the rest replay it;
                    # every one of them must be byte-identical.
                    for frame in range(3):
                        rendered = _render(lm, size, bg)
                        assert rendered.tobytes() == reference.tobytes(), (
                            f"{position}/{alignment}/outline={outline_width} frame "
                            f"{frame}: cached label render differs from a direct "
                            f"draw.text -- the blit replay is not pixel-exact")
                    assert lm._static_ops.get(position) is not None, (
                        f"{position}/{alignment}/outline={outline_width}: nothing "
                        f"cached, the label is still rasterized every frame")
                    checked += 1
        print(f"PASS: {checked} position/alignment/outline combinations render "
              f"byte-identical to a direct draw.text across 3 frames each")
    finally:
        fixtures.teardown(controller)


def check_alpha_ink_is_exact_and_cached() -> None:
    """The scroll strip documents a caveat: semi-transparent ink composites
    with straight-alpha OVER instead of PIL's coverage blend, so scrolling
    frames differ slightly from a static draw. Replaying the blits has no
    such gap -- it IS the coverage blend -- so alpha ink is cached like any
    other label and stays exact. This pins that the caveat was not widened
    to the static path along with the cache."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-b")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()

        cases = [
            ("translucent fill", [255, 255, 255, 96], [0, 0, 0, 255]),
            ("translucent outline", [255, 255, 255, 255], [0, 0, 0, 64]),
            ("both translucent", [200, 40, 40, 128], [10, 200, 10, 90]),
        ]
        for name, color, outline_color in cases:
            for bg in ((30, 60, 90, 255), (0, 0, 0, 0)):
                lm.clear_labels()
                _set_label(key, "center", text=TEXT, color=color, outline_width=2,
                           outline_color=outline_color, alignment="center")
                reference = _reference(lm, "center", size, bg)
                for frame in range(3):
                    rendered = _render(lm, size, bg)
                    assert rendered.tobytes() == reference.tobytes(), (
                        f"{name} on bg {bg}, frame {frame}: cached render differs "
                        f"from the direct draw -- semi-transparent ink is not exact")
                assert lm._static_ops.get("center") is not None, (
                    f"{name}: fell back to the per-frame raster")
        print("PASS: semi-transparent fill/outline ink renders byte-identical and "
              "is still cached (no alpha carve-out needed)")
    finally:
        fixtures.teardown(controller)


def check_pathological_label_not_cached() -> None:
    from src.backend.DeckManagement.DeckController import LabelManager
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-c")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        bg = (0, 0, 0, 255)
        cap = LabelManager._MAX_STRIP_WIDTH

        _set_label(key, "center", text=TEXT, color=[255, 255, 255, 255],
                   outline_width=2, outline_color=[0, 0, 0, 255], alignment="center")
        _render(lm, size, bg)
        assert lm._static_ops.get("center") is not None, \
            "premise: a normal label must be cached"

        pathological = "m" * 20000
        _set_label(key, "center", text=pathological, color=[255, 255, 255, 255],
                   outline_width=2, outline_color=[0, 0, 0, 255], alignment="center")
        composed = lm.get_composed_label("center")
        w, _ = lm._measure_text("center", composed)
        assert w > cap, f"probe premise: {w}px text must exceed the {cap}px cap"

        reference = _reference(lm, "center", size, bg)
        rendered = _render(lm, size, bg)
        assert rendered.tobytes() == reference.tobytes(), (
            "the capped direct-draw fallback does not match a direct draw")
        assert lm._static_ops.get("center") is None, (
            f"a {w}px-wide label retained cached blits past the {cap}px cap -- "
            f"that is the unbounded per-label pixel buffer the memory war closed")
        print(f"PASS: {w}px label falls back to the direct draw, nothing retained "
              f"(cap {cap}px)")
    finally:
        fixtures.teardown(controller)


def check_label_edits_invalidate() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-d")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        bg = (12, 34, 56, 255)

        # Every attribute that changes the pixels must re-key the cache.
        variants = [
            dict(text="one", color=[255, 255, 255, 255], outline_width=2,
                 outline_color=[0, 0, 0, 255], alignment="center"),
            dict(text="two", color=[255, 255, 255, 255], outline_width=2,
                 outline_color=[0, 0, 0, 255], alignment="center"),
            dict(text="two", color=[255, 0, 0, 255], outline_width=2,
                 outline_color=[0, 0, 0, 255], alignment="center"),
            dict(text="two", color=[255, 0, 0, 255], outline_width=5,
                 outline_color=[0, 0, 0, 255], alignment="center"),
            dict(text="two", color=[255, 0, 0, 255], outline_width=5,
                 outline_color=[0, 0, 255, 255], alignment="center"),
            dict(text="two", color=[255, 0, 0, 255], outline_width=5,
                 outline_color=[0, 0, 255, 255], alignment="left"),
            dict(text="two", color=[255, 0, 0, 255], outline_width=5,
                 outline_color=[0, 0, 255, 255], alignment="left", font_size=22),
        ]
        previous = None
        for i, variant in enumerate(variants):
            _set_label(key, "center", **variant)
            rendered = _render(lm, size, bg)
            assert rendered.tobytes() == _reference(lm, "center", size, bg).tobytes(), (
                f"variant {i} ({variant}) rendered stale pixels after the edit")
            if previous is not None:
                assert rendered.tobytes() != previous, (
                    f"variant {i} ({variant}) rendered identically to the previous "
                    f"label -- the cache key ignores that attribute")
            previous = rendered.tobytes()

        # The in-place editor path (Page.set_label_*) bypasses the setters and
        # relies on invalidate_scroll_caches().
        controller.active_page.set_label_text(key.identifier, 0, "center",
                                              "edited", update=False)
        rendered = _render(lm, size, bg)
        assert lm.get_composed_label("center").text == "edited", \
            "premise: the in-place edit did not land"
        assert rendered.tobytes() == _reference(lm, "center", size, bg).tobytes(), (
            "Page.set_label_text left a stale cached raster on screen -- the "
            "in-place mutation path is not invalidating")

        # And the explicit reset sites drop both caches.
        _render(lm, size, bg)
        assert lm._static_ops and lm._composed_labels_cache is not None
        lm.invalidate_scroll_caches()
        assert not lm._static_ops, "invalidate_scroll_caches() left cached blits"
        assert lm._composed_labels_cache is None, \
            "invalidate_scroll_caches() left the composed-labels memo"
        _render(lm, size, bg)
        lm.clear_labels()
        assert not lm._static_ops, "clear_labels() left cached blits"
        assert lm._composed_labels_cache is None, \
            "clear_labels() left the composed-labels memo"
        print("PASS: text/color/size/outline/alignment edits, the in-place editor "
              "path and the reset sites all invalidate the cache")
    finally:
        fixtures.teardown(controller)


def check_draw_count_contract() -> None:
    """The mutation-proof: on an animated background the keys recomposite
    every tick, and the label must NOT be rasterized again."""
    import json
    import os

    from src.backend.DeckManagement.DeckController import LabelManager

    fixtures._install_integration_globals()
    settings = gl.settings_manager.get_app_settings()
    settings.setdefault("general", {})["rolling-labels"] = False
    gl.settings_manager.save_app_settings(settings)

    data_dir = gl.DATA_PATH
    video = fixtures.make_test_mp4(os.path.join(data_dir, "bg.mp4"),
                                   n_frames=30, fps=30)
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    labelled = {f"{x}x0": {"states": {"0": {
        "labels": {"center": {"text": f"K{x}", "font-size": 12}}}}}
        for x in range(3)}
    with open(os.path.join(pages_dir, "Anim.json"), "w") as f:
        json.dump({"keys": labelled, "dials": {}, "touchscreens": {},
                   "settings": {"background": {
                       "overwrite": True, "show": True, "media-path": video,
                       "loop": True, "fps": 30}}}, f)

    text_calls = []
    label_calls = []
    orig_text = ImageDraw.ImageDraw.text
    orig_add = LabelManager.add_labels_to_image

    def counting_text(self, *a, **k):
        text_calls.append(1)
        return orig_text(self, *a, **k)

    def counting_add(self, image):
        label_calls.append(1)
        return orig_add(self, image)

    ImageDraw.ImageDraw.text = counting_text
    LabelManager.add_labels_to_image = counting_add
    controller = None
    try:
        controller = fixtures.make_headless_controller(serial="labelcache-e",
                                                       page_name="Anim")
        time.sleep(2.0)  # warm-up: the background video and the first rasters
        warm_rasters = len(text_calls)
        assert warm_rasters > 0, (
            "no label was ever rasterized -- the page did not render labels, so "
            "this scenario would pass vacuously")

        text_calls.clear()
        label_calls.clear()
        time.sleep(3.0)
        steady_rasters = len(text_calls)
        steady_labelings = len(label_calls)
    finally:
        ImageDraw.ImageDraw.text = orig_text
        LabelManager.add_labels_to_image = orig_add
        if controller is not None:
            fixtures.teardown(controller)

    assert steady_labelings >= 30, (
        f"only {steady_labelings} label composites in 3s of animated background "
        f"-- the keys are not re-rendering, so a zero raster count proves nothing")
    assert steady_rasters == 0, (
        f"{steady_rasters} label rasterizations across {steady_labelings} "
        f"composites of unchanged static labels -- draw.text is back in the "
        f"per-frame path")
    print(f"PASS: {steady_labelings} steady-state label composites in 3s cost "
          f"{steady_rasters} rasterizations ({warm_rasters} during warm-up)")


def check_labelless_key_identity() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-f")
    try:
        key = controller.inputs[Input.Key][0]
        state = key.get_active_state()
        lm = state.label_manager
        size = key.get_image_size()
        # Whichever page load_default_page() picked in this shared data dir,
        # this key must genuinely carry no label for the identity contract.
        lm.clear_labels()
        assert not lm.get_has_visible_labels()

        text_calls = []
        orig_text = ImageDraw.ImageDraw.text

        def counting_text(self, *a, **k):
            text_calls.append(1)
            return orig_text(self, *a, **k)

        source = Image.new("RGBA", size, (7, 8, 9, 255))
        ImageDraw.ImageDraw.text = counting_text
        try:
            result = lm.add_labels_to_image(source)
        finally:
            ImageDraw.ImageDraw.text = orig_text

        assert result is source, (
            "a label-less key still pays a full key-sized RGBA copy per frame")
        assert not text_calls, f"{len(text_calls)} draws on a label-less key"

        # The identity return means get_current_image()'s own buffers can BE
        # the image it returns; closing them anyway would hand the media
        # thread a released buffer ("Operation on closed image" on the next
        # read). A background COLOR keeps the key off the bare-tile fast path
        # so the label stage actually runs.
        state.background_manager.set_page_color([10, 20, 30, 255], update=False)
        assert not key._tile_passthrough_ok(state), \
            "premise: the key must not take the bare-tile fast path"
        composed = key.get_current_image()
        assert composed.tobytes(), (
            "get_current_image() returned an image whose buffer was already "
            "closed -- the label stage handed back one of the buffers the "
            "composite closes")
        assert composed.getpixel((0, 0))[:3] == (10, 20, 30)
        print("PASS: label-less key returns its own image (no copy, no draws) and "
              "get_current_image() keeps the returned buffer alive")
    finally:
        fixtures.teardown(controller)


STYLE = dict(color=[255, 255, 255, 255], outline_width=2,
             outline_color=[0, 0, 0, 255], alignment="center")


def check_composed_memo_race() -> None:
    """The memo is filled on the render path and dropped on the edit path,
    unlocked. Injects the exact interleaving the reviewer reproduced under
    thread pressure: the builder composes the pre-edit labels, the edit lands
    (and drops the memo), and only THEN does the builder store. Without an
    epoch stamp that store is indistinguishable from fresh data and the old
    text stays on the key forever -- silently, and with no path back short of
    another label edit."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-g")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        bg = (5, 5, 5, 255)

        _set_label(key, "center", text="OLD", **STYLE)
        _render(lm, size, bg)
        lm.invalidate_scroll_caches()

        fired = []
        real_get_composed_label = lm.get_composed_label

        def racing_get_composed_label(position: str):
            label = real_get_composed_label(position)
            # "bottom" is the LAST position the builder composes, so the edit
            # lands after every value in the dict is already pre-edit.
            if position == "bottom" and not fired:
                fired.append(True)
                _set_label(key, "center", text="NEW", **STYLE)
            return label

        lm.get_composed_label = racing_get_composed_label
        try:
            raced = lm.get_composed_labels()
        finally:
            del lm.get_composed_label

        assert fired, "premise: the injected edit never ran"
        assert raced["center"].text == "OLD", (
            "premise: the racing builder must have composed the PRE-edit "
            "label, otherwise there is no stale value to publish")
        memo = lm._composed_labels_cache
        assert memo is not None, "premise: the racing builder must have stored"
        assert memo[0] != lm._label_epoch, (
            "the memo carries the CURRENT epoch even though it was built "
            "before the edit -- readers cannot tell it from fresh data")

        assert lm.get_composed_labels()["center"].text == "NEW", (
            "the store that raced the edit is still being served: the label "
            "text is stale FOREVER, for every later render")
        rendered = _render(lm, size, bg)
        assert rendered.tobytes() == _reference(lm, "center", size, bg).tobytes(), (
            "the post-edit render does not match a direct draw of the new "
            "label -- a stale memo reached the pixels")
        print("PASS: a composed-labels store that lands after a concurrent "
              "edit is rejected by its epoch stamp, not served forever")
    finally:
        fixtures.teardown(controller)


def check_visible_labels_memo_race() -> None:
    """Same race, worse blast radius, and it pre-exists on main: a stale
    False in _has_visible_labels_cache does not merely skip a repaint, it
    makes ControllerKey._tile_passthrough_ok classify the key as bare, so the
    composite short-circuits to the shared background tile and the label
    never appears again."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-h")
    try:
        key = controller.inputs[Input.Key][0]
        state = key.get_active_state()
        lm = state.label_manager
        lm.clear_labels()
        assert not lm.get_has_visible_labels()
        assert key._tile_passthrough_ok(state), (
            "premise: the un-labelled key must start on the bare-tile path")

        lm.invalidate_scroll_caches()
        fired = []
        real_get_composed_labels = lm.get_composed_labels

        def racing_get_composed_labels():
            labels = real_get_composed_labels()
            if not fired:
                fired.append(True)
                _set_label(key, "center", text="LATE", **STYLE)
            return labels

        lm.get_composed_labels = racing_get_composed_labels
        try:
            raced = lm.get_has_visible_labels()
        finally:
            del lm.get_composed_labels

        assert fired, "premise: the injected edit never ran"
        assert raced is False, (
            "premise: the racing read must have seen the empty label set")
        assert lm.get_has_visible_labels() is True, (
            "a False computed before the edit is still being served -- the "
            "label stage is skipped for the life of this LabelManager")
        assert not key._tile_passthrough_ok(state), (
            "_tile_passthrough_ok still calls the labelled key bare: it "
            "renders as an empty background tile, permanently")
        print("PASS: a has-visible-labels store that lands after a concurrent "
              "edit cannot pin the key on the bare-tile fast path")
    finally:
        fixtures.teardown(controller)


def check_many_line_label_not_cached() -> None:
    """The width cap is one axis. A label's mask total and its RECORD TIME
    also scale with line count, and the recording runs on the sole device
    writer -- measured headless, 500 lines is 1000 blits / 430 KB / 0.22s and
    2000 lines is 2.0 MB / 1.06s. This pins the height-axis bound, including
    that the refusal costs nothing per frame."""
    from src.backend.DeckManagement.DeckController import LabelManager
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-i")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        bg = (0, 0, 0, 255)

        many_lines = "\n".join(f"L{i}" for i in range(500))
        _set_label(key, "center", text=many_lines, **STYLE)
        composed = lm.get_composed_label("center")
        w, h = lm._measure_text("center", composed)
        assert w + 2 * (composed.outline_width + 6) + 1 <= LabelManager._MAX_STRIP_WIDTH, (
            f"probe premise: {w}px must stay UNDER the {LabelManager._MAX_STRIP_WIDTH}px "
            f"width cap, so only the byte/op bound can reject this label")
        assert not lm._label_ops_budget_ok(composed, w, h), (
            f"a {h}px-tall / 500-line label passed the retention budget")

        records = []
        real_record = lm._record_label_blits

        def counting_record(*a, **k):
            records.append(1)
            return real_record(*a, **k)

        lm._record_label_blits = counting_record
        try:
            reference = _reference(lm, "center", size, bg)
            started = time.perf_counter()
            for frame in range(3):
                rendered = _render(lm, size, bg)
                assert rendered.tobytes() == reference.tobytes(), (
                    f"frame {frame}: the refused label does not render as a "
                    f"plain direct draw")
            per_frame = (time.perf_counter() - started) / 3
        finally:
            del lm._record_label_blits

        assert not records, (
            f"{len(records)} blit recordings for a 500-line label -- the "
            f"multi-second record is back on the media thread, per frame")
        assert lm._static_ops.get("center") is None, (
            f"a {h}px-tall label retained recorded blits; the width cap alone "
            f"does not bound retention on this axis")

        # The pre-check deliberately OVER-estimates, so it always fires first
        # for a label this size. The recorder's own hard abort is the backstop
        # for an estimate that under-shoots; exercise it directly.
        lm.clear_labels()
        _set_label(key, "center", text=TEXT, **STYLE)
        normal = lm.get_composed_label("center")
        nw, nh = lm._measure_text("center", normal)
        assert lm._label_ops_budget_ok(normal, nw, nh), (
            "premise: a normal label must pass the pre-check")
        probe = Image.new("RGBA", size, bg)
        args = (probe, normal, normal.get_font(), (size[0] / 2, size[1] / 2), "mm")
        assert lm._record_label_blits(*args) is not None, (
            "premise: a normal label must record")
        for attr, value in (("_MAX_LABEL_MASK_BYTES", 1), ("_MAX_LABEL_OPS", 1)):
            setattr(lm, attr, value)
            try:
                assert lm._record_label_blits(*args) is None, (
                    f"the recorder ran to completion past its {attr} budget "
                    f"instead of aborting -- the bound is advisory only")
            finally:
                delattr(lm, attr)
        print(f"PASS: a 500-line / {h}px-tall label is refused by the retention "
              f"bound, renders as a direct draw at {per_frame * 1000:.0f}ms/frame "
              f"with zero recording attempts, and the recorder aborts on its own "
              f"budget")
    finally:
        fixtures.teardown(controller)


def check_many_line_scroll_strip_not_cached() -> None:
    """The scroll strip has the same height-axis hole, and it is 4 bytes per
    pixel rather than 1: a many-line label that overflows the key would ask
    for a strip hundreds of MB wide-by-tall while sailing under the width
    cap. Rolling labels ON here, unlike every other leg, because that is the
    only way into _composite_scroll_strip."""
    from src.backend.DeckManagement.DeckController import LabelManager
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-l", rolling=True, page_name="Roll")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        bg = (0, 0, 0, 255)

        # Control: a single-line label that overflows the key still gets a
        # strip, so the leg below is testing the byte cap and not "scrolling
        # is broken".
        _set_label(key, "center", text="m" * 60, **STYLE)
        assert lm.get_has_scroll_labels(), "premise: this label must scroll"
        _render(lm, size, bg)
        assert lm._scroll_strips.get("center") is not None, (
            "premise: an ordinary overflowing label must be strip-cached")

        many_lines = "\n".join("m" * 60 for _ in range(500))
        _set_label(key, "center", text=many_lines, **STYLE)
        assert lm.get_has_scroll_labels(), "premise: this label must scroll too"
        composed = lm.get_composed_label("center")
        w, h = lm._measure_text("center", composed)
        pad = composed.outline_width + 6
        strip_bytes = (int(w) + 2 * pad + 1) * (int(h) + 2 * pad + 1) * 4
        assert int(w) + 2 * pad + 1 <= LabelManager._MAX_STRIP_WIDTH, (
            f"probe premise: {w}px must stay under the width cap so only the "
            f"byte bound can reject this label")
        assert strip_bytes > LabelManager._MAX_STRIP_BYTES, (
            f"probe premise: {strip_bytes} bytes must exceed the "
            f"{LabelManager._MAX_STRIP_BYTES}-byte cap")

        _render(lm, size, bg)
        assert lm._scroll_strips.get("center") is None, (
            f"a {strip_bytes / 1024 / 1024:.0f} MB strip was retained -- the "
            f"width cap alone does not bound the scroll cache vertically")
        print(f"PASS: a 500-line scrolling label ({strip_bytes / 1024 / 1024:.0f} MB "
              f"of strip) falls back to the direct draw while an ordinary "
              f"overflowing label is still cached")
    finally:
        fixtures.teardown(controller)


def check_partial_interception_refused() -> None:
    """`if not ops` only catches TOTAL recording loss. PIL's embedded-color
    route pastes onto the target image directly instead of going through the
    draw core, so with an outline the stroke pass records and the fill pass
    escapes: a non-empty op list that would replay an outline-only label
    forever. The probe is the tripwire -- a recording is safe to replay only
    if the probe came out blank."""
    from src.backend.DeckManagement import DeckController as dc
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("labelcache-j")
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        bg = (20, 40, 60, 255)
        real_recorder = dc._BitmapRecorder
        captured = []

        class SwallowAll(real_recorder):
            """Total interception loss: nothing recorded, nothing drawn."""

            def draw_bitmap(self, coord, mask, ink):
                return 0

        class SwallowOne(real_recorder):
            """Partial loss: the first pass records, the rest reach the
            canvas -- the shape the embedded-color route produces."""

            def draw_bitmap(self, coord, mask, ink):
                if not self.ops:
                    return real_recorder.draw_bitmap(self, coord, mask, ink)
                captured.append(len(self.ops))
                return self._core.draw_bitmap(coord, mask, ink)

        for name, recorder_cls in (("total loss", SwallowAll),
                                   ("partial loss", SwallowOne)):
            lm.clear_labels()
            _set_label(key, "center", text=TEXT, **STYLE)
            reference = _reference(lm, "center", size, bg)

            records = []
            real_record = lm._record_label_blits

            def counting_record(*a, **k):
                records.append(1)
                return real_record(*a, **k)

            lm._record_label_blits = counting_record
            dc._BitmapRecorder = recorder_cls
            try:
                for frame in range(3):
                    rendered = _render(lm, size, bg)
                    assert rendered.tobytes() == reference.tobytes(), (
                        f"{name}, frame {frame}: the fallback render is not a "
                        f"plain direct draw -- a lossy recording reached the "
                        f"pixels")
            finally:
                dc._BitmapRecorder = real_recorder
                del lm._record_label_blits

            cached = lm._static_ops.get("center")
            assert cached is not None, (
                f"{name}: the failed recording was not memoized under its "
                f"cache key, so every frame re-attempts it")
            assert cached[1] is None, (
                f"{name}: the recording was accepted and will be replayed for "
                f"the life of this label")
            assert len(records) == 1, (
                f"{name}: {len(records)} recording attempts across 3 frames -- "
                f"the refusal is not sticky, so every frame pays the record")

        assert captured, (
            "premise: the partial-loss recorder never let a pass escape, so "
            "the residue check was not the thing under test")
        print("PASS: both a fully and a partially swallowed recording are "
              "refused (the partial one had a non-empty op list, so only the "
              "probe-residue check can catch it) and the refusal is sticky")
    finally:
        fixtures.teardown(controller)


def check_mutation_round_gaps() -> None:
    """Gaps the review's mutation round found uncovered: the setters' own
    reset of the recorded blits, multiline parity, the dial's label stage,
    and the two post-label composite steps that operate on whatever
    add_labels_to_image handed back -- which, since the identity early-out,
    can BE the caller's own buffer."""
    from src.backend.DeckManagement.InputIdentifier import Input
    from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel

    controller = _make_controller("labelcache-k")
    try:
        key = controller.inputs[Input.Key][0]
        state = key.get_active_state()
        lm = state.label_manager
        size = key.get_image_size()
        bg = (9, 9, 9, 255)

        # (1) Both setters must drop the recorded blits, not just
        # invalidate_scroll_caches()/clear_labels().
        for setter in ("set_page_label", "set_action_label"):
            lm.clear_labels()
            _set_label(key, "center", text="AAA", **STYLE)
            _render(lm, size, bg)
            assert lm._static_ops.get("center") is not None, (
                f"{setter} premise: the label must be cached first")
            getattr(lm, setter)("center", KeyLabel(controller_input=key,
                                                   text="BBB", font_size=15),
                                update=False)
            assert not lm._static_ops, (
                f"{setter}() left recorded blits behind -- they outlive the "
                f"label they were recorded for")
            rendered = _render(lm, size, bg)
            assert rendered.tobytes() == _reference(lm, "center", size, bg).tobytes(), (
                f"{setter}() rendered stale pixels after the edit")

        # (2) Multiline labels: text() masks each line separately, so the
        # replay has to reproduce a whole SEQUENCE of blits in order.
        lm.clear_labels()
        _set_label(key, "center", text="Vol\nUp", **STYLE)
        reference = _reference(lm, "center", size, bg)
        for frame in range(3):
            assert _render(lm, size, bg).tobytes() == reference.tobytes(), (
                f"multiline frame {frame}: the replayed blit sequence differs "
                f"from a direct draw")
        assert lm._static_ops.get("center") is not None, (
            "a two-line label was not cached")

        # (3) The dial's touch image runs the same label stage, including the
        # identity early-out, through a caller that never copies.
        dial = controller.inputs[Input.Dial][0]
        dial_state = dial.get_active_state()
        dial_lm = dial_state.label_manager
        dial_lm.clear_labels()
        assert not dial_lm.get_has_visible_labels()
        bare = dial_state.get_rendered_touch_image()
        bare_bytes = bare.tobytes()
        assert bare_bytes, (
            "get_rendered_touch_image() returned a released buffer -- the "
            "label stage's identity return reached a caller that does not "
            "own a copy of it")
        dial_lm.set_page_label("center", KeyLabel(controller_input=dial,
                                                  text="D", font_size=15),
                               update=False)
        assert dial_lm.get_has_visible_labels()
        painted = dial_state.get_rendered_touch_image()
        assert painted.tobytes() != bare_bytes, (
            "the dial's touch image is identical with and without a label -- "
            "the early-out is swallowing the label stage on this path")

        # (4) The pressed shrink and the warning-point marker both run AFTER
        # the label stage, on whatever it returned. With no label that is the
        # composite's own buffer, so neither may write through to the shared
        # background tile.
        state.background_manager.set_page_color([10, 20, 30, 255], update=False)
        lm.clear_labels()
        assert not lm.get_has_visible_labels()
        assert not key._tile_passthrough_ok(state), (
            "premise: the key must not take the bare-tile fast path")
        tile = controller.background.tiles[key.index]
        tile_before = tile.tobytes() if tile is not None else None

        key.press_state = True
        try:
            pressed = key.get_current_image()
            assert pressed.tobytes(), (
                "the pressed composite returned a released buffer")
        finally:
            key.press_state = False

        key.has_unavailable_action = lambda: True
        try:
            warned = key.get_current_image()
            assert warned.tobytes(), (
                "the warning-point composite returned a released buffer")
        finally:
            del key.has_unavailable_action

        tile_after = tile.tobytes() if tile is not None else None
        assert tile_before == tile_after, (
            "a post-label composite step drew IN PLACE onto the shared "
            "background tile -- every other key on the page inherits it")
        print("PASS: both setters clear the recorded blits, multiline labels "
              "replay exactly, the dial's label stage survives the identity "
              "early-out, and neither the pressed shrink nor the warning "
              "point writes through to the shared tile")
    finally:
        fixtures.teardown(controller)


def main() -> None:
    fixtures.start_watchdog(420, label="scenario_label_strip_cache")
    check_pixel_parity()
    check_alpha_ink_is_exact_and_cached()
    check_pathological_label_not_cached()
    check_label_edits_invalidate()
    check_draw_count_contract()
    check_labelless_key_identity()
    check_composed_memo_race()
    check_visible_labels_memo_race()
    check_many_line_label_not_cached()
    check_many_line_scroll_strip_not_cached()
    check_partial_interception_refused()
    check_mutation_round_gaps()
    print("scenario_label_strip_cache: OK")


if __name__ == "__main__":
    main()
