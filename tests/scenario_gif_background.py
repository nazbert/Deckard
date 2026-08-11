"""
Integration-tier scenario (phase 2): GIF deck backgrounds must
decode through the PIL provider (GifBackground), preserving colors and
alpha, instead of the cv2 canvas cache (whose GIF demuxer mangles palette
frames and drops the alpha channel -- the defect upstream's bf218894
confirmed).

Covers:
  (a) a .gif deck background lands a GifBackground (not BackgroundVideo)
      on Background.video;
  (b) the published key tile byte-matches an INDEPENDENT PIL decode of the
      identified source frame (fit to canvas + crop) -- catches BGR swaps
      and palette mangling outright;
  (c) alpha survives to the composite boundary (RGBA tiles, transparent
      region intact on the retained frame);
  (d) with extend-to-touchscreen on, the strip slice is published
      (get_touchscreen_image) at strip resolution, RGBA;
  (e) the per-touchscreen strip-background route
      (ControllerTouchScreenState._get_background_video_frame) also diverts
      .gif to GifBackground and returns an exact strip-size RGBA frame that
      byte-matches the independent reference (the alpha_composite in
      get_current_image needs same-size RGBA -- exactness is load-bearing).

GIF fixtures are generated with PIL at runtime -- no binary fixtures.
"""
import json
import os

import fixtures
import globals as gl
from fixtures import start_watchdog, teardown

from PIL import Image, ImageDraw, ImageOps

from src.backend.DeckManagement.InputIdentifier import Input

DISC_COLOR = (200, 40, 60, 255)  # asymmetric R/B so a BGR swap can't hide


def _make_gif(path: str, size=(64, 64), n_frames: int = 4) -> str:
    """Transparent background + an opaque disc that shifts per frame
    (distinct frames, so PIL never merges any at save time)."""
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 8 + i * 4
        draw.ellipse([x0, 12, x0 + 28, 40], fill=DISC_COLOR)
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=2,
    )
    return path


def _reference_canvas(gif_path: str, frame_index: int, canvas_size) -> Image.Image:
    """Independent PIL decode of one source frame, fitted the way
    GifBackground fits (exact-canvas ImageOps.fit, LANCZOS) -- computed
    from scratch here so the comparison can't inherit a provider bug."""
    with Image.open(gif_path) as gif:
        gif.seek(frame_index)
        return ImageOps.fit(gif.convert("RGBA"), tuple(canvas_size), Image.Resampling.LANCZOS)


def _seed_deck_background(serial: str, gif_path: str) -> None:
    """Put the extended GIF background in the deck's settings BEFORE the
    controller exists.

    check_deck_background sets the same background on the object directly,
    which is the state it means to test. But the controller loads the deck's
    background on a worker thread while it is starting up, and that load reads
    these settings -- so with nothing on disk it read `extend-to-touchscreen`
    absent, defaulted to False, and turned the extension back off underneath
    the check whenever it landed after it. Seeding the values the check sets
    makes the two agree: whichever order they run in, both apply the same
    background with the same geometry. It also means the check now covers the
    real read path -- a deck-scope background, loaded from the deck's own
    settings -- rather than only the direct setters.

    Written raw because the settings manager does not exist yet: the fixture
    builds it while it builds the controller, which is the point of writing
    this first.
    """
    path = os.path.join(gl.DATA_PATH, "settings", "decks", f"{serial}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        # loop/fps match set_from_path's defaults, which is what the check
        # calls, so the two applications are identical in every argument.
        json.dump({"background": {
            "enable": True,
            "extend-to-touchscreen": True,
            "media-path": gif_path,
            "loop": True,
            "fps": 30,
        }}, f)


def _await_startup_background(controller) -> None:
    """Wait out the controller's own background load before touching it.

    The load runs on a worker thread as the controller starts, and it goes
    through the same setters this check does -- so anything it does after the
    check has begun lands in the middle of it: the flag turned back off, or a
    rebuild in flight when the check asks for the published tile (which is
    None mid-rebuild, by contract). Seeding the deck settings above makes the
    two agree on WHAT to apply; this makes them agree on WHEN, so the check
    reads a settled background instead of one still being built.

    The published tile is the signal because it is the last thing the load
    publishes and exactly what the check reads. The lock is then taken and
    released to fence the worker's exit from the load itself: the tile appears
    while it is still held."""
    assert fixtures.wait_until(
        lambda: controller.background.get_identified_tile(0) is not None, timeout=10.0
    ), "the controller never published its deck-scope background"
    with controller._background_load_lock:
        pass


def check_deck_background(controller, gif_path: str) -> None:
    _await_startup_background(controller)

    background = controller.background
    background.set_extend_to_touchscreen(True, update=False)
    background.set_from_path(gif_path)

    video = background.video
    assert type(video).__name__ == "GifBackground", (
        f"(a) a .gif deck background must land a GifBackground, got {type(video).__name__}"
    )
    assert video.extend_touchscreen, "fixture sanity: the FakeDeck is touch-capable, extension must stick"

    # (c) alpha survives decode: the retained canvas frames carry both fully
    # transparent and fully opaque pixels.
    alphas = video.frames[0].getchannel("A").getextrema()
    assert alphas[0] == 0 and alphas[1] == 255, (
        f"(c) alpha did not survive the background decode (extrema {alphas})"
    )

    # (b) published tile == independent reference decode, byte for byte.
    # get_identified_tile hands (tile, identity) out as ONE read, so the
    # frame the media tick lands on meanwhile can't tear the pair.
    identified = background.get_identified_tile(0)
    assert identified is not None, "(b) no identified tile published after set_from_path"
    tile, (md5, frame_index) = identified
    assert md5 == video.video_md5, "(b) identity must carry the provider's source md5"
    assert tile.mode == "RGBA", f"(c) background tiles must be RGBA, got {tile.mode}"

    ref_canvas = _reference_canvas(gif_path, frame_index, video.canvas_size)
    ref_tile = ref_canvas.crop(video._key_regions[0])
    assert tile.tobytes() == ref_tile.tobytes(), (
        "(b) key tile does not match the independent PIL reference decode -- "
        "palette/BGR/geometry mangling on the background path"
    )

    # (d) strip slice published for the extended background.
    strip = background.get_touchscreen_image()
    assert strip is not None, "(d) extended GIF background must publish a strip slice"
    assert tuple(strip.size) == tuple(controller.get_touchscreen_image_size()), (
        f"(d) strip slice must be at strip resolution, got {strip.size}"
    )
    assert strip.mode == "RGBA", f"(d) strip slice must be RGBA, got {strip.mode}"

    print("PASS: deck background routed to GifBackground; tiles/strip match reference, alpha intact")


def check_strip_background_route(controller, gif_path: str) -> None:
    ts = controller.inputs[Input.Touchscreen][0]
    ts_state = ts.get_active_state()

    frame = ts_state._get_background_video_frame(gif_path, fps=30, loop=True)
    # Capture immediately: the media tick's get_current_image releases a
    # background_video its page config doesn't back, so only local refs are
    # stable from here on.
    bg_video = ts_state.background_video
    assert type(bg_video).__name__ == "GifBackground", (
        f"(e) a .gif strip background must land a GifBackground, got {type(bg_video).__name__}"
    )
    frame_index = bg_video.active_frame

    strip_dims = tuple(ts.get_screen_dimensions())
    assert frame is not None and frame.mode == "RGBA", (
        f"(e) strip route must return an RGBA frame, got {None if frame is None else frame.mode}"
    )
    assert tuple(frame.size) == strip_dims, (
        f"(e) strip route must return an EXACT strip-size frame "
        f"(alpha_composite needs same-size), got {frame.size} vs {strip_dims}"
    )

    ref = _reference_canvas(gif_path, frame_index, strip_dims)
    assert frame.tobytes() == ref.tobytes(), (
        "(e) strip frame does not match the independent PIL reference decode"
    )

    print("PASS: per-touchscreen strip background routed to GifBackground, exact-size RGBA")


def main() -> None:
    start_watchdog(60, label="scenario_gif_background")
    gif_path = _make_gif(os.path.join(gl.DATA_PATH, "media", "bg.gif"))

    _seed_deck_background("gif-bg-1", gif_path)
    controller = fixtures.make_headless_controller(serial="gif-bg-1")
    try:
        check_deck_background(controller, gif_path)
        check_strip_background_route(controller, gif_path)
        print("PASS: scenario_gif_background")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
