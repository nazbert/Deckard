"""
Unit-tier scenario (docs/presenter-migration-plan.md §4 M4, §7 "GIF
wall-clock timeline"): KeyGIF.get_next_frame() must pick the time-correct
frame from a cumulative-delay timeline (`itertools.accumulate` over
per-frame delays in seconds), not the old increment-at-render loop.

Drives KeyGIF directly with a synthetic `frame_delays` list (variable
delays, so a single-fps factor would be wrong -- the reason this plan picked
bisect over a fixed-fps ratio). `get_next_frame` takes an optional `now`
(mirrors MediaPlayerThread.check_resume_gap's `now: float = None` pattern,
DeckController.py ~390) so this test is deterministic -- no real sleeping,
no thread.

Bypasses KeyGIF.__init__ (which decodes an actual GIF file from disk) via
__new__ and hand-sets exactly the attributes get_next_frame reads: this
keeps the scenario a pure arithmetic test of the timeline, independent of
PIL/GIF decoding.
"""
import itertools

import fixtures
from src.backend.DeckManagement.DeckController import KeyGIF


def make_gif(frame_delays_ms: list[float], loop: bool = True, n_frames: int = None) -> KeyGIF:
    g = KeyGIF.__new__(KeyGIF)
    g.fps = 30
    g.loop = loop
    g.active_frame = -1
    g._play_start = None
    g._last_frame_tick = None
    g.frame_delays = list(frame_delays_ms)
    n = n_frames if n_frames is not None else len(frame_delays_ms)
    g.frames = list(range(n))  # stand-ins; get_next_frame only indexes them
    g._cum_delays = list(itertools.accumulate(d / 1000.0 for d in g.frame_delays))
    g._total_delay = g._cum_delays[-1] if g._cum_delays else 0.0
    return g


def main() -> None:
    T0 = 1_000_000.0  # arbitrary wall-clock base, far from 0 to catch base-0 bugs

    # --- Boundary picking over variable delays: [0.1, 0.5, 0.2, 1.0]s,
    # cumulative edges at 0.1 / 0.6 / 0.8 / 1.8. ---
    g = make_gif([100, 500, 200, 1000], loop=True)

    frame = g.get_next_frame(now=T0)  # seeds _play_start at elapsed=0
    assert frame == 0 and g.active_frame == 0, f"t=0 must be frame 0, got {frame}"

    edges = [0.1, 0.6, 0.8]
    for i, edge in enumerate(edges):
        just_before = g.get_next_frame(now=T0 + edge - 0.001)
        assert just_before == i, f"just-before edge {edge}: expected frame {i}, got {just_before}"
        just_after = g.get_next_frame(now=T0 + edge + 0.001)
        assert just_after == i + 1, f"just-after edge {edge}: expected frame {i + 1}, got {just_after}"

    # --- Loop wraparound: past the total (1.8s), time wraps modulo total.
    # Step in <1s increments so we exercise the mod-wrap arithmetic, not the
    # >1s gap-reseed path (that's a separate, deliberate test below). ---
    g_wrap = make_gif([100, 500, 200, 1000], loop=True)
    assert g_wrap.get_next_frame(now=T0) == 0  # seed, elapsed 0
    assert g_wrap.get_next_frame(now=T0 + 0.9) == 3  # elapsed .9 -> frame 3
    wrapped = g_wrap.get_next_frame(now=T0 + 1.8)  # elapsed == total exactly -> wraps to 0
    assert wrapped == 0, f"loop wraparound at exactly one full cycle: expected frame 0, got {wrapped}"
    wrapped2 = g_wrap.get_next_frame(now=T0 + 2.45)  # elapsed 2.45 -> t=0.65 in the 2nd cycle -> frame 2
    assert wrapped2 == 2, f"loop wraparound mid next cycle: expected frame 2, got {wrapped2}"

    # --- Non-loop clamp: once elapsed reaches the total, pin to the last
    # frame and stay there (no wrap). Same <1s stepping to reach the end
    # without tripping the gap-reseed path. ---
    g_noloop = make_gif([100, 500, 200, 1000], loop=False)
    assert g_noloop.get_next_frame(now=T0) == 0
    assert g_noloop.get_next_frame(now=T0 + 0.9) == 3
    clamped = g_noloop.get_next_frame(now=T0 + 1.8)
    assert clamped == 3, f"non-loop clamp at the total: expected last frame (3), got {clamped}"
    clamped2 = g_noloop.get_next_frame(now=T0 + 2.7)  # well past the end
    assert clamped2 == 3, f"non-loop clamp past the total: expected last frame (3), got {clamped2}"
    assert g_noloop.active_frame == 3

    # --- Gap re-seed: a >1s gap between picks (page-away/suspend) must
    # shift the timebase so playback resumes near where it left off, not
    # jump forward by the full raw elapsed gap (mirrors BackgroundVideo's
    # get_next_tiles gap clamp, DeckController.py ~1712-1715). ---
    g_gap = make_gif([100, 500, 200, 1000], loop=True)
    g_gap.get_next_frame(now=T0)  # prime: seeds _play_start, always frame 0
    before_gap = g_gap.get_next_frame(now=T0 + 0.3)  # elapsed 0.3 -> frame 1
    assert before_gap == 1
    last_tick_before = g_gap._last_frame_tick
    play_start_before = g_gap._play_start

    GAP = 5.0  # > 1.0s threshold
    after_gap = g_gap.get_next_frame(now=last_tick_before + GAP)
    # frame_period used for the clamp is _cum_delays[0] == 0.1s (mirrors the
    # BackgroundVideo formula: play_start += gap - frame_period).
    expected_play_start = play_start_before + (GAP - g_gap._cum_delays[0])
    assert abs(g_gap._play_start - expected_play_start) < 1e-9, (
        f"gap clamp did not shift _play_start as expected: "
        f"{g_gap._play_start} != {expected_play_start}"
    )
    # Resulting elapsed-since-shifted-start is small (~0.4s), landing back
    # near frame 1 -- not fast-forwarded through several full 1.8s loops.
    assert after_gap == 1, f"gap re-seed should resume at frame 1, got frame {after_gap}"

    # --- Edge cases: 1-frame and 0-frame GIFs must not raise / must be inert. ---
    g_one = make_gif([100], loop=True)
    f1 = g_one.get_next_frame(now=T0)
    f2 = g_one.get_next_frame(now=T0 + 50.0)
    assert f1 == 0 and f2 == 0 and g_one.active_frame == 0, "1-frame GIF must always report frame 0"

    g_zero = KeyGIF.__new__(KeyGIF)
    g_zero.fps = 30
    g_zero.loop = True
    g_zero.active_frame = -1
    g_zero._play_start = None
    g_zero._last_frame_tick = None
    g_zero.frame_delays = []
    g_zero.frames = []
    g_zero._cum_delays = []
    g_zero._total_delay = 0.0
    assert g_zero.get_next_frame(now=T0) is None, "0-frame GIF must return None, not raise"

    print("PASS: scenario_gif_timeline")


if __name__ == "__main__":
    main()
