"""
Unit-tier scenario for issue #68 ask (4): the KeyVideoCache tile-cache
registry (mp4_tile_cache.acquire/_registry_key) must carry the saturation as
a *distinguishing* dimension -- two key/dial videos of the same source and
tile size but DIFFERENT display-saturation factors must resolve to distinct
_TileCacheEntry objects, distinct on-disk cache files, and each must bake in
its own factor.

This is the SEPARATION direction of the registry's saturation key. The
existing scenario_tile_cache.check_saturation_key_and_path_agree() proves the
COLLAPSE direction only (two raw factors that round to the same bucket share
one entry, and the reader targets the file that entry's builder actually
wrote -- issue #53). Neither it nor scenario_display_saturation (which covers
the *background* BackgroundVideoCache naming, a different class) asserts that
two genuinely different factors are kept apart in the key/dial tile registry
-- the audit draws exactly this distinction (docs/deep-audit-2026-07-10.md
§ commit-6: "KeyVideoCache keys correctly, the bg path doesn't"). Without the
saturation component in _registry_key, a 1.3 key/dial video would be served
the 1.0 cache file (or vice-versa) -- the whole "keys correctly" claim.

Covers:
  (a) _registry_key(src, size, 1.0) != _registry_key(src, size, 1.3): the key
      tuple's saturation component differs, so the two never collide in
      _registry.
  (b) end to end: acquire() at 1.0 and at 1.3 for the same (source, size)
      return readers backed by DISTINCT registry entries and DISTINCT cache
      file paths (the ".satNNN" suffix), each targeting the file its own
      builder writes.
  (c) the two cached canvases actually differ in saturation: the 1.3 reader's
      decoded tile is measurably more saturated than the 1.0 reader's -- the
      key separation is not cosmetic, it selects a differently-baked file.
"""
import os

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import cv2
import numpy as np
from PIL import Image

import globals as gl
from src.backend.DeckManagement.Subclasses import mp4_tile_cache

WATCHDOG_SECONDS = 30


def _mean_hsv_saturation(image: Image.Image) -> float:
    _, s, _ = image.convert("RGB").convert("HSV").split()
    data = list(s.getdata())
    return sum(data) / len(data)


def _make_vivid_video(path: str, n_frames: int = 12, size=(160, 120)) -> None:
    """A vivid, saturated source so a 1.3 boost is unambiguous in the decoded
    tile (a low-chroma source could hide the difference in rounding)."""
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    # cv2 uses BGR: a vivid red band.
    frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    frame[:, :] = (30, 30, 220)  # BGR -> vivid red
    for _ in range(n_frames):
        writer.write(frame)
    writer.release()


def check_registry_key_separates_saturation() -> None:
    """(a) the pure key function keeps distinct factors distinct."""
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "sat_sep_key.mp4")
    _make_vivid_video(video_path, n_frames=4)
    size = (48, 48)

    key_plain = mp4_tile_cache._registry_key(video_path, size, 1.0)
    key_boost = mp4_tile_cache._registry_key(video_path, size, 1.3)

    # Same source + size => same md5 + size components; only the saturation
    # component may differ, and it MUST.
    assert key_plain[0] == key_boost[0], "same source must share the md5 component"
    assert key_plain[1] == key_boost[1], "same tile size must share the size component"
    assert key_plain[2] != key_boost[2], (
        f"1.0 and 1.3 must map to DIFFERENT registry-key saturation buckets, "
        f"got {key_plain[2]} vs {key_boost[2]} -- distinct factors collapsing "
        f"into one key would serve one file's pixels for both"
    )
    assert key_plain != key_boost, "the full registry keys must differ"

    print("PASS: _registry_key keeps distinct saturation factors distinct")


def check_acquire_separates_entries_and_files() -> None:
    """(b)+(c) end to end: two factors -> two entries, two files, two bakes."""
    fixtures.install_stub_globals()
    video_path = os.path.join(gl.DATA_PATH, "sat_sep_acquire.mp4")
    _make_vivid_video(video_path)
    size = (48, 48)

    r_plain = mp4_tile_cache.acquire(video_path, size, 1.0)
    r_boost = mp4_tile_cache.acquire(video_path, size, 1.3)
    try:
        # (b) distinct registry entries and distinct on-disk cache files.
        assert r_plain._registry_entry is not r_boost._registry_entry, (
            "1.0 and 1.3 must attach to different _TileCacheEntry objects -- "
            "sharing one entry means sharing one builder + one cache file"
        )
        assert r_plain.cache_path != r_boost.cache_path, (
            f"the two factors must target different cache files, both got "
            f"{r_plain.cache_path!r}"
        )
        # The boosted file must carry the ".satNNN" suffix; the plain one must
        # not (today's default-factor filename stays unadorned).
        plain_name = os.path.basename(r_plain.cache_path)
        boost_name = os.path.basename(r_boost.cache_path)
        assert ".sat" not in plain_name, f"factor 1.0 must keep the plain filename, got {plain_name!r}"
        assert mp4_tile_cache.sat_suffix(1.3) in boost_name, (
            f"factor 1.3 file must carry {mp4_tile_cache.sat_suffix(1.3)!r}, got {boost_name!r}"
        )

        # Both builders promote their own file; wait for both.
        assert fixtures.wait_until(lambda: r_plain._registry_entry.ready, timeout=10.0), \
            "plain-factor builder never promoted its cache"
        assert fixtures.wait_until(lambda: r_boost._registry_entry.ready, timeout=10.0), \
            "boosted-factor builder never promoted its cache"
        assert os.path.isfile(r_plain.cache_path) and os.path.isfile(r_boost.cache_path), \
            "both distinct cache files must exist on disk"

        # (c) each reader's decoded tile carries ITS OWN factor: the boosted
        # tile is measurably more saturated than the plain one. Drive one
        # get_frame each so they adopt their promoted files.
        r_plain.get_frame(0)
        r_boost.get_frame(0)
        plain_tile = r_plain.get_frame(0)
        boost_tile = r_boost.get_frame(0)
        assert plain_tile is not None and boost_tile is not None, "both readers must decode a tile"

        sat_plain = _mean_hsv_saturation(plain_tile)
        sat_boost = _mean_hsv_saturation(boost_tile)
        assert sat_boost > sat_plain + 1.0, (
            f"the 1.3 cache file must bake in a measurably higher saturation than "
            f"the 1.0 file: plain={sat_plain:.2f} boosted={sat_boost:.2f} -- if the "
            f"registry collapsed both factors onto one file these would be equal"
        )
    finally:
        mp4_tile_cache.release(r_plain)
        mp4_tile_cache.release(r_boost)

    print(f"PASS: acquire() separates 1.0/1.3 into distinct entries+files "
          f"(sat plain={sat_plain:.2f} boosted={sat_boost:.2f})")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_saturation_tile_registry")

    check_registry_key_separates_saturation()
    check_acquire_separates_entries_and_files()

    print("PASS: scenario_saturation_tile_registry")


if __name__ == "__main__":
    main()
