"""InputVideo.get_next_frame() must advance sequentially while the cache builds.

Wall-clock picking engages only once the cache reports complete, because a
jumped index makes a building cache walk every frame in between.
"""
import threading

import fixtures
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo


class StubKeyVideoCache:
    """Mimics the KeyVideoCache surface InputVideo reads.

    That is n_frames, is_cache_complete() and get_frame(n). It counts how many
    times each frame index is decoded, so amplification is directly assertable.
    """

    def __init__(self, n_frames: int):
        self.n_frames = n_frames
        self._complete = False
        self.decode_counts: dict[int, int] = {}
        self.call_log: list[int] = []  # every index actually requested, in order

    def is_cache_complete(self) -> bool:
        return self._complete

    def get_source_fps(self) -> float:
        return getattr(self, "source_fps", None)

    def get_frame(self, n: int):
        n = min(n, self.n_frames - 1)  # KeyVideoCache.get_frame does the same clamp
        self.decode_counts[n] = self.decode_counts.get(n, 0) + 1
        self.call_log.append(n)
        return n  # the frame is its own index, which is enough to assert on


def make_video(n_frames: int, fps: float = 10.0, loop: bool = True) -> InputVideo:
    v = InputVideo.__new__(InputVideo)
    v.fps = fps
    v.loop = loop
    v.natural_speed = False  # key and dial semantics, where fps is the playback rate
    v.active_frame = -1
    v._play_start = None
    v._last_frame_tick = None
    v.video_cache = StubKeyVideoCache(n_frames)
    v._close_lock = threading.Lock()  # __init__ sets this; __new__ bypasses it
    return v


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_keyvideo_build")
    T0 = 1_000_000.0

    # Building phase. Sequential advance by one, with one get_frame call per
    # get_next_frame() call, however large the wall-clock jump between ticks
    # is, which models a slow media loop.
    v = make_video(n_frames=5, fps=10.0, loop=True)

    ticks = [T0, T0 + 0.01, T0 + 50.0, T0 + 50.02, T0 + 9000.0]  # erratic, to stress the sequential advance
    expected_sequence = [0, 1, 2, 3, 4]  # sequential, independent of the now argument
    for i, now in enumerate(ticks):
        frame = v.get_next_frame(now=now)
        assert frame == expected_sequence[i], (
            f"building phase must advance sequentially regardless of wall-clock "
            f"jumps: tick {i} expected frame {expected_sequence[i]}, got {frame}"
        )
        assert v.active_frame == expected_sequence[i]

    # No amplification. Exactly one decode per tick and one per frame index,
    # never more. The failure mode is a jump that causes extra get_frame calls
    # to walk through the skipped intermediate indices.
    assert len(v.video_cache.call_log) == len(ticks), (
        f"expected exactly {len(ticks)} get_frame calls (one per tick), "
        f"got {len(v.video_cache.call_log)}: {v.video_cache.call_log}"
    )
    assert all(count == 1 for count in v.video_cache.decode_counts.values()), (
        f"each frame index must be requested exactly once, got {v.video_cache.decode_counts}"
    )

    # One more tick wraps, because loop is True and n_frames is 5.
    wrapped = v.get_next_frame(now=T0 + 9000.1)
    assert wrapped == 0 and v.active_frame == 0, f"building-phase loop wrap: expected 0, got {wrapped}"

    # Non-looping build. active_frame may run past n_frames, because the clamp
    # in get_frame handles it, and it must not wrap.
    v_noloop = make_video(n_frames=3, fps=10.0, loop=False)
    for now in (T0, T0 + 1, T0 + 2, T0 + 3):
        v_noloop.get_next_frame(now=now)
    assert v_noloop.active_frame == 3, f"non-loop build must not wrap, got {v_noloop.active_frame}"
    assert v_noloop.video_cache.call_log[-1] == 2, "get_frame must clamp to the last valid index"

    # Flip to complete. Wall-clock picking engages, seeded from the current
    # position, so it continues from the build phase rather than restarting.
    v.video_cache._complete = True
    pre_switch_active_frame = v.active_frame  # 0, from the wrap above
    v.video_cache.call_log.clear()
    v.video_cache.decode_counts.clear()

    t0 = T0 + 20000.0
    first_complete = v.get_next_frame(now=t0)
    # The seed formula is _play_start = now - (active_frame + 1) / fps, so the
    # first wall-clock pick continues one frame past where sequential advance
    # left off. Tolerate a one-frame float wobble at the exact boundary, because
    # reconstructing that term is not bit-exact at large wall-clock magnitudes.
    # BackgroundVideo uses the same formula unmodified.
    expected_first = (pre_switch_active_frame + 1) % v.video_cache.n_frames
    acceptable = {expected_first, (expected_first - 1) % v.video_cache.n_frames}
    assert first_complete in acceptable, (
        f"wall-clock pick must seed from the build-phase position: "
        f"expected one of {acceptable}, got {first_complete}"
    )

    # A wall-clock jump now genuinely jumps the frame, with no amplification
    # concern once complete, because get_frame is a free lookup. 0.7 s at fps 10
    # is 7 frames ahead, wrapping modulo 5.
    jumped = v.get_next_frame(now=t0 + 0.7)
    # Compute directly from the wall-clock formula rather than re-deriving the
    # frame arithmetic by hand, so frame = int((now - play_start) * fps).
    expected_jumped = int((t0 + 0.7 - v._play_start) * v.fps) % v.video_cache.n_frames
    assert jumped == expected_jumped, f"expected wall-clock jump to frame {expected_jumped}, got {jumped}"
    # It must be a single free lookup, not a walk through intermediates.
    assert len(v.video_cache.call_log) == 2, (
        f"wall-clock phase must do exactly one get_frame per get_next_frame call, "
        f"got {v.video_cache.call_log}"
    )

    # Gap clamp once complete. A tick gap over 1 s, from a page-away resume,
    # shifts the timebase instead of fast-forwarding, mirroring BackgroundVideo.
    last_tick_before = v._last_frame_tick
    play_start_before = v._play_start
    GAP = 5.0
    v.get_next_frame(now=last_tick_before + GAP)
    expected_play_start = play_start_before + (GAP - 1.0 / v.fps)
    assert abs(v._play_start - expected_play_start) < 1e-9, (
        f"gap clamp did not shift _play_start as expected: "
        f"{v._play_start} != {expected_play_start}"
    )

    # natural_speed. Playback runs at the source fps, and fps is only a render
    # cap that quantizes the pick, so composites re-triggered by other animated
    # content within a cap window return the same frame.
    vn = make_video(n_frames=100, fps=5.0, loop=True)  # cap=5
    vn.natural_speed = True
    vn.video_cache.source_fps = 20.0  # native speed, 4x the cap
    vn.video_cache._complete = True

    tn = T0 + 40000.0
    vn.get_next_frame(now=tn)
    base = vn._play_start
    # The position advances at the source fps, so after 1 s it must be about 20
    # frames on, not 5, which is what fps-as-speed would give.
    f_1s = vn.get_next_frame(now=tn + 1.0)
    assert f_1s == int(int((tn + 1.0 - base) * 5.0) / 5.0 * 20.0) % 100, (
        f"natural-speed pick mismatch: got {f_1s}"
    )
    assert f_1s >= 15, (
        f"natural_speed must advance at source fps (~20 frames/s), got {f_1s} after 1s"
    )
    # Within one cap window, 0.2 s at cap 5, the pick must not advance.
    f_a = vn.get_next_frame(now=tn + 2.00)
    f_b = vn.get_next_frame(now=tn + 2.19)
    assert f_a == f_b, (
        f"picks within one 1/cap window must be identical (render cap), got {f_a} then {f_b}"
    )
    # The next window advances by source_fps over cap frames, which is 4.
    f_c = vn.get_next_frame(now=tn + 2.21)
    assert f_c != f_a, "the next cap window must advance the pick"

    print("PASS: scenario_keyvideo_build")


if __name__ == "__main__":
    main()
