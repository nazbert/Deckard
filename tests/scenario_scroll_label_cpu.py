"""
Regression test for the scroll-label CPU fix (issues #115/#116).

Root cause: `_needs_key_ticks()` flipped the media loop from the idle
throttle (2 FPS) to full 30 FPS whenever ANY label measured wider than its
key, and every tick then re-rendered every such key (full composite +
draw.text with stroke), even when

  * rolling labels were DISABLED in the app settings (detection ignored the
    setting entirely -- the #115 reporter's exact configuration),
  * the measured overflow was a getbbox artifact ('\n' counted toward the
    width -- #116), so the render path never actually scrolled, or
  * the scroll offset had not moved this tick (odd half-ticks, and the
    scroll_wait hold plateaus), so the composite was discarded by the hash
    de-dup after the cost was already paid.

The fix: get_has_scroll_labels() honors the rolling-labels setting and
measures with the same multiline-aware textbbox the render uses; scroll
state advances in LabelManager.tick_scroll_labels() on the media tick
(wall-clock cadence, 1px/(2/30)s as before) and keys re-render ONLY when an
offset moved; the scrolling text itself is rasterized once into a cached
strip and each frame composites a window of it.

Asserted here:
  (a) rolling disabled + over-wide labels -> no scroll detection, loop stays
      at the idle rate, zero per-tick renders.
  (b) a multiline label whose LINES fit does not phantom-scroll, even though
      single-line getbbox (the old detector) measures it over-wide.
  (c) rolling enabled + over-wide labels -> renders stay within the scroll
      cadence budget (~15/s per scrolling key, ~0 during the leading hold),
      static keys on the same page never re-render, and the animation
      genuinely advances.
  (d) the strip composite is pixel-equivalent to a direct draw.text of the
      same frame.
"""
import time

import fixtures
import globals as gl

from PIL import Image, ImageDraw

WIDE_TEXT = "m" * 24


def _make_controller(serial: str, rolling: bool):
    """App settings must be in place before labels are composed (the scroll
    caches read them lazily); the controller's async page load must settle
    before the scenario sets labels, or load_all_inputs can wipe them."""
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
                "at full FPS on a static deck (#115)")

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

        # Build a two-line label whose LINES fit but whose single-line
        # getbbox measurement (the old detector: '\n' counts toward the
        # width) overflows -- the #116 phantom-scroll shape.
        from src.backend.DeckManagement.Subclasses.KeyLabel import KeyLabel
        probe = KeyLabel(controller_input=key, text="m", font_size=15)
        font = lm.get_composed_label("center").get_font()
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        line = "m"
        while measure.textbbox((0, 0), line + "m", font=font)[2] <= available * 0.9:
            line += "m"
        text = f"{line}\n{line}"
        _, _, single_line_w, _ = font.getbbox(text)
        assert single_line_w > available, (
            f"test premise broken: getbbox width {single_line_w} does not "
            f"exceed {available} -- pick a longer line")

        _set_center_label(key, text)
        time.sleep(1.0)

        assert not lm.get_has_scroll_labels(), (
            "multiline label whose lines fit is scroll-flagged: detection is "
            "measuring with single-line getbbox again (#116) -- the render "
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

        # Window 1: the leading hold (scroll_wait -> ~1.67s). The offset
        # doesn't move, so scrolling keys must not re-render even though the
        # loop ticks at full FPS.
        ControllerKey.update = counting_update
        try:
            time.sleep(1.2)
            hold_counts = dict(counts)
            # Window 2: the sweep. Budget: cadence is 1px per 2/30s => <=15
            # renders/s per scrolling key, plus slack for the window edges.
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


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_scroll_label_cpu")
    check_rolling_disabled_idles()
    check_multiline_no_phantom_scroll()
    check_scroll_render_budget()
    check_strip_matches_direct_draw()
    print("PASS: scenario_scroll_label_cpu")


if __name__ == "__main__":
    main()
