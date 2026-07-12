"""Startup sweep of the video cache directory.

Cache entries are keyed by the md5 of the source video, so entries for
videos no longer referenced by any deck settings or page become unreachable
garbage the moment the user picks a different file. This sweep removes them,
along with legacy pickle caches (pre canvas-mp4 format) and abandoned
writer temp files.
"""
import hashlib
import os
import re
import shutil
import time

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.HelperMethods import is_video
from src.backend.DeckManagement.Subclasses.mp4_tile_cache import registry_cache_paths, sat_suffix

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")

# A .tmp.mp4 younger than this may be a build in progress; older ones are
# leftovers from a crash.
TMP_MAX_AGE_S = 24 * 60 * 60

# Current cache-file naming: "<md5>.mp4" (default saturation) or
# "<md5>.satNNN.mp4" (a baked-in saturation variant, see
# mp4_tile_cache.sat_suffix). Anything else in a layout dir is legacy or a
# writer temp file and is handled by the other sweep branches.
_MP4_NAME_RE = re.compile(r"^(?P<hash>[0-9a-f]+)(?P<sat>\.sat\d+)?\.mp4$")

# Top-level directory names the deleted key_video_cache.py's JPEG-per-frame
# format wrote into: VID_CACHE/single_key/<stem>/<size>/<frame>.jpg and
# VID_CACHE/key: <n>/<stem>/<size>/<n>/<frame>.jpg (key_video_cache.py:
# write_cache, now removed). No code can read this format anymore.
_LEGACY_KEY_DIR_RE = re.compile(r"^key: \d+$")


def _is_legacy_key_video_dir(name: str) -> bool:
    return name == "single_key" or bool(_LEGACY_KEY_DIR_RE.match(name))


def _sweep_legacy_key_video_dirs() -> None:
    """One-shot migration cleanup (docs/memory-footprint-impl-plan.md P2.2):
    every entry under the two legacy top-level directories above is
    unconditionally dead now that key_video_cache.py is gone -- unlike
    `sweep_stale_video_caches` below, this bypasses the referenced-hash
    check entirely, since a still-referenced video's old JPEG frames are
    exactly as unreachable as an unreferenced one's (nothing will ever
    decode them again either way). Idempotent: once removed, `os.listdir`
    simply stops finding them on every later startup."""
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
    paths = []
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    if os.path.isdir(decks_dir):
        paths.extend(
            os.path.join(decks_dir, name)
            for name in os.listdir(decks_dir) if name.endswith(".json")
        )
    # Includes plugin-registered custom pages.
    paths.extend(gl.page_manager.get_pages(add_custom_pages=True, sort=False))
    # Plugins keep their own settings JSONs (PluginBase.settings_path:
    # settings/plugins/<id>/settings.json) and may reference media there
    # that appears in no deck or page file. Missing these used to delete
    # caches whose in-process registry entries were live and marked ready.
    plugins_dir = os.path.join(gl.DATA_PATH, "settings", "plugins")
    if os.path.isdir(plugins_dir):
        for root, _, files in os.walk(plugins_dir):
            paths.extend(
                os.path.join(root, name) for name in files if name.endswith(".json")
            )
    return paths


def _walk_for_video_paths(node, found: set) -> None:
    """Any string anywhere in the JSON that points at an existing video file
    counts as a reference — media can appear as deck/page backgrounds,
    screensavers, or per-key/dial media, and this survives structure drift."""
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
    # Same hashing as BackgroundVideoCache/KeyVideoCache so keys match.
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        while block := f.read(2 ** 16):
            md5.update(block)
    return md5.hexdigest()


def collect_referenced_video_hashes() -> set[str]:
    video_paths = set()
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
    """Cache-filename suffixes some deck's CURRENT display.saturation can
    still produce, plus the default "" (the unsuffixed cache is the
    upstream-format file and becomes live again the moment a deck resets to
    1.0). Any other .satNNN variant of a referenced video is a leftover
    from a factor tried and abandoned -- bounded but permanent disk growth
    unless swept. An unreadable deck file contributes nothing (its variant
    may be wrongly swept, but a reader that then finds its ready cache
    missing invalidates the registry entry and rebuilds -- see
    mp4_tile_cache._maybe_adopt_shared_cache)."""
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
            suffixes.add(sat_suffix(float(settings.get("display", {}).get("saturation", 1.0))))
        except Exception:
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
    # Never delete a file a live in-process cache reader/builder is attached
    # to: the reference scan can miss sources (source file deleted since
    # acquire, settings formats it can't parse), but an attached consumer is
    # direct proof of use.
    protected_paths = registry_cache_paths()
    freed = 0
    removed = 0

    for layout in os.listdir(VID_CACHE):
        if _is_legacy_key_video_dir(layout):
            # Already handled unconditionally above; skip so a leftover
            # entry from a failed rmtree there doesn't fall into the
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
                    # Defensive: no current cache format nests a directory
                    # inside a layout dir (the legacy single_key/key: <n>
                    # directories that used to are handled unconditionally
                    # above, before this loop ever sees them).
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
                    # Legacy pickle format — unreadable by current code.
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
                        # Referenced video, but a saturation variant no
                        # deck's current factor produces (issue #53 item 8):
                        # fall through and sweep it.
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
