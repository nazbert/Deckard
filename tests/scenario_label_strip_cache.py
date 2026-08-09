"""
Regression test for the static-label blit cache (issue #207, folds in #188).

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
    """The pre-#207 render of one label: a direct draw.text at the geometry
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
        f"per-frame path (#207)")
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


def main() -> None:
    fixtures.start_watchdog(300, label="scenario_label_strip_cache")
    check_pixel_parity()
    check_alpha_ink_is_exact_and_cached()
    check_pathological_label_not_cached()
    check_label_edits_invalidate()
    check_draw_count_contract()
    check_labelless_key_identity()
    print("scenario_label_strip_cache: OK")


if __name__ == "__main__":
    main()
