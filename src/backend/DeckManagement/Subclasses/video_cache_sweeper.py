"""Startup sweep of the video cache directory.

Cache entries are keyed by the md5 of the source video, so an entry for a
video that no deck settings and no page reference becomes unreachable garbage
the moment the user picks a different file. This sweep removes those, plus
legacy pickle caches and abandoned writer temp files.
"""
import hashlib
import math
import os
import re
import shutil
import time

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import registry_cache_paths, sat_suffix
from src.backend.PageManagement import page_flush

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")

# A .tmp.mp4 younger than this can be a build in progress. An older one is a
# leftover from a crash.
TMP_MAX_AGE_S = 24 * 60 * 60

# Valid saturation-factor range. It mirrors DeckController's UI scale
# (DeckGroup.Saturation min=1.0, max=1.5) and its runtime
# _read_display_saturation clamp. The sweep must produce the same suffix the
# runtime writes on disk. The runtime clamps a persisted out-of-range or
# non-finite factor before it derives the cache filename, so a raw read here
# protects a variant playback never writes, e.g. ".sat200" for a hand-edited
# 2.0, and sweeps away the ".sat150" it does write.
MIN_DISPLAY_SATURATION = 1.0
MAX_DISPLAY_SATURATION = 1.5
DEFAULT_DISPLAY_SATURATION = 1.0


def _clamp_saturation(raw) -> float:
    """Maps a persisted saturation to the factor the runtime applies.

    A non-numeric or non-finite value, that is NaN or inf, falls back to the
    default, and then this clamps the value to the MIN and MAX range. It
    matches DeckController._read_display_saturation, so the sweep and playback
    agree on the cache filename.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DISPLAY_SATURATION
    if not math.isfinite(value):
        return DEFAULT_DISPLAY_SATURATION
    return min(MAX_DISPLAY_SATURATION, max(MIN_DISPLAY_SATURATION, value))


# Current cache-file naming. A default-saturation file is "<md5>.mp4". It can
# carry a ".satNNN" baked-in saturation variant (mp4_tile_cache.sat_suffix)
# and a rendering variant, e.g. ".bounded" for KeyGIF's alpha-dropped
# over-budget artifact (mp4_tile_cache.acquire_from_frames). The sweep matches
# on the saturation group. It accepts the rendering variant, so it sweeps a
# ".satNNN.bounded.mp4" with the factor that file belongs to instead of
# falling through as an unrecognized name and protecting it forever. Anything
# else in a layout dir is legacy or a writer temp file, and the other sweep
# branches handle it.
_MP4_NAME_RE = re.compile(
    r"^(?P<hash>[0-9a-f]+)(?P<sat>\.sat\d+)?(?P<variant>\.[a-z]+)?\.mp4$")

# Top-level directory names the deleted key_video_cache.py JPEG-per-frame
# format wrote into. Those were VID_CACHE/single_key/<stem>/<size>/<frame>.jpg
# and VID_CACHE/key: <n>/<stem>/<size>/<n>/<frame>.jpg, written by
# key_video_cache.write_cache, which is removed. No code can read this format.
_LEGACY_KEY_DIR_RE = re.compile(r"^key: \d+$")


def _is_legacy_key_video_dir(name: str) -> bool:
    return name == "single_key" or bool(_LEGACY_KEY_DIR_RE.match(name))


def _sweep_legacy_key_video_dirs() -> None:
    """One-shot migration cleanup.

    Every entry under the two legacy top-level directories above is dead,
    because key_video_cache.py is gone. Unlike sweep_stale_video_caches below,
    this bypasses the referenced-hash check. A still-referenced video's old
    JPEG frames are as unreachable as an unreferenced one's, because nothing
    decodes them again either way. It is idempotent. Once removed, os.listdir
    stops finding them on every later startup.
    """
    if not os.path.isdir(VID_CACHE):
        return
    freed = 0
    removed = 0
    for name in os.listdir(VID_CACHE):
        if not _is_legacy_key_video_dir(name):
            continue
        path = os.path.join(VID_CACHE, name)
        if not os.path.isdir(path):
            continue
        try:
            size = sum(
                os.path.getsize(os.path.join(root, fname))
                for root, _, files in os.walk(path) for fname in files
            )
            shutil.rmtree(path)
        except OSError:
            log.opt(exception=True).warning(f"Could not remove legacy key-video cache dir {path}")
            continue
        freed += size
        removed += 1
    if removed:
        log.success(f"Removed {removed} legacy key-video cache directories ({freed / 1e6:.1f} MB)")


def _collect_json_paths() -> list[str]:
    paths: list[str] = []
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    if os.path.isdir(decks_dir):
        paths.extend(
            os.path.join(decks_dir, name)
            for name in os.listdir(decks_dir) if name.endswith(".json")
        )
    # This includes plugin-registered custom pages. The sweep thread starts
    # after create_global_objects(), so the page manager is set by the time
    # this runs. If it is not, the reference set is incomplete and the sweep
    # deletes live caches, so abort instead. sweep_stale_video_caches carries
    # @log.catch, so the raise skips the sweep.
    page_manager = gl.page_manager
    if page_manager is None:
        raise RuntimeError(
            "video cache sweep started before the page manager exists -- "
            "refusing to sweep against an incomplete reference set")
    paths.extend(page_manager.get_pages(add_custom_pages=True, sort=False))
    # Plugins keep their own settings JSONs at PluginBase.settings_path, that
    # is settings/plugins/<id>/settings.json, and can reference media there
    # that appears in no deck or page file. Scan them, or the sweep deletes
    # caches whose in-process registry entries are live and marked ready.
    plugins_dir = os.path.join(gl.DATA_PATH, "settings", "plugins")
    if os.path.isdir(plugins_dir):
        for root, _, files in os.walk(plugins_dir):
            paths.extend(
                os.path.join(root, name) for name in files if name.endswith(".json")
            )
    return paths


def _walk_for_video_paths(node, found: set[str]) -> None:
    """Any string anywhere in the JSON that points at an existing video file
    counts as a reference. Media appears as a deck or page background, a
    screensaver, or per-key and per-dial media, and this survives a change in
    the JSON structure."""
    if isinstance(node, dict):
        for value in node.values():
            _walk_for_video_paths(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_for_video_paths(value, found)
    elif isinstance(node, str):
        if is_video(node):
            found.add(node)


def _md5_of_file(path: str) -> str:
    # Same hashing as BackgroundVideoCache and KeyVideoCache, so keys match.
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        while block := f.read(2 ** 16):
            md5.update(block)
    return md5.hexdigest()


def collect_referenced_video_hashes() -> set[str]:
    video_paths: set[str] = set()
    # Read barrier before this reads every page file. The sweep deletes what
    # it does not find, so a page whose new background video still sits on the
    # write debounce loses that video's cache. Use flush_all and not a
    # per-path call, because the scan reads the whole page set, and only page
    # files are ever pending.
    page_flush.get().flush_all()
    for json_path in _collect_json_paths():
        try:
            _walk_for_video_paths(gl.settings_manager.load_settings_from_file(json_path), video_paths)
        except Exception:
            log.opt(exception=True).warning(f"Could not scan {json_path} for video references")

    hashes = set()
    for path in video_paths:
        try:
            hashes.add(_md5_of_file(path))
        except OSError:
            pass
    return hashes


def collect_active_sat_suffixes() -> set[str]:
    """Cache-filename suffixes that some deck's current display.saturation can
    still produce, plus the default empty suffix.

    The unsuffixed cache is the upstream-format file, and it becomes live
    again the moment a deck resets to 1.0. Any other .satNNN variant of a
    referenced video is a leftover from a factor tried and abandoned, which is
    bounded but permanent disk growth unless the sweep removes it.
    """
    suffixes = {""}
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    if not os.path.isdir(decks_dir):
        return suffixes
    for name in os.listdir(decks_dir):
        if not name.endswith(".json"):
            continue
        try:
            settings = gl.settings_manager.load_settings_from_file(
                os.path.join(decks_dir, name)
            ) or {}
            raw = settings.get("display", {}).get("saturation", 1.0)
            # Clamp the persisted factor exactly as the runtime clamps it, so
            # the suffix collected here is the one playback writes. An
            # out-of-range or hand-edited value then cannot make the sweep
            # protect a variant name the runtime never produces.
            suffixes.add(sat_suffix(_clamp_saturation(raw)))
        except Exception:
            # An unreadable deck file contributes nothing. The sweep can then
            # wrongly remove its variant, but a reader that finds its ready
            # cache missing invalidates the registry entry and rebuilds (see
            # mp4_tile_cache._maybe_adopt_shared_cache).
            log.opt(exception=True).warning(f"Could not read display saturation from {name}")
    return suffixes


@log.catch
def sweep_stale_video_caches(startup_delay: float = 0.0) -> None:
    if startup_delay:
        time.sleep(startup_delay)
    if not os.path.isdir(VID_CACHE):
        return

    _sweep_legacy_key_video_dirs()

    referenced = collect_referenced_video_hashes()
    active_sat_suffixes = collect_active_sat_suffixes()
    # Never delete a file a live in-process cache reader or builder is
    # attached to. The reference scan can miss a source, e.g. a source file
    # deleted since acquire, or a settings format it cannot parse. An attached
    # consumer is direct proof of use.
    protected_paths = registry_cache_paths()
    freed = 0
    removed = 0

    for layout in os.listdir(VID_CACHE):
        if _is_legacy_key_video_dir(layout):
            # The unconditional pass above already handled this. Skip it, so a
            # leftover entry from a failed rmtree there does not fall into the
            # referenced-hash check below.
            continue
        layout_dir = os.path.join(VID_CACHE, layout)
        if not os.path.isdir(layout_dir):
            continue
        for entry in os.listdir(layout_dir):
            entry_path = os.path.join(layout_dir, entry)
            entry_hash = entry.split(".")[0]

            try:
                if os.path.isdir(entry_path):
                    # A guard. No current cache format nests a directory
                    # inside a layout dir. The unconditional pass above
                    # handles the legacy single_key and key directories
                    # before this loop sees them.
                    if entry_hash in referenced:
                        continue
                    size = sum(
                        os.path.getsize(os.path.join(root, name))
                        for root, _, names in os.walk(entry_path) for name in names
                    )
                    shutil.rmtree(entry_path)
                elif ".tmp." in entry:
                    if time.time() - os.path.getmtime(entry_path) < TMP_MAX_AGE_S:
                        continue
                    size = os.path.getsize(entry_path)
                    os.remove(entry_path)
                elif entry.endswith(".cache"):
                    # Legacy pickle format. Current code cannot read it.
                    size = os.path.getsize(entry_path)
                    os.remove(entry_path)
                elif entry.endswith(".mp4"):
                    if entry_path in protected_paths:
                        continue
                    if entry_hash in referenced:
                        match = _MP4_NAME_RE.match(entry)
                        suffix = (match.group("sat") or "") if match else ""
                        if suffix in active_sat_suffixes:
                            continue
                        # The video is referenced, but no deck's current
                        # factor produces this saturation variant. Fall
                        # through and sweep it.
                    size = os.path.getsize(entry_path)
                    os.remove(entry_path)
                else:
                    continue
            except OSError:
                log.opt(exception=True).warning(f"Could not sweep video cache entry {entry_path}")
                continue

            freed += size
            removed += 1

    if removed:
        log.success(f"Video cache sweep removed {removed} stale entries ({freed / 1e6:.1f} MB)")
