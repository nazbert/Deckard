"""
Unit-tier scenario (docs/memory-footprint-impl-plan.md P2.3): KeyGIF must
fit every decoded frame to at most 2x the key tile size at construction,
instead of retaining full source-resolution RGBA frames forever (design doc
§3.2: "41-200MB per GIF key").

Drives the REAL KeyGIF.__init__ against a synthetic on-disk GIF (unlike
scenario_gif_timeline.py, which bypasses __init__ entirely to unit-test the
wall-clock frame-picking arithmetic in isolation).

Covers:
  (a) a GIF much larger than the tile size ends up with every retained
      frame at or below 2x the tile size in both dimensions.
  (b) alpha survives the decode+fit round trip (the reason this stays a PIL
      frame list instead of routing through Mp4FrameCache -- cv2's GIF
      demuxer drops alpha; see the design doc's alpha-probe note).
  (c) a GIF already smaller than the 2x budget keeps its source dimensions
      (shrink-only, same policy as P2.4's static images: upscaling a small
      GIF to the budget would multiply its retained memory -- 40px -> 144px
      is 13x per frame -- for zero display benefit, since compositing
      scales per-tick anyway).
  (d) the source GIF file handle is closed once decoding finishes (no
      dangling fd kept alive behind the fitted frames) -- checked via
      close()'s cleanup and by confirming `self.gif` is no longer retained
      as an attribute (mem-plan P2.3: "close the source PIL handle").
"""
import os

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from PIL import Image, ImageDraw

import globals as gl
from src.backend.DeckManagement.DeckController import KeyGIF


class _StubDeckController:
    """Exposes exactly what KeyGIF.__init__ reads: get_key_image_size() and
    get_display_saturation() (default factor -- saturation has its own
    scenario, scenario_media_saturation.py)."""

    def __init__(self, key_size: tuple[int, int]):
        self._key_size = key_size

    def get_key_image_size(self) -> tuple[int, int]:
        return self._key_size

    def get_display_saturation(self) -> float:
        return 1.0


class _StubControllerKey:
    """SingleKeyAsset only reads controller_input.deck_controller."""

    def __init__(self, key_size: tuple[int, int]):
        self.deck_controller = _StubDeckController(key_size)


def _make_test_gif(path: str, size=(320, 320), n_frames: int = 6) -> None:
    """A small animated GIF with a fully transparent background and an
    opaque colored disc that shifts each frame -- large enough (well above
    2x any of this test's tile sizes) to exercise the fit, and with real
    alpha so the alpha-preservation assertion has something to check."""
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 20 + i * 5
        draw.ellipse([x0, 40, x0 + 180, 220], fill=(220, 30, 30, 255))
        frames.append(frame)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=80, loop=0, disposal=2,
    )


def check_large_gif_is_fit() -> None:
    gif_path = os.path.join(gl.DATA_PATH, "media", "large_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    _make_test_gif(gif_path, size=(320, 320), n_frames=6)

    tile_size = (72, 72)
    budget = (tile_size[0] * 2, tile_size[1] * 2)  # KeyGIF.MAX budget == 2x tile

    key = _StubControllerKey(tile_size)
    gif = KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)

    try:
        assert len(gif.frames) == 6, f"expected 6 decoded frames, got {len(gif.frames)}"

        for i, frame in enumerate(gif.frames):
            assert frame.width <= budget[0] and frame.height <= budget[1], (
                f"frame {i}: {frame.size} exceeds the 2x-tile budget {budget}"
            )
            # The source (320x320) is well above the budget, so the fit must
            # have actually done something, not left it at source res.
            assert frame.width < 320 and frame.height < 320, (
                f"frame {i}: {frame.size} was not downsized from the 320x320 source"
            )
            assert frame.mode == "RGBA", f"frame {i}: expected RGBA, got {frame.mode}"

        # (b) alpha preserved: the disc is opaque, the surrounding
        # background is fully transparent -- both must still be present
        # after decode+fit, on every frame.
        for i, frame in enumerate(gif.frames):
            alphas = frame.getchannel("A").getextrema()
            assert alphas[0] == 0, f"frame {i}: fully-transparent background did not survive fitting (min alpha {alphas[0]})"
            assert alphas[1] == 255, f"frame {i}: opaque disc did not survive fitting (max alpha {alphas[1]})"

        # (d) the source file handle must not be retained behind the fitted
        # frames -- KeyGIF no longer keeps a `self.gif` attribute at all
        # once construction finishes.
        assert not hasattr(gif, "gif"), "KeyGIF must not retain the source PIL handle after decoding"

        print(f"PASS: large GIF (320x320) fit to <= {budget} per frame, alpha preserved, source handle released")
    finally:
        gif.close()


def _count_open_fds_to(path: str) -> int:
    """Number of file descriptors in THIS process currently pointing at
    `path`, resolved through /proc/self/fd symlinks. A real fd count -- not a
    `hasattr` attribute proxy -- so a KeyGIF that leaks the source PIL handle
    (an open fd surviving construction) is caught directly."""
    real = os.path.realpath(path)
    count = 0
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(os.path.join("/proc/self/fd", entry))
        except OSError:
            continue
        if os.path.realpath(target) == real:
            count += 1
    return count


def check_non_square_gif_preserves_aspect_ratio() -> None:
    """#71 (e): the scenario never asserted aspect ratio. KeyGIF fits with
    ImageOps.contain, which shrinks to fit the 2x-tile budget while preserving
    the source aspect ratio -- a non-square GIF must NOT be squished to the
    budget's square. Build a 2:1 source well above budget and assert every
    fitted frame keeps the 2:1 ratio (and fits within budget)."""
    gif_path = os.path.join(gl.DATA_PATH, "media", "wide_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    # 320x160 == 2:1, well above the 144x144 budget for a 72px tile.
    _make_test_gif(gif_path, size=(320, 160), n_frames=4)

    tile_size = (72, 72)
    budget = (tile_size[0] * 2, tile_size[1] * 2)  # 144x144
    key = _StubControllerKey(tile_size)
    gif = KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)

    try:
        src_ratio = 320 / 160  # 2.0
        for i, frame in enumerate(gif.frames):
            assert frame.width <= budget[0] and frame.height <= budget[1], (
                f"frame {i}: {frame.size} exceeds the 2x-tile budget {budget}"
            )
            # The wide source must have actually shrunk (not left at source).
            assert frame.width < 320, f"frame {i}: {frame.size} was not downsized from the 320px-wide source"
            # Aspect ratio preserved within a 1px rounding tolerance -- contain
            # never squishes a non-square source into the square budget.
            frame_ratio = frame.width / frame.height
            assert abs(frame_ratio - src_ratio) < 0.05, (
                f"frame {i}: aspect ratio {frame_ratio:.3f} ({frame.size}) does "
                f"not match the 2:1 source -- the fit squished it"
            )
            # Concretely: a 2:1 source fit into a 144x144 budget must land at
            # width==144 (the binding dimension), height==72.
            assert frame.width == 144 and frame.height == 72, (
                f"frame {i}: expected a 144x72 aspect-preserving fit, got {frame.size}"
            )
        print("PASS: a non-square GIF keeps its aspect ratio through the fit (contain, not squish)")
    finally:
        gif.close()


def check_disposal_method_1_gif() -> None:
    """#71 (e): disposal-method-1 (do-not-dispose / incremental-frame) GIFs
    were untested. With disposal=1 each frame is composited onto the previous
    frame's result rather than a cleared canvas, so a naive decode that reads
    raw frame buffers (instead of PIL's coalesced RGBA) loses earlier frames'
    pixels. Build a disposal=1 GIF whose frames ADD an opaque block each step
    and assert the decoded+fitted frames are coalesced (later frames retain
    earlier frames' opaque content) and RGBA."""
    gif_path = os.path.join(gl.DATA_PATH, "media", "disposal1_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)

    # Build incremental frames: frame k paints k+1 opaque stripes on a
    # transparent base; with disposal=1 each is drawn over the prior, so a
    # correctly-coalesced decode shows strictly non-decreasing opaque area.
    size = (160, 160)
    n_frames = 4
    frames = []
    accumulated = Image.new("RGBA", size, (0, 0, 0, 0))
    for k in range(n_frames):
        step = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(step)
        x0 = k * 30
        draw.rectangle([x0, 0, x0 + 25, size[1]], fill=(50, 200, 50, 255))
        # What the viewer should see after coalescing frame k:
        accumulated = Image.alpha_composite(accumulated, step)
        frames.append(step)

    frames[0].save(
        gif_path, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=1,  # 1 = do not dispose (incremental)
    )

    tile_size = (48, 48)
    key = _StubControllerKey(tile_size)
    gif = KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)
    try:
        assert len(gif.frames) == n_frames, f"expected {n_frames} decoded frames, got {len(gif.frames)}"

        # Coalescing check: the count of opaque pixels must be non-decreasing
        # across frames (each incremental frame ADDS a stripe over the prior).
        # A decode that dropped earlier frames' pixels (treated disposal=1 as
        # a clear) would show a constant single-stripe area instead.
        opaque_counts = []
        for i, frame in enumerate(gif.frames):
            assert frame.mode == "RGBA", f"frame {i}: expected RGBA, got {frame.mode}"
            alpha = frame.getchannel("A")
            opaque = sum(1 for a in alpha.getdata() if a > 0)
            opaque_counts.append(opaque)

        for i in range(1, n_frames):
            assert opaque_counts[i] >= opaque_counts[i - 1], (
                f"disposal=1 frames must coalesce (non-decreasing opaque area): "
                f"frame {i} has {opaque_counts[i]} opaque px vs frame {i-1}'s "
                f"{opaque_counts[i-1]} -- earlier content was lost"
            )
        # The last frame must have strictly more opaque area than the first
        # (four stripes vs one) -- proves real incremental accumulation, not a
        # vacuously-equal sequence.
        assert opaque_counts[-1] > opaque_counts[0], (
            "the final coalesced frame must contain more opaque content than "
            "the first -- disposal=1 accumulation was not decoded"
        )
        print("PASS: disposal-method-1 incremental-frame GIF decodes coalesced RGBA frames")
    finally:
        gif.close()


def check_source_fd_released() -> None:
    """#71 (e): the source-handle check was a `hasattr(gif, 'gif')` attribute
    proxy. Replace it with a REAL fd assertion: count open fds pointing at the
    source file via /proc/self/fd immediately before and after construction.
    KeyGIF opens the source only for the decode loop and closes it in a
    finally -- so after __init__ returns, zero fds may point at it."""
    gif_path = os.path.join(gl.DATA_PATH, "media", "fd_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    _make_test_gif(gif_path, size=(200, 200), n_frames=5)

    fds_before = _count_open_fds_to(gif_path)
    assert fds_before == 0, f"fixture sanity: no fd should point at the source before construction, saw {fds_before}"

    key = _StubControllerKey((64, 64))
    gif = KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)
    try:
        fds_after = _count_open_fds_to(gif_path)
        assert fds_after == 0, (
            f"KeyGIF must release the source file descriptor after decoding "
            f"(mem-plan P2.3), but {fds_after} fd(s) still point at the source"
        )
        # And the attribute proxy the old test used must also hold (defense in
        # depth, not the primary check).
        assert not hasattr(gif, "gif"), "KeyGIF must not retain the source PIL handle attribute"
        print("PASS: KeyGIF releases the source file descriptor after construction (real fd count)")
    finally:
        gif.close()


def check_small_gif_keeps_source_size() -> None:
    gif_path = os.path.join(gl.DATA_PATH, "media", "small_test.gif")
    os.makedirs(os.path.dirname(gif_path), exist_ok=True)
    _make_test_gif(gif_path, size=(40, 40), n_frames=3)

    tile_size = (72, 72)  # budget = 144x144, well above the 40x40 source
    budget = (tile_size[0] * 2, tile_size[1] * 2)
    key = _StubControllerKey(tile_size)
    gif = KeyGIF(controller_key=key, gif_path=gif_path, fps=30, loop=True)

    try:
        for i, frame in enumerate(gif.frames):
            assert frame.size == (40, 40), (
                f"frame {i}: a smaller-than-budget source must keep its own "
                f"dimensions (shrink-only fit), got {frame.size}"
            )
        print(f"PASS: small GIF (40x40) kept at source size (budget {budget} not forced)")
    finally:
        gif.close()


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_gif_fit")
    check_large_gif_is_fit()
    check_small_gif_keeps_source_size()
    check_non_square_gif_preserves_aspect_ratio()
    check_disposal_method_1_gif()
    check_source_fd_released()
    print("PASS: scenario_gif_fit")


if __name__ == "__main__":
    main()
