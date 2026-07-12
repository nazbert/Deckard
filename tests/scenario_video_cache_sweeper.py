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
import time

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


def check_tmp_age_gate() -> None:
    """#67 ask: the `.tmp.` age gate (sweeper.py's `.tmp.` branch). A
    writer temp file YOUNGER than TMP_MAX_AGE_S may be a build in progress and
    must survive; one OLDER is a crash leftover and must be swept. The sweep
    keys purely on `.tmp.` in the name and the file's mtime -- assert the
    EXACT survivor/deletion split across the age boundary, not just
    non-deletion."""
    layout_dir = os.path.join(video_cache_sweeper.VID_CACHE, "keys_64x64")

    young = _seed_cache_file("keys_64x64", "deadbeef.tmp.mp4")
    old = _seed_cache_file("keys_64x64", "cafebabe.tmp.mp4")
    # Age `old` well past the 24h gate; leave `young` at its just-written
    # mtime (now). Determinism: the gate compares mtime to a fixed threshold,
    # not to wall-clock jitter -- push `old` a full hour past the boundary.
    old_mtime = time.time() - (video_cache_sweeper.TMP_MAX_AGE_S + 3600)
    os.utime(old, (old_mtime, old_mtime))

    video_cache_sweeper.sweep_stale_video_caches()

    assert os.path.isfile(young), (
        "a `.tmp.` writer file younger than the age gate may be a build in "
        "progress and must survive the sweep"
    )
    assert not os.path.isfile(old), (
        "a `.tmp.` writer file older than TMP_MAX_AGE_S is a crash leftover "
        "and must be swept"
    )
    # The layout dir itself must remain (only its stale member was removed).
    assert os.path.isdir(layout_dir)

    print("PASS: `.tmp.` age gate keeps in-progress temps, sweeps stale crash leftovers")


def check_legacy_dir_sweep_idempotent() -> None:
    """#67 ask: legacy-dir sweep idempotence. The two legacy top-level dirs
    (`single_key/`, `key: <n>/`) that the deleted key_video_cache.py's
    JPEG-per-frame format wrote are UNCONDITIONALLY dead -- removed even when
    the source video's hash is still referenced, because nothing can decode
    those frames anymore. Assert: (1) both are removed on first sweep even
    with a referenced stem, (2) a normal layout dir with the same referenced
    content survives (the legacy sweep is scoped to the two dir shapes only),
    (3) a SECOND sweep is a clean no-op (idempotent -- os.listdir stops
    finding them)."""
    # Reference a video so its hash is in `referenced`; the legacy dirs must
    # STILL be swept despite that (their frames are dead regardless).
    video_path = os.path.join(gl.DATA_PATH, "legacy_referenced_video.mp4")
    _make_test_video(video_path)
    md5 = video_cache_sweeper._md5_of_file(video_path)
    fixtures.seed_page_with_background("LegacyRefPage", video_path)

    # Legacy JPEG-frame layout: VID_CACHE/single_key/<stem>/<size>/<frame>.jpg
    # and VID_CACHE/key: 3/<stem>/... -- seed a couple of nested files each.
    legacy_single = _seed_cache_file(os.path.join("single_key", md5, "72x72"), "0.jpg")
    legacy_keyn = _seed_cache_file(os.path.join("key: 3", md5, "72x72", "3"), "0.jpg")

    # A CURRENT-format cache for the SAME referenced video in a normal layout
    # dir -- this must survive (the legacy sweep must not over-reach).
    survivor = _seed_cache_file("keys_72x72", f"{md5}.mp4")

    video_cache_sweeper.sweep_stale_video_caches()

    assert not os.path.exists(os.path.join(video_cache_sweeper.VID_CACHE, "single_key")), (
        "the legacy `single_key/` dir must be swept unconditionally, even when "
        "its stem's hash is still referenced"
    )
    assert not os.path.exists(os.path.join(video_cache_sweeper.VID_CACHE, "key: 3")), (
        "the legacy `key: <n>/` dir must be swept unconditionally"
    )
    assert not os.path.isfile(legacy_single) and not os.path.isfile(legacy_keyn)
    assert os.path.isfile(survivor), (
        "a current-format cache of the SAME referenced video in a normal "
        "layout dir must survive -- the legacy sweep is scoped to the two "
        "legacy dir shapes only"
    )

    # Idempotence: a second sweep with the legacy dirs already gone must be a
    # clean no-op -- must not raise, and must not touch the survivor.
    video_cache_sweeper.sweep_stale_video_caches()
    assert os.path.isfile(survivor), "a second sweep must remain a no-op over the surviving current cache"

    print("PASS: legacy key-video dirs swept unconditionally, scoped, and idempotently")


def check_entry_name_parsing() -> None:
    """#67 ask: the `entry.split(".")[0]` hash parse and the per-branch name
    dispatch. Assert the EXACT split across every branch in one dir:
      - `.cache` legacy pickle -> swept (unreadable format).
      - `<hash>.satNNN.mp4` -> hash parsed from `split(".")[0]`, so a
        referenced video's CURRENT-suffix variant is kept while a stale-suffix
        one is swept -- isolating that the hash (not the whole filename) is
        what's matched against `referenced`.
      - a garbage/non-cache name (no recognized extension) -> left untouched
        (the sweep's final `else: continue`).
    """
    video_path = os.path.join(gl.DATA_PATH, "name_parse_video.mp4")
    _make_test_video(video_path)
    md5 = video_cache_sweeper._md5_of_file(video_path)
    fixtures.seed_page_with_background("NameParsePage", video_path)

    # A deck at exactly 1.15 -> ".sat115"; the stale-suffix cache below uses
    # ".sat110", which NO deck seeded by this scenario produces (earlier legs
    # leave decks at 1.3/1.5 on disk in the shared decks dir; the sweep reads
    # ALL of them). This isolates the parse contract from cross-leg deck
    # state: only the hash match + suffix membership decide kept vs swept.
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    os.makedirs(decks_dir, exist_ok=True)
    with open(os.path.join(decks_dir, "NAMEPARSEDECK.json"), "w") as f:
        json.dump({"display": {"saturation": 1.15}}, f)

    kept_default = _seed_cache_file("keys_96x96", f"{md5}.mp4")          # referenced, default suffix -> kept
    stale_sat = _seed_cache_file("keys_96x96", f"{md5}.sat110.mp4")     # referenced hash, no-deck suffix -> swept
    legacy_cache = _seed_cache_file("keys_96x96", f"{md5}.cache")       # legacy pickle -> swept
    garbage = _seed_cache_file("keys_96x96", "README.txt")             # unrecognized -> untouched

    video_cache_sweeper.sweep_stale_video_caches()

    assert os.path.isfile(kept_default), (
        "the referenced video's default-suffix cache must be kept (hash "
        "parsed from split('.')[0] matched `referenced`)"
    )
    assert not os.path.isfile(stale_sat), (
        "a `<hash>.satNNN.mp4` whose hash IS referenced but whose suffix no "
        "deck's current factor produces must be swept -- proving the parse "
        "matches the hash, not the whole filename"
    )
    assert not os.path.isfile(legacy_cache), "a legacy `.cache` pickle must be swept (unreadable format)"
    assert os.path.isfile(garbage), (
        "a name matching no cache-file branch must be left untouched (the "
        "sweep's final `else: continue`)"
    )

    print("PASS: entry-name parse dispatches each branch and matches the hash, not the filename")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_video_cache_sweeper")
    fixtures._install_integration_globals()  # real SettingsManager + PageManagerBackend
    fixtures.seed_page("Main")

    check_plugin_settings_reference_protects_cache()
    check_live_registry_entry_protects_cache()
    check_stale_sat_variants_swept()
    check_out_of_range_saturation_protects_clamped_variant()
    check_tmp_age_gate()
    check_legacy_dir_sweep_idempotent()
    check_entry_name_parsing()

    print("PASS: scenario_video_cache_sweeper")


if __name__ == "__main__":
    main()
