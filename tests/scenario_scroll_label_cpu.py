"""
Regression test for the scroll-label CPU cost.

get_has_scroll_labels honors the rolling-labels setting and measures with the
multiline-aware textbbox the render uses. A key re-renders only when its own
offset moved, and the scrolling text rasterizes once into a cached strip,
which is capped by width.
"""
import time

import fixtures
import globals as gl

from PIL import Image, ImageDraw, ImageFont

WIDE_TEXT = "m" * 24


def _make_controller(serial: str, rolling: bool):
    """App settings must be in place before labels are composed, because the
    scroll caches read them lazily. The page load must settle before the
    scenario sets labels, or load_all_inputs wipes them."""
    fixtures._install_integration_globals()
    settings = gl.settings_manager.get_app_settings()
    settings.setdefault("general", {})["rolling-labels"] = rolling
    gl.settings_manager.save_app_settings(settings)

    controller = fixtures.make_headless_controller(serial=serial)
    time.sleep(1.5)
    fixtures.wait_until(lambda: not controller.media_player.image_tasks, timeout=5.0)
    return controller


def _set_center_label(key, text: str):
    from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
    key.get_active_state().label_manager.set_page_label(
        "center", KeyLabel(controller_input=key, text=text, font_size=15), update=True)


def _tick_rate(controller, window: float) -> float:
    t0 = controller.media_player.media_ticks
    time.sleep(window)
    return (controller.media_player.media_ticks - t0) / window


def check_rolling_disabled_idles() -> None:
    from src.backend.DeckManagement.DeckController import ControllerKey
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("scrolllbl-a", rolling=False)
    try:
        keys = controller.inputs[Input.Key]
        for key in keys[:3]:
            _set_center_label(key, WIDE_TEXT)
        time.sleep(1.0)  # let initial paints drain and the loop re-throttle

        for key in keys[:3]:
            lm = key.get_active_state().label_manager
            assert lm.page_labels["center"].text == WIDE_TEXT, "label was wiped before the assert window"
            assert not lm.get_has_scroll_labels(), (
                "rolling labels are disabled, but get_has_scroll_labels() still "
                "flags the over-wide label -- this is what held the media loop "
                "at full FPS on a static deck")

        counts = {}
        orig_update = ControllerKey.update

        def counting_update(self, *a, **k):
            counts[self.index] = counts.get(self.index, 0) + 1
            return orig_update(self, *a, **k)

        ControllerKey.update = counting_update
        try:
            rate = _tick_rate(controller, 2.5)
        finally:
            ControllerKey.update = orig_update

        assert rate < 8, (
            f"media loop ran at {rate:.1f} ticks/s with rolling labels disabled "
            f"-- expected the idle throttle (~2/s); over-wide labels are still "
            f"forcing full-FPS ticks")
        assert not counts, (
            f"keys re-rendered {counts} times on a fully static deck "
            f"(rolling labels disabled)")
        print(f"PASS: rolling disabled -> idle loop ({rate:.1f} ticks/s), 0 renders")
    finally:
        fixtures.teardown(controller)


def check_multiline_no_phantom_scroll() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("scrolllbl-b", rolling=True)
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        available = lm.get_available_width()

        # Build a two-line label whose lines fit but whose single-line
        # getbbox measurement overflows, because the newline counts toward
        # the width. That is the phantom-scroll shape.
        from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
        probe = KeyLabel(controller_input=key, text="m", font_size=15)
        font = lm.get_composed_label("center").get_font()
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        line = "m"
        while measure.textbbox((0, 0), line + "m", font=font)[2] <= available * 0.9:
            line += "m"
        text = f"{line}\n{line}"
        # Measure the premise with the BASIC layout engine. raqm 0.11 and
        # later refuse a control character, while BASIC counts the newline
        # glyph toward the width, and the harness must not depend on the host
        # shaping stack.
        basic_font = ImageFont.truetype(
            font.path, font.size, layout_engine=ImageFont.Layout.BASIC)
        _, _, single_line_w, _ = basic_font.getbbox(text)
        assert single_line_w > available, (
            f"test premise broken: getbbox width {single_line_w} does not "
            f"exceed {available} -- pick a longer line")

        _set_center_label(key, text)
        time.sleep(1.0)

        assert not lm.get_has_scroll_labels(), (
            "multiline label whose lines fit is scroll-flagged: detection is "
            "measuring with single-line getbbox again -- the render "
            "path never scrolls this, so the loop burns full-FPS renders of "
            "identical frames")
        rate = _tick_rate(controller, 2.0)
        assert rate < 8, (
            f"media loop at {rate:.1f} ticks/s over a non-scrolling multiline "
            f"label -- phantom scroll detection is back")
        print(f"PASS: fitting multiline label not scroll-flagged, loop idle ({rate:.1f} ticks/s)")
    finally:
        fixtures.teardown(controller)


def check_scroll_render_budget() -> None:
    from src.backend.DeckManagement.DeckController import ControllerKey
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("scrolllbl-c", rolling=True)
    try:
        keys = controller.inputs[Input.Key]
        assert len(keys) >= 5, "fake deck should expose at least 5 keys"
        for key in keys[:3]:
            _set_center_label(key, WIDE_TEXT)
        _set_center_label(keys[3], "ok")  # static, fits
        scroll_indices = {k.index for k in keys[:3]}
        static_indices = {k.index for k in keys[3:]}

        for key in keys[:3]:
            assert key.get_active_state().label_manager.get_has_scroll_labels(), \
                "over-wide label not detected as scrolling with rolling enabled"

        counts = {}
        orig_update = ControllerKey.update

        def counting_update(self, *a, **k):
            counts[self.index] = counts.get(self.index, 0) + 1
            return orig_update(self, *a, **k)

        # Window 1 is the leading hold, about 1.67s. The offset does not
        # move, so scrolling keys must not re-render even at full-FPS ticks.
        ControllerKey.update = counting_update
        try:
            time.sleep(1.2)
            hold_counts = dict(counts)
            # Window 2 is the sweep. A cadence of 1px per 2/30s allows at
            # most 15 renders per second per scrolling key, plus edge slack.
            window = 3.0
            counts.clear()
            t0 = controller.media_player.media_ticks
            time.sleep(window)
            ticks = controller.media_player.media_ticks - t0
        finally:
            ControllerKey.update = orig_update

        for idx in scroll_indices:
            assert hold_counts.get(idx, 0) <= 3, (
                f"key {idx} rendered {hold_counts.get(idx)} times during the "
                f"leading hold -- renders are not gated on offset movement")

        budget = int(window * 15) + 8
        for idx in scroll_indices:
            n = counts.get(idx, 0)
            assert n <= budget, (
                f"scrolling key {idx} rendered {n} times in {window}s "
                f"(budget {budget}, cadence 15/s) -- per-tick rendering is back")
            assert n >= 10, (
                f"scrolling key {idx} rendered only {n} times in {window}s -- "
                f"the animation is not advancing")
        for idx in static_indices:
            assert counts.get(idx, 0) == 0, (
                f"static key {idx} re-rendered {counts.get(idx)} times while "
                f"another key scrolls -- per-key gating regressed")

        assert ticks / window > 20, (
            f"loop at {ticks / window:.1f} ticks/s while labels scroll -- the "
            f"scroll state machine needs full-rate ticks")
        pos = keys[0].get_active_state().label_manager.frames["center"]["position"]
        assert pos > 5, f"scroll position only reached {pos} -- animation stalled"
        print(f"PASS: render budget held (hold: {sum(hold_counts.values())} renders, "
              f"sweep: {[counts.get(i, 0) for i in sorted(scroll_indices)]}/{budget} "
              f"per key in {window}s, static keys 0), position={pos}")
    finally:
        fixtures.teardown(controller)


def check_strip_matches_direct_draw() -> None:
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("scrolllbl-d", rolling=True)
    try:
        key = controller.inputs[Input.Key][0]
        _set_center_label(key, WIDE_TEXT)
        time.sleep(0.5)
        lm = key.get_active_state().label_manager
        label = lm.get_composed_label("center")
        w, h = lm._measure_text("center", label)
        assert lm.get_has_scroll_labels()

        size = key.get_image_size()
        worst_frac = 0.0
        for position in (0, 5, 60, int(w) - size[0] + 15):
            lm.frames["center"]["position"] = position
            rendered = lm.add_labels_to_image(Image.new("RGBA", size, (30, 60, 90, 255)))

            reference = Image.new("RGBA", size, (30, 60, 90, 255))
            start = size[0] / 2 - (size[0] - w) / 2 + 10
            ImageDraw.Draw(reference).text(
                (start - position, size[1] / 2), text=label.text,
                font=label.get_font(), anchor="mm", align=label.alignment,
                fill=tuple(label.color), stroke_width=label.outline_width,
                stroke_fill=tuple(label.outline_color))

            a = rendered.tobytes()
            b = reference.tobytes()
            n_diff = sum(1 for x, y in zip(a, b) if abs(x - y) > 8)
            frac = n_diff / len(a)
            worst_frac = max(worst_frac, frac)
        assert worst_frac <= 0.002, (
            f"strip composite deviates from direct draw.text on "
            f"{worst_frac:.3%} of channel bytes (allowed 0.2%)")
        print(f"PASS: strip composite matches direct draw (worst deviation {worst_frac:.4%})")
    finally:
        fixtures.teardown(controller)


def check_label_edit_invalidates_detection() -> None:
    """A label edit through Page.set_label_* mutates the KeyLabel in place
    and bypasses set_page_label's cache invalidation. Without an explicit
    invalidate a shortened label keeps scrolling and a lengthened one never
    starts until a page reload."""
    from src.backend.DeckManagement.InputIdentifier import Input

    controller = _make_controller("scrolllbl-e", rolling=True)
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager

        # From long to short. A scrolling label must fall back to the idle
        # rate and a static draw.
        _set_center_label(key, WIDE_TEXT)
        time.sleep(2.0)  # past the leading hold, so it is genuinely sweeping
        assert lm.get_has_scroll_labels(), "wide label not scrolling before edit"

        controller.active_page.set_label_text(key.identifier, 0, "center", "ok", update=True)
        time.sleep(0.3)
        assert not lm.get_has_scroll_labels(), (
            "shortened label still scroll-flagged after set_label_text -- the "
            "detection cache is stale (Page.set_label_* bypasses the invalidator)")
        assert lm.get_scroll_label_widths() == {}, "stale scroll widths after shorten"
        rate = _tick_rate(controller, 1.5)
        assert rate < 8, (
            f"loop at {rate:.1f} t/s after shortening a scrolling label -- stale "
            f"detection is still forcing full-FPS ticks on now-static text")
        # It must also draw statically, with no leftover sweep offset.
        size = key.get_image_size()
        composed = lm.get_composed_label("center")
        w, h = lm._measure_text("center", composed)
        drawn = lm.add_labels_to_image(Image.new("RGBA", size, (0, 0, 0, 255)))
        ref = Image.new("RGBA", size, (0, 0, 0, 255))
        ImageDraw.Draw(ref).text((size[0] / 2, size[1] / 2), text=composed.text,
                                 font=composed.get_font(), anchor="mm",
                                 align=composed.alignment, fill=tuple(composed.color),
                                 stroke_width=composed.outline_width,
                                 stroke_fill=tuple(composed.outline_color))
        a, b = drawn.tobytes(), ref.tobytes()
        dev = sum(1 for x, y in zip(a, b) if abs(x - y) > 8) / len(a)
        assert dev < 0.002, (
            f"shortened label drawn {dev:.2%} off a centered static draw -- it is "
            f"still being composited at a scroll offset")

        # From short to long. It must start scrolling with no page reload.
        controller.active_page.set_label_text(key.identifier, 0, "center", WIDE_TEXT, update=True)
        time.sleep(0.3)
        assert lm.get_has_scroll_labels(), (
            "lengthened label not scroll-flagged after set_label_text -- detection "
            "cache stale, scrolling would not begin until a page reload")
        rate = _tick_rate(controller, 1.5)
        assert rate > 20, (
            f"loop at {rate:.1f} t/s after lengthening a label past key width -- "
            f"scrolling did not resume the full-rate tick")
        print("PASS: editor label edits invalidate scroll detection (long->short idles, "
              "short->long resumes scrolling; no reload)")
    finally:
        fixtures.teardown(controller)


def check_pathological_label_strip_capped() -> None:
    """The precomposed strip is width by key height by 4 bytes of RGBA,
    retained per label, and its width scales with the text length. Past the
    width cap the render falls back to a direct draw, which retains nothing
    and still produces the right pixels."""
    from src.backend.DeckManagement.InputIdentifier import Input
    from src.backend.DeckManagement.DeckController import LabelManager

    controller = _make_controller("scrolllbl-f", rolling=True)
    try:
        key = controller.inputs[Input.Key][0]
        lm = key.get_active_state().label_manager
        size = key.get_image_size()
        cap = LabelManager._MAX_STRIP_WIDTH

        # A normal wide label still uses and retains a strip.
        _set_center_label(key, WIDE_TEXT)
        time.sleep(0.3)
        lm.frames["center"]["position"] = 10
        lm.add_labels_to_image(Image.new("RGBA", size, (0, 0, 0, 255)))
        assert lm._scroll_strips.get("center") is not None, (
            "normal wide label should still use the precomposed strip")

        # A pathological label must retain no strip, and must render the
        # same pixels as a direct draw at the same offset.
        pathological = "m" * 20000
        _set_center_label(key, pathological)
        time.sleep(0.3)
        composed = lm.get_composed_label("center")
        w, h = lm._measure_text("center", composed)
        assert w + 1 > cap, f"probe premise: {w}px text must exceed the {cap}px cap"

        offset = 30
        lm.frames["center"]["position"] = offset
        rendered = lm.add_labels_to_image(Image.new("RGBA", size, (0, 0, 0, 255)))
        assert lm._scroll_strips.get("center") is None, (
            f"a {w}px-wide label retained a strip past the {cap}px cap -- this is "
            f"the uncapped per-label pixel buffer the memory war closed")

        ref = Image.new("RGBA", size, (0, 0, 0, 255))
        start = size[0] / 2 - (size[0] - w) / 2 + 10
        ImageDraw.Draw(ref).text((start - offset, size[1] / 2), text=composed.text,
                                 font=composed.get_font(), anchor="mm",
                                 align=composed.alignment, fill=tuple(composed.color),
                                 stroke_width=composed.outline_width,
                                 stroke_fill=tuple(composed.outline_color))
        a, b = rendered.tobytes(), ref.tobytes()
        dev = sum(1 for x, y in zip(a, b) if abs(x - y) > 8) / len(a)
        assert dev < 0.002, (
            f"capped direct-draw fallback deviates {dev:.2%} from the direct draw")
        print(f"PASS: strip width capped at {cap}px (normal label strips; {w}px "
              f"pathological label falls back to direct draw, 0 retained, dev {dev:.4%})")
    finally:
        fixtures.teardown(controller)


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_scroll_label_cpu")
    check_rolling_disabled_idles()
    check_multiline_no_phantom_scroll()
    check_scroll_render_budget()
    check_strip_matches_direct_draw()
    check_label_edit_invalidates_detection()
    check_pathological_label_strip_capped()
    print("PASS: scenario_scroll_label_cpu")


if __name__ == "__main__":
    main()
