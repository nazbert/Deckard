"""
Integration-tier scenario (issue #196, phase 1a): plugin-set GIFs must route
to KeyGIF.

ActionCore.set_media used to send every video -- GIFs included -- down the
generic InputVideo path, whose cv2-backed frame cache drops the alpha
channel and ignores per-frame delays. The page-media loader
(ControllerKey.load_from_input_dict) already routed ".gif" to KeyGIF; this
scenario pins the set_media parity fix:

  (a) a KEY action calling set_media(media_path=<gif>) ends up with a
      KeyGIF (not InputVideo) on the state, and the composited key retains
      the GIF's alpha over a colored background (transparent-region pixel
      == background color, opaque-region pixel == the GIF's own content);
  (b) the same call on a DIAL action still takes the non-KeyGIF path
      (KeyGIF is a SingleKeyAsset -- the isinstance(ControllerKey) guard is
      load-bearing) and does not raise;
  (c) a CORRUPT .gif through set_media falls back to the InputVideo path
      without raising -- KeyGIF decodes eagerly (PIL raises on bad media)
      where InputVideo's detached cv2 builder fails soft, and set_media
      never raised into plugin code for bad media before the KeyGIF route
      existed;
  (d) the same corrupt GIF as PAGE media (ControllerKey.load_from_input_dict)
      also fails soft (issue #199): that route never got (c)'s try/except, so
      the raise escaped mid-load and took the rest of the key's state load
      with it -- layout, page background color and the final set_state()
      repaint all skipped. Pins the fallback video AND that the load ran to
      completion past the media block.

Drives the REAL DeckController/Page/ActionCore machinery with a LatchAction
injected via the stub plugin_manager (fixtures helpers), against a GIF
generated with PIL at runtime -- no binary fixtures in-repo.
"""
import json
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import start_watchdog, wait_until, teardown

from PIL import Image, ImageDraw

from src.backend.DeckManagement.InputIdentifier import Input

WATCHDOG_SECONDS = 60

DISC_COLOR = (220, 30, 30, 255)
BG_COLOR = [0, 0, 255, 255]  # page background color for the alpha probe


def _make_transparent_gif(path: str, size=(64, 64), n_frames: int = 4) -> str:
    """Animated GIF: fully transparent background, opaque centered disc that
    shifts slightly each frame (distinct frames so PIL never merges any)."""
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        x0 = 16 + i * 2
        draw.ellipse([x0, 16, x0 + 30, 46], fill=DISC_COLOR)
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(
        path, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0, disposal=2,
    )
    return path


def seed_gif_action_page(page_name: str, key_ident: str, dial_ident: str) -> str:
    """A page whose key AND dial each carry the stub LatchAction as their
    image-control action (fixtures.seed_action_page covers keys only)."""
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    action_state = {"states": {"0": {
        "actions": [{"id": fixtures.STUB_ACTION_ID, "settings": {}}],
        "image-control-action": 0,
    }}}
    with open(path, "w") as f:
        json.dump({
            "keys": {key_ident: action_state},
            "dials": {dial_ident: json.loads(json.dumps(action_state))},
        }, f)
    return path


PAGE_MEDIA_BG_COLOR = [10, 200, 30, 255]  # applied AFTER the media block in the loader


def seed_gif_page_media_page(page_name: str, key_ident: str, media_path: str) -> str:
    """A page whose key carries `media_path` as plain PAGE media (no actions)
    plus a state background color -- the color is set after the media block
    in ControllerKey.load_from_input_dict, so it only lands if the media
    branch returned instead of raising."""
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {key_ident: {"states": {"0": {
                "media": {"path": media_path, "loop": True, "fps": 30},
                "background": {"color": list(PAGE_MEDIA_BG_COLOR)},
            }}}},
            "dials": {},
            "touchscreens": {},
        }, f)
    return path


def _make_repainting_action_class():
    """A LatchAction variant that repaints until its media slot actually
    holds a video, then stops. The plain latch paints exactly once -- but
    the load-time state wipe (create_n_states) restores action-owned media
    on KEY states only (the #131 stash-restore; dial states don't carry
    media_owner_action), so a dial's single paint can be wiped and never
    re-established. Converging (instead of always-painting) keeps the
    composite probe below race-free: once both slots are populated, ticks
    stop swapping the video objects out from under get_current_image()."""
    base = fixtures.make_latch_action_class()

    class RepaintingGifAction(base):
        def on_tick(self):
            state = self.get_state()
            if state is None:
                return
            has_video = (getattr(state, "key_video", None) is not None
                         or getattr(state, "video", None) is not None)
            if not has_video:
                self.set_media(media_path=type(self).icon_path, size=0.8)

    return RepaintingGifAction


def main() -> None:
    action_cls = _make_repainting_action_class()
    gif_path = _make_transparent_gif(os.path.join(gl.DATA_PATH, "media", "action_media.gif"))
    fixtures.install_stub_plugin_manager(action_cls, gif_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_gif_action_media")

    controller = fixtures.make_headless_controller(serial="gif-action-1")
    try:
        key = controller.inputs[Input.Key][0]
        dial = controller.inputs[Input.Dial][0]

        page = gl.page_manager.get_page(
            seed_gif_action_page(
                "GifActions",
                key.identifier.json_identifier,
                dial.identifier.json_identifier,
            ),
            controller,
        )
        controller.load_page(page, allow_reload=True)

        # The actions paint via set_media from the executor threads; wait for
        # both media slots to land (deterministic seam, not a fixed sleep).
        assert wait_until(lambda: key.get_active_state().key_video is not None, timeout=5), (
            "the key action's set_media(<gif>) never landed a video on the state"
        )
        assert wait_until(lambda: dial.get_active_state().video is not None, timeout=5), (
            "the dial action's set_media(<gif>) never landed a video on the state"
        )

        # (a) key: routed to KeyGIF, not InputVideo.
        key_video = key.get_active_state().key_video
        assert type(key_video).__name__ == "KeyGIF", (
            f"a plugin-set GIF on a key must become a KeyGIF, got {type(key_video).__name__}"
        )

        # (b) dial: the ControllerKey guard held -- generic InputVideo path,
        # no KeyGIF, and nothing raised on the way here.
        dial_video = dial.get_active_state().video
        assert type(dial_video).__name__ == "InputVideo", (
            f"a plugin-set GIF on a dial must stay on the InputVideo path, "
            f"got {type(dial_video).__name__}"
        )

        # (a, alpha probe) composite the key over an opaque colored page
        # background: the GIF's transparent region must show the background
        # color (alpha survived decode + composite), the disc must not.
        state = key.get_active_state()
        state.background_manager.set_page_color(list(BG_COLOR), update=False)
        composed = key.get_current_image().convert("RGBA")

        corner = composed.getpixel((2, 2))
        assert corner[:3] == tuple(BG_COLOR[:3]), (
            f"transparent GIF region must composite to the background color "
            f"{tuple(BG_COLOR[:3])}, got {corner} -- alpha was lost"
        )
        center = composed.getpixel((composed.width // 2, composed.height // 2))
        assert center[:3] != tuple(BG_COLOR[:3]), (
            "the GIF's opaque disc must cover the key center, got background "
            "color instead -- no GIF content composited"
        )

        # (c) corrupt GIF: set_media must not raise into the (plugin) caller
        # -- it falls back to the InputVideo path, whose detached cv2
        # builder fails soft downstream (pre-KeyGIF-route behavior).
        corrupt_path = os.path.join(gl.DATA_PATH, "media", "corrupt.gif")
        with open(corrupt_path, "wb") as f:
            f.write(b"not a gif at all, just bytes with the extension")
        action = key.get_active_state().get_own_actions()[0]
        action.set_media(media_path=corrupt_path)  # must not raise
        fallback_video = key.get_active_state().key_video
        assert type(fallback_video).__name__ == "InputVideo", (
            f"a corrupt plugin-set GIF must fall back to InputVideo "
            f"(fail-soft), got {type(fallback_video).__name__}"
        )

        # (d) the same corrupt GIF as PAGE media: loading the page must not
        # leave the key half-loaded. Before the #199 fix KeyGIF's raise
        # escaped load_from_input_dict's media branch, so the state's page
        # layout, its background color and the closing set_state() repaint
        # were all skipped (the exception died in the load pool's future).
        page_media_page = gl.page_manager.get_page(
            seed_gif_page_media_page("GifPageMediaCorrupt", key.identifier.json_identifier, corrupt_path),
            controller,
        )
        controller.load_page(page_media_page, allow_reload=True)
        # The load runs on the controller's input pool; the page color is set
        # at the END of the state load (after the media block), so waiting on
        # it IS the "the load ran to completion" seam.
        assert wait_until(
            lambda: key.get_active_state().background_manager.page_color == PAGE_MEDIA_BG_COLOR,
            timeout=5,
        ), (
            f"the state load must run PAST the media block -- expected page color "
            f"{PAGE_MEDIA_BG_COLOR}, got "
            f"{key.get_active_state().background_manager.page_color!r} (the loader "
            f"raised out of the media branch, issue #199)"
        )
        page_media_video = key.get_active_state().key_video
        assert type(page_media_video).__name__ == "InputVideo", (
            f"a corrupt page-media GIF must fall back to InputVideo (fail-soft), "
            f"got {type(page_media_video).__name__}"
        )

        print("PASS: scenario_gif_action_media")
    finally:
        teardown(controller)


if __name__ == "__main__":
    main()
