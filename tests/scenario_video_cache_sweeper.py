"""
Unit-tier scenario for the startup video-cache sweep
(video_cache_sweeper.py), covering issue #53 items 2 and 8:

  (a) a cache whose source video is referenced ONLY from a plugin's own
      settings JSON (settings/plugins/<id>/settings.json) must survive the
      sweep -- the old scan only read deck settings and pages, so these
      were deleted while their in-process registry entries said ready=True.
  (b) a cache file a live tile-cache registry entry points at must survive
      the sweep even when the reference scan can't see its source; once the
      last consumer releases it (and nothing references it), it is swept.
  (c) stale .satNNN saturation variants of a still-referenced video are
      swept; the variant matching a deck's CURRENT display.saturation and
      the unsuffixed default cache are kept.
  (d) regression guard: unreferenced caches are still swept.
"""
import json
import os

import fixtures
import cv2
import numpy as np

import globals as gl
from src.backend.DeckManagement.Subclasses import mp4_tile_cache
from src.backend.DeckManagement.Subclasses import video_cache_sweeper

WATCHDOG_SECONDS = 60


def _make_test_video(path: str, n_frames: int = 10, size=(120, 90), fps: int = 30) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    for i in range(n_frames):
        frame[:, :] = (i % 255, 60, 120)
        writer.write(frame)
    writer.release()


def _seed_cache_file(layout: str, name: str) -> str:
    """The sweep judges files purely by name, so a stand-in cache file only
    needs to exist -- no real mp4 content required."""
    path = os.path.join(video_cache_sweeper.VID_CACHE, layout, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 64)
    return path


def check_plugin_settings_reference_protects_cache() -> None:
    video_path = os.path.join(gl.DATA_PATH, "plugin_only_video.mp4")
    _make_test_video(video_path)
    md5 = video_cache_sweeper._md5_of_file(video_path)

    # The ONLY reference to this video lives in a plugin's settings file.
    plugin_settings = os.path.join(gl.DATA_PATH, "settings", "plugins", "com_example_test", "settings.json")
    os.makedirs(os.path.dirname(plugin_settings), exist_ok=True)
    with open(plugin_settings, "w") as f:
        json.dump({"media": {"video-path": video_path}}, f)

    kept = _seed_cache_file("keys_64x64", f"{md5}.mp4")
    stale = _seed_cache_file("keys_64x64", "0" * 32 + ".mp4")

    video_cache_sweeper.sweep_stale_video_caches()

    assert os.path.isfile(kept), (
        "a cache whose source is referenced only from plugin settings must "
        "survive the sweep"
    )
    assert not os.path.isfile(stale), "unreferenced caches must still be swept"

    print("PASS: plugin-settings references protect caches; unreferenced ones still swept")


def check_live_registry_entry_protects_cache() -> None:
    video_path = os.path.join(gl.DATA_PATH, "registry_only_video.mp4")
    _make_test_video(video_path, n_frames=15)

    reader = mp4_tile_cache.acquire(video_path, (48, 48), 1.0)
    try:
        entry = reader._registry_entry
        assert fixtures.wait_until(lambda: entry.ready, timeout=10.0), "builder never promoted"
        assert os.path.isfile(entry.path)

        # The source is referenced by NO settings file -- only the live
        # registry entry proves the cache is in use.
        video_cache_sweeper.sweep_stale_video_caches()
        assert os.path.isfile(entry.path), (
            "the sweep must not delete a cache file a live registry entry "
            "points at"
        )
    finally:
        mp4_tile_cache.release(reader)

    # Released and unreferenced -> now it is genuinely stale.
    video_cache_sweeper.sweep_stale_video_caches()
    assert not os.path.isfile(entry.path), "a released, unreferenced cache must be swept"

    print("PASS: live registry entries protect their cache files until release")


def check_stale_sat_variants_swept() -> None:
    video_path = os.path.join(gl.DATA_PATH, "saturated_video.mp4")
    _make_test_video(video_path)
    md5 = video_cache_sweeper._md5_of_file(video_path)

    # Reference the video from a page background so its hash is protected.
    fixtures.seed_page_with_background("SatSweepPage", video_path)

    # One deck currently at saturation 1.3.
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    os.makedirs(decks_dir, exist_ok=True)
    with open(os.path.join(decks_dir, "TESTDECK1.json"), "w") as f:
        json.dump({"display": {"saturation": 1.3}}, f)

    default = _seed_cache_file("keys_72x72", f"{md5}.mp4")
    current = _seed_cache_file("keys_72x72", f"{md5}.sat130.mp4")
    stale_a = _seed_cache_file("keys_72x72", f"{md5}.sat120.mp4")
    stale_b = _seed_cache_file("3x5", f"{md5}.sat90.mp4")

    video_cache_sweeper.sweep_stale_video_caches()

    assert os.path.isfile(default), "the unsuffixed default-factor cache must always be kept"
    assert os.path.isfile(current), "the variant matching a deck's current factor must be kept"
    assert not os.path.isfile(stale_a), (
        "a saturation variant no deck's current factor produces must be swept"
    )
    assert not os.path.isfile(stale_b), (
        "stale variants must be swept in background-layout dirs too"
    )

    print("PASS: stale saturation variants of referenced videos are swept, current+default kept")


def check_out_of_range_saturation_protects_clamped_variant() -> None:
    """Issue #53 item 8 (round 1): a persisted display.saturation outside the
    valid [1.0, 1.5] range (corruption or a hand-edit) is clamped by the
    runtime before it derives a cache filename, so playback writes the
    CLAMPED variant. The sweep must protect that same clamped variant -- a
    raw read would protect a name (e.g. ".sat200" for 2.0) the runtime never
    writes while sweeping away the ".sat150" it actually does."""
    video_path = os.path.join(gl.DATA_PATH, "over_saturated_video.mp4")
    _make_test_video(video_path)
    md5 = video_cache_sweeper._md5_of_file(video_path)

    fixtures.seed_page_with_background("OverSatPage", video_path)

    # One deck persisted at an out-of-range 2.0 (clamps to 1.5 -> ".sat150").
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    os.makedirs(decks_dir, exist_ok=True)
    with open(os.path.join(decks_dir, "OVERSATDECK.json"), "w") as f:
        json.dump({"display": {"saturation": 2.0}}, f)

    clamped = _seed_cache_file("keys_80x80", f"{md5}.sat150.mp4")  # what runtime writes
    raw = _seed_cache_file("keys_80x80", f"{md5}.sat200.mp4")      # never written

    video_cache_sweeper.sweep_stale_video_caches()

    assert os.path.isfile(clamped), (
        "the sweep must protect the CLAMPED saturation variant the runtime "
        "actually writes for an out-of-range persisted factor"
    )
    assert not os.path.isfile(raw), (
        "the raw out-of-range variant is never written by playback and must "
        "be swept, not protected"
    )

    print("PASS: out-of-range persisted saturation protects the clamped variant, not the raw one")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_video_cache_sweeper")
    fixtures._install_integration_globals()  # real SettingsManager + PageManagerBackend
    fixtures.seed_page("Main")

    check_plugin_settings_reference_protects_cache()
    check_live_registry_entry_protects_cache()
    check_stale_sat_variants_swept()
    check_out_of_range_saturation_protects_clamped_variant()

    print("PASS: scenario_video_cache_sweeper")


if __name__ == "__main__":
    main()
