"""
Unit-tier scenario (phase 1b): KeyGIF's per-frame delay
normalization must follow the browser-compatible rule (Firefox/Chrome):

    duration missing or < 20ms  ->  100ms
    otherwise                   ->  used as-is (no x10 anywhere)

The replaced heuristic ("duration < 50 -> x10", inherited from upstream)
had two defects this scenario pins:

  (a) an all-zero-duration GIF produced _total_delay == 0 and froze on
      frame 0 forever (0 * 10 is still 0);
  (b) a legitimate 40ms (25fps) GIF was misread as centiseconds and played
      at 400ms/frame -- 10x too slow.

Drives the REAL KeyGIF.__init__ (decode + timeline build) against synthetic
on-disk GIFs generated with PIL at runtime -- no binary fixtures in-repo
(same approach as scenario_gif_fit.py). GIF delay storage is centiseconds,
so every test duration is a multiple of 10ms and round-trips exactly.
Frames are visually distinct (a shifting disc) so PIL's duplicate-frame
merge can't coalesce them (merging would sum durations and change the
timeline under test).
"""
import os

import fixtures  # noqa: F401  (import first: isolated data dir + sys.path)

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import KeyGIF


class _StubDeckController:
    """Exactly what KeyGIF.__init__ reads (same stub as scenario_gif_fit)."""

    def get_key_image_size(self) -> tuple[int, int]:
        return (72, 72)

    def get_display_saturation(self) -> float:
        return 1.0


class _StubControllerKey:
    def __init__(self):
        self.deck_controller = _StubDeckController()


def _make_gif(path: str, durations_ms: list[int], size=(64, 64)) -> str:
    """An animated GIF with one explicit per-frame duration each; frames are
    visually distinct so PIL never merges them at save time."""
    frames = []
    for i in range(len(durations_ms)):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 4 + i * 6
        draw.ellipse([x0, 10, x0 + 24, 34], fill=(220, 30, 30, 255))
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations_ms, loop=0, disposal=2,
    )
    return path


def _decode(path: str) -> KeyGIF:
    return KeyGIF(controller_key=_StubControllerKey(), gif_path=path, fps=30, loop=True)


def check_all_zero_durations_animate() -> None:
    """(a) all-zero durations -> every frame normalized to 100ms, the
    timeline is non-degenerate, and wall-clock picking actually advances
    (the old heuristic froze on frame 0 via _total_delay == 0)."""
    path = _make_gif(os.path.join(gl.DATA_PATH, "media", "zero_delays.gif"),
                     [0, 0, 0, 0])
    gif = _decode(path)
    try:
        assert gif.frame_delays == [100, 100, 100, 100], (
            f"all-zero durations must normalize to 100ms each, got {gif.frame_delays}"
        )
        assert gif._total_delay > 0, (
            f"_total_delay must be non-degenerate, got {gif._total_delay}"
        )

        T0 = 1_000_000.0
        gif.get_next_frame(now=T0)
        assert gif.active_frame == 0
        gif.get_next_frame(now=T0 + 0.15)  # inside frame 1's 100-200ms window
        assert gif.active_frame == 1, (
            f"a zero-duration GIF must animate at 100ms/frame windows "
            f"(expected frame 1 at t=0.15s, got {gif.active_frame})"
        )
        print("PASS: all-zero durations -> 100ms windows, playback advances")
    finally:
        gif.close()


def check_40ms_frames_kept() -> None:
    """(b) 40ms (25fps) frames are trusted as-is -- NOT inflated x10 to
    400ms by the centisecond misread."""
    path = _make_gif(os.path.join(gl.DATA_PATH, "media", "fast_25fps.gif"),
                     [40, 40, 40, 40])
    gif = _decode(path)
    try:
        assert gif.frame_delays == [40, 40, 40, 40], (
            f"40ms frames must stay 40ms (not x10 -> 400), got {gif.frame_delays}"
        )
        # Timeline windows are 40ms wide: at t=0.05s playback is in frame
        # 1's (40-80ms) window, where the old x10 heuristic (400ms windows)
        # would still be showing frame 0.
        T0 = 1_000_000.0
        gif.get_next_frame(now=T0)
        assert gif.active_frame == 0
        gif.get_next_frame(now=T0 + 0.05)
        assert gif.active_frame == 1, (
            f"40ms windows expected (frame 1 at t=0.05s); x10 inflation would "
            f"leave frame 0 (got {gif.active_frame})"
        )
        print("PASS: 40ms (25fps) frames play at 40ms windows, no x10 inflation")
    finally:
        gif.close()


def check_mixed_durations_normalized() -> None:
    """(c) mixed zero/sub-20ms/valid durations: only the degenerate ones are
    replaced by 100ms; valid ones (>= 20ms) pass through untouched."""
    path = _make_gif(os.path.join(gl.DATA_PATH, "media", "mixed_delays.gif"),
                     [0, 40, 10, 200])
    gif = _decode(path)
    try:
        assert gif.frame_delays == [100, 40, 100, 200], (
            f"expected [100, 40, 100, 200] after normalization, got {gif.frame_delays}"
        )
        # Cumulative edges: 0.1 / 0.14 / 0.24 / 0.44 seconds.
        expected_cum = [0.1, 0.14, 0.24, 0.44]
        assert all(abs(a - b) < 1e-9 for a, b in zip(gif._cum_delays, expected_cum)), (
            f"cumulative timeline mismatch: {gif._cum_delays} != {expected_cum}"
        )
        print("PASS: mixed durations -> zeros/sub-20ms become 100ms, valid kept")
    finally:
        gif.close()


def check_probe_matches_the_full_decode() -> None:
    """(e) The pixel-free timeline probe and the full decode
    must agree to the last float -- a warm KeyGIF (its pixels already in the
    tile cache) builds its timeline from the probe while a cold one builds
    it from the decode walk, so any drift would change playback depending on
    whether the GIF had been seen before. Also pins the frame count and the
    O(1)-RAM contract (the probe retains no frames)."""
    from src.backend.DeckManagement.DeckController import probe_gif_timeline

    path = _make_gif(os.path.join(gl.DATA_PATH, "media", "probe_parity.gif"),
                     [0, 40, 10, 200, 100])
    probe = probe_gif_timeline(path)
    gif = _decode(path)
    try:
        assert probe.n_frames == len(gif.frames) == 5, (
            f"probe/decode frame count mismatch: {probe.n_frames} vs {len(gif.frames)}"
        )
        assert probe.frame_delays == gif.frame_delays, (
            f"probe delays {probe.frame_delays} != decoded delays {gif.frame_delays}"
        )
        assert probe.cum_delays == gif._cum_delays, (
            f"probe timeline {probe.cum_delays} != decoded timeline {gif._cum_delays}"
        )
        assert probe.size == (64, 64), f"probe must report the source size, got {probe.size}"
    finally:
        gif.close()
    print("PASS: the timeline probe reproduces the decoded timeline exactly")


def check_close_leaves_late_ticks_harmless() -> None:
    """(d) close() must leave the object tickable. Teardown races
    the media loop, so a tick can land after close(); the old close() set
    frames/frame_delays to None and then `del`eted them, so the very next
    get_next_frame() raised (TypeError on len(None), AttributeError once the
    attributes were gone) instead of quietly returning nothing. Empty
    containers make the late tick, get_frame_delay(), get_raw_image() and a
    double close() all no-ops by construction."""
    path = _make_gif(os.path.join(gl.DATA_PATH, "media", "close_noop.gif"),
                     [100, 100, 100])
    gif = _decode(path)
    T0 = 1_000_000.0
    gif.get_next_frame(now=T0)

    gif.close()

    assert gif.frames == [] and gif.frame_delays == [] and gif._cum_delays == [], (
        f"close() must swap in fresh EMPTY containers, got "
        f"{gif.frames!r} / {gif.frame_delays!r} / {gif._cum_delays!r}"
    )
    assert gif.get_next_frame(now=T0 + 0.25) is None, (
        "a media tick after close() must return None, not a frame"
    )
    assert gif.get_raw_image() is None, "get_raw_image() after close() must be None"
    # Falls back to the fps-based delay rather than indexing an empty list.
    assert gif.get_frame_delay() == 1.0 / gif.fps, (
        f"get_frame_delay() after close() must fall back to 1/fps, got {gif.get_frame_delay()}"
    )
    assert gif.budget_bytes() == 0, (
        f"a closed GIF holds no frames -- its census share must be 0, got {gif.budget_bytes()}"
    )
    gif.close()  # idempotent
    assert gif.get_next_frame(now=T0 + 0.5) is None
    print("PASS: close() empties the frame list; late ticks and double close are no-ops")


def main() -> None:
    # KeyGIF reads performance.cache-videos at construction: a
    # GIF only has somewhere to route to when the disk cache is on. Every
    # fixture here carries alpha, so they all stay on the frame list either
    # way -- the stub tier just has to exist for the setting to be readable.
    fixtures.install_stub_globals({"performance": {"cache-videos": True}})
    fixtures.start_watchdog(60, label="scenario_gif_delays")
    check_all_zero_durations_animate()
    check_40ms_frames_kept()
    check_mixed_durations_normalized()
    check_probe_matches_the_full_decode()
    check_close_leaves_late_ticks_harmless()
    print("PASS: scenario_gif_delays")


if __name__ == "__main__":
    main()
