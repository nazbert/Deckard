"""
Unit-tier scenario (docs/presenter-migration-plan.md §4 M4, §7 "KeyVideo
during cache build"; ported to the tile-cache registry by
docs/memory-footprint-impl-plan.md P2.1/P2.2): InputVideo.get_next_frame()
must advance sequentially (+1, loop-wrapped) while its cache is still being
decoded, and only switch to wall-clock picking once the cache reports
complete.

This is the C-F8 regression the plan calls out: picking frames by wall-clock
BEFORE the cache is complete would jump the requested frame index around
under slow/simulated ticks, and a still-decoding cache has to walk every
intermediate frame to reach an out-of-order index (the old
key_video_cache.py's VideoFrameCache.get_frame() did this by decoding+
disk-writing each one; mp4_tile_cache.Mp4FrameCache._decode_source_frame()
does it by re-seeking the source capture and decoding forward -- same
amplification risk, different mechanism) -- i.e. each jumped frame silently
decodes every frame in between. A stub cache that counts get_frame calls per
index makes that amplification directly observable: sequential advance must
call get_frame exactly once per InputVideo.get_next_frame() call, in
monotonic +1 steps.

Drives InputVideo directly via __new__ (skips __init__, which acquires a
real KeyVideoCache reader from the registry, opening cv2 captures) with a
stub cache object exposing exactly the surface InputVideo reads
(n_frames, is_cache_complete(), get_frame(n)) -- this is a pure
arithmetic/call-count test, independent of cv2/disk decoding or the
registry.
"""
import fixtures
from src.backend.DeckManagement.Subclasses.KeyVideo import InputVideo


class StubKeyVideoCache:
    """Mimics mp4_tile_cache.KeyVideoCache's public surface used by
    InputVideo: n_frames, is_cache_complete(), get_frame(n). Counts how many
    times each frame index is decoded so amplification is directly
    assertable."""

    def __init__(self, n_frames: int):
        self.n_frames = n_frames
        self._complete = False
        self.decode_counts: dict[int, int] = {}
        self.call_log: list[int] = []  # every index actually requested, in order

    def is_cache_complete(self) -> bool:
        return self._complete

    def get_frame(self, n: int):
        n = min(n, self.n_frames - 1)  # KeyVideoCache.get_frame does the same clamp
        self.decode_counts[n] = self.decode_counts.get(n, 0) + 1
        self.call_log.append(n)
        return n  # the "frame" is just its own index -- enough to assert on


def make_video(n_frames: int, fps: float = 10.0, loop: bool = True) -> InputVideo:
    v = InputVideo.__new__(InputVideo)
    v.fps = fps
    v.loop = loop
    v.active_frame = -1
    v._play_start = None
    v._last_frame_tick = None
    v.video_cache = StubKeyVideoCache(n_frames)
    return v


def main() -> None:
    T0 = 1_000_000.0

    # --- Building phase: sequential +1 advance, one get_frame call per
    # InputVideo.get_next_frame() call, regardless of how large the
    # wall-clock jump between ticks is (a "slow media loop" tick pattern). ---
    v = make_video(n_frames=5, fps=10.0, loop=True)

    ticks = [T0, T0 + 0.01, T0 + 50.0, T0 + 50.02, T0 + 9000.0]  # deliberately erratic
    expected_sequence = [0, 1, 2, 3, 4]  # sequential, independent of `now`
    for i, now in enumerate(ticks):
        frame = v.get_next_frame(now=now)
        assert frame == expected_sequence[i], (
            f"building phase must advance sequentially regardless of wall-clock "
            f"jumps: tick {i} expected frame {expected_sequence[i]}, got {frame}"
        )
        assert v.active_frame == expected_sequence[i]

    # No amplification: exactly one decode per tick, one per frame index,
    # never more (the C-F8 failure mode is a jump causing several extra
    # get_frame calls to walk through the skipped intermediate indices).
    assert len(v.video_cache.call_log) == len(ticks), (
        f"expected exactly {len(ticks)} get_frame calls (one per tick), "
        f"got {len(v.video_cache.call_log)}: {v.video_cache.call_log}"
    )
    assert all(count == 1 for count in v.video_cache.decode_counts.values()), (
        f"each frame index must be requested exactly once, got {v.video_cache.decode_counts}"
    )

    # One more tick wraps (loop=True, n_frames=5): index 4 -> 0.
    wrapped = v.get_next_frame(now=T0 + 9000.1)
    assert wrapped == 0 and v.active_frame == 0, f"building-phase loop wrap: expected 0, got {wrapped}"

    # --- Non-looping build: active_frame is allowed to run past n_frames
    # (get_frame's own clamp handles it); it must NOT wrap. ---
    v_noloop = make_video(n_frames=3, fps=10.0, loop=False)
    for now in (T0, T0 + 1, T0 + 2, T0 + 3):
        v_noloop.get_next_frame(now=now)
    assert v_noloop.active_frame == 3, f"non-loop build must not wrap, got {v_noloop.active_frame}"
    assert v_noloop.video_cache.call_log[-1] == 2, "get_frame must clamp to the last valid index"

    # --- Flip to complete: wall-clock picking engages, seeded from the
    # current position so it continues (not restarts) from the build phase. ---
    v.video_cache._complete = True
    pre_switch_active_frame = v.active_frame  # 0, from the wrap above
    v.video_cache.call_log.clear()
    v.video_cache.decode_counts.clear()

    t0 = T0 + 20000.0
    first_complete = v.get_next_frame(now=t0)
    # Seed formula: _play_start = now - (active_frame + 1) / fps: the first
    # wall-clock pick continues one frame past where sequential advance left
    # off. Tolerate a +/-1 float-rounding wobble at the exact frame boundary
    # (now - play_start reconstructing (active_frame+1)/fps isn't bit-exact
    # at large wall-clock magnitudes) -- the same formula BackgroundVideo
    # uses unmodified; what matters is continuity, not sub-frame precision.
    expected_first = (pre_switch_active_frame + 1) % v.video_cache.n_frames
    acceptable = {expected_first, (expected_first - 1) % v.video_cache.n_frames}
    assert first_complete in acceptable, (
        f"wall-clock pick must seed from the build-phase position: "
        f"expected one of {acceptable}, got {first_complete}"
    )

    # Wall-clock jump now genuinely jumps the frame (no amplification
    # concern once complete -- get_frame is a free lookup): 0.7s at fps=10
    # is 7 frames ahead, wrapping mod 5.
    jumped = v.get_next_frame(now=t0 + 0.7)
    # Compute directly from the wall-clock formula rather than re-deriving
    # frame arithmetic by hand: frame = int((now - play_start) * fps).
    expected_jumped = int((t0 + 0.7 - v._play_start) * v.fps) % v.video_cache.n_frames
    assert jumped == expected_jumped, f"expected wall-clock jump to frame {expected_jumped}, got {jumped}"
    # And it must be a single free lookup, not a walk through intermediates.
    assert len(v.video_cache.call_log) == 2, (
        f"wall-clock phase must do exactly one get_frame per get_next_frame call, "
        f"got {v.video_cache.call_log}"
    )

    # --- Gap clamp once complete: a >1s tick gap (page-away resume) shifts
    # the timebase instead of fast-forwarding (mirrors BackgroundVideo). ---
    last_tick_before = v._last_frame_tick
    play_start_before = v._play_start
    GAP = 5.0
    v.get_next_frame(now=last_tick_before + GAP)
    expected_play_start = play_start_before + (GAP - 1.0 / v.fps)
    assert abs(v._play_start - expected_play_start) < 1e-9, (
        f"gap clamp did not shift _play_start as expected: "
        f"{v._play_start} != {expected_play_start}"
    )

    print("PASS: scenario_keyvideo_build")


if __name__ == "__main__":
    main()
