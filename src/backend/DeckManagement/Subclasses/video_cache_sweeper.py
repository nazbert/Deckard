"""Startup sweep of the video cache directory.

Cache entries are keyed by the md5 of the source video, so entries for
videos no longer referenced by any deck settings or page become unreachable
garbage the moment the user picks a different file. This sweep removes them,
along with legacy pickle caches (pre canvas-mp4 format) and abandoned
writer temp files.
"""
import hashlib
import os
import shutil
import time

from loguru import logger as log

import globals as gl
from src.backend.DeckManagement.HelperMethods import is_video

VID_CACHE = os.path.join(gl.DATA_PATH, "cache", "videos")

# A .tmp.mp4 younger than this may be a build in progress; older ones are
# leftovers from a crash.
TMP_MAX_AGE_S = 24 * 60 * 60


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
    # Same hashing as BackgroundVideoCache/VideoFrameCache so keys match.
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


@log.catch
def sweep_stale_video_caches(startup_delay: float = 0.0) -> None:
    if startup_delay:
        time.sleep(startup_delay)
    if not os.path.isdir(VID_CACHE):
        return

    referenced = collect_referenced_video_hashes()
    freed = 0
    removed = 0

    for layout in os.listdir(VID_CACHE):
        layout_dir = os.path.join(VID_CACHE, layout)
        if not os.path.isdir(layout_dir):
            continue
        for entry in os.listdir(layout_dir):
            entry_path = os.path.join(layout_dir, entry)
            entry_hash = entry.split(".")[0]

            try:
                if os.path.isdir(entry_path):
                    # single_key/<md5>/ frame directories
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
                    if entry_hash in referenced:
                        continue
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
