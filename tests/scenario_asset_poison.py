"""
Poison-file resilience for the asset import -> thumbnail backend (#112).

An unreadable or corrupt imported file used to raise out of
MediaManager.generate_thumbnail / get_thumbnail and
AssetManagerBackend.add / fill_missing_thumbnails, killing the import
worker thread (and, via fill_missing_thumbnails in __init__, app startup)
and leaving the Custom Assets page spinning forever.

This scenario proves, headless (no GTK widgets are built):

  1. generate_thumbnail never raises for any poison shape -- garbage .png,
     0-byte .png, truncated-but-valid-header .png (the lazy Image.open
     case), garbage .gif, garbage .svg, 0-byte .mp4, garbage .mp4, and a
     chmod-000 unreadable file -- and returns the tagged fallback image.
  2. get_thumbnail never raises AND never persists the fallback into the
     thumbnail cache (a corrupt file must stay retryable), while a valid
     file still gets cached.
  3. (rev1) A poisoned CACHE entry must not wedge a VALID source file:
     get_thumbnail drops the bad entry and regenerates from source.
  4. AssetManagerBackend.add() survives every poison file (unreadable ->
     warning + None; corrupt video -> asset added with thumbnail None),
     and a VALID asset added AFTER the poison batch still lands -- one
     poison file must not block the batch.
  5. (rev1) A failed thumbnail generation is RETRYABLE: once the source
     becomes valid, the next fill_missing_data produces a real thumbnail
     (the old fallback-to-asset-path wedged it forever).
  6. fill_missing_data() + remove_invalid_data() + a full backend re-init
     over a poisoned json (null thumbnail, null internal-path -- the app
     startup path) do not raise; the null-internal-path entry is dropped.
  7. (rev1) copy_asset failures (read-only Assets dir) fail soft: add()
     returns None instead of killing the import worker thread.

The GTK half (Preview.set_image broken-image marker, Chooser.build()'s
guaranteed set_loading(False)) is not scenario-testable headless -- see the
MR's manual test steps.
"""
import fixtures  # noqa: F401  (must be first -- see fixtures.py docstring)

import os
import shutil
import tempfile

import cv2
import numpy as np

import globals as gl
from PIL import Image

from src.backend.MediaManager import MediaManager
from src.backend.DeckManagement.HelperMethods import sha256

gl.media_manager = MediaManager()

from src.backend.AssetManagerBackend import AssetManagerBackend  # noqa: E402


POISON_DIR = os.path.join(gl.DATA_PATH, "poison")
INTERNAL_ASSETS_DIR = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets")


def write_bytes(name: str, data: bytes) -> str:
    os.makedirs(POISON_DIR, exist_ok=True)
    path = os.path.join(POISON_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def make_test_video(path: str, n_frames: int = 10, size=(64, 48), fps: int = 10) -> None:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    for i in range(n_frames):
        frame[:, :] = (i % 255, 60, 120)
        writer.write(frame)
    writer.release()


def make_poison_files() -> dict:
    files = {
        "garbage_png": write_bytes("garbage.png", b"garbage\n"),
        "empty_png": write_bytes("empty.png", b""),
        "garbage_gif": write_bytes("garbage.gif", b"not a gif at all"),
        "garbage_svg": write_bytes("garbage.svg", b"<not-svg>garbage</not-svg>"),
        "empty_mp4": write_bytes("empty.mp4", b""),
        "garbage_mp4": write_bytes("garbage.mp4", b"\x00\x01\x02\x03 definitely not ffmpeg-decodable"),
    }

    # Truncated png: valid magic + IHDR so Image.open() succeeds, but the
    # pixel data is cut off -- the decode only fails at .load() time. This is
    # exactly the lazy-decode hole generate_thumbnail's forced .load() closes.
    valid_tmp = os.path.join(POISON_DIR, "_tmp_full.png")
    fixtures.make_test_png(valid_tmp, size=(128, 128))
    with open(valid_tmp, "rb") as f:
        full = f.read()
    files["truncated_png"] = write_bytes("truncated.png", full[: max(64, len(full) // 2)])
    os.remove(valid_tmp)

    # Unreadable file: valid content, no read permission.
    unreadable = fixtures.make_test_png(os.path.join(POISON_DIR, "unreadable.png"), size=(32, 32))
    os.chmod(unreadable, 0o000)
    if os.access(unreadable, os.R_OK):
        # e.g. running as root -- permissions don't bite; drop the case.
        os.chmod(unreadable, 0o644)
        os.remove(unreadable)
        unreadable = None
    files["unreadable_png"] = unreadable

    return files


def check_generate_thumbnail_never_raises(files: dict) -> None:
    for name, path in files.items():
        if path is None:
            continue
        thumb = gl.media_manager.generate_thumbnail(path)  # must not raise
        assert isinstance(thumb, Image.Image), (
            f"{name}: generate_thumbnail must return a PIL image, got {type(thumb)}"
        )
        assert thumb.info.get("sc_broken"), (
            f"{name}: expected the tagged fallback for an undecodable file"
        )
    print("ok: generate_thumbnail survives every poison shape")


def check_get_thumbnail_no_cache_poison(files: dict) -> None:
    cache_dir = os.path.join(gl.DATA_PATH, "cache", "thumbnails")

    # Poison file: image back, nothing cached.
    path = files["garbage_png"]
    thumb = gl.media_manager.get_thumbnail(path)  # must not raise
    assert isinstance(thumb, Image.Image)
    poison_cache = os.path.join(cache_dir, f"{sha256(path)}.png")
    assert not os.path.exists(poison_cache), (
        "the broken-image fallback must never be persisted into the "
        "thumbnail cache (the file must stay retryable)"
    )

    # Unreadable file: sha256() itself raises inside get_thumbnail -- still
    # must come back as an image.
    if files["unreadable_png"] is not None:
        thumb = gl.media_manager.get_thumbnail(files["unreadable_png"])
        assert isinstance(thumb, Image.Image)

    # Valid file: still decoded AND cached (the guard must not break the
    # happy path).
    valid = fixtures.make_test_png(os.path.join(POISON_DIR, "valid_cache_probe.png"), size=(64, 64))
    thumb = gl.media_manager.get_thumbnail(valid)
    assert isinstance(thumb, Image.Image)
    assert not thumb.info.get("sc_broken"), "valid file must not come back as fallback"
    assert os.path.exists(os.path.join(cache_dir, f"{sha256(valid)}.png")), (
        "valid files must still be cached"
    )
    print("ok: get_thumbnail falls back without caching poison, still caches valid files")


def check_cache_poison_recovery() -> None:
    """rev1 review finding 1: a poisoned/truncated FS-cache entry (e.g. left
    by a crash mid-write) must not permanently wedge a VALID source file to
    the broken placeholder -- get_thumbnail must drop the bad entry and
    regenerate from the source."""
    cache_dir = os.path.join(gl.DATA_PATH, "cache", "thumbnails")

    valid = fixtures.make_test_png(os.path.join(POISON_DIR, "cache_recovery_probe.png"), size=(96, 96))
    cache_path = os.path.join(cache_dir, f"{sha256(valid)}.png")

    # Prime the cache, then poison the cached entry (undecodable garbage --
    # what a crash/disk-full mid-save used to manufacture).
    first = gl.media_manager.get_thumbnail(valid)
    assert not first.info.get("sc_broken")
    assert os.path.exists(cache_path)
    with open(cache_path, "wb") as f:
        f.write(b"poisoned cache entry")

    # Next call must return the REAL thumbnail, not the placeholder...
    second = gl.media_manager.get_thumbnail(valid)
    assert isinstance(second, Image.Image)
    assert not second.info.get("sc_broken"), (
        "a poisoned cache entry must not wedge a valid source file to the "
        "broken placeholder -- it must be dropped and regenerated"
    )
    # ...and the poison must be gone: the entry was re-written and decodes.
    with Image.open(cache_path) as img:
        img.load()

    # No half-written temp files may linger from the atomic save.
    leftovers = [n for n in os.listdir(cache_dir) if n.endswith(".tmp")]
    assert not leftovers, f"atomic save leaked temp files: {leftovers}"
    print("ok: poisoned cache entry is dropped and regenerated from source")


def check_backend_add_batch_continues(files: dict) -> AssetManagerBackend:
    backend = AssetManagerBackend()
    gl.asset_manager_backend = backend

    # Corrupt image: no decode happens at add() time -- must land as an asset.
    garbage_id = backend.add(files["garbage_png"])  # must not raise
    assert garbage_id is not None, "corrupt png must still be importable (decode fails only at preview time)"
    garbage_asset = backend.get_by_id(garbage_id)
    assert garbage_asset["internal-path"].startswith(gl.DATA_PATH), (
        "internal-path must never point outside the app data dir "
        "(remove_asset_by_id deletes whatever it points at)"
    )

    # 0-byte video: add() runs save_thumbnail -> generate_thumbnail; must not
    # raise. rev1: the thumbnail must be None (retryable on every boot), NOT
    # some existing path that fill_missing_thumbnails would skip forever.
    empty_mp4_id = backend.add(files["empty_mp4"])
    assert empty_mp4_id is not None, "0-byte mp4 must not abort the import"
    empty_mp4_asset = backend.get_by_id(empty_mp4_id)
    assert "thumbnail" in empty_mp4_asset
    assert empty_mp4_asset["thumbnail"] is None, (
        "a failed thumbnail generation must leave thumbnail=None so it is "
        "retried at the next boot (an existing path wedges it forever)"
    )

    # Truncated png with a valid header -- the lazy-decode case.
    truncated_id = backend.add(files["truncated_png"])
    assert truncated_id is not None

    # Unreadable file: must fail softly (None), not raise out of the worker.
    if files["unreadable_png"] is not None:
        unreadable_id = backend.add(files["unreadable_png"])
        assert unreadable_id is None, "unreadable file must be rejected with a warning, not imported"

    # THE point of #112: a valid asset AFTER the poison batch still lands.
    valid = fixtures.make_test_png(os.path.join(POISON_DIR, "valid_after_poison.png"), size=(48, 48))
    valid_id = backend.add(valid)
    assert valid_id is not None, "a poison file must not block later imports in the batch"
    valid_asset = backend.get_by_id(valid_id)
    assert os.path.exists(valid_asset["thumbnail"])
    assert valid_asset in backend.get_all()

    print("ok: backend.add survives poison files and the batch continues")
    return backend


def check_basename_collision_still_copied(backend: AssetManagerBackend) -> None:
    """rev1 review finding 3: the old `file_in_dir(basename, DATA_PATH/cache)`
    skip could leave internal-path pointing at the user's ORIGINAL file
    outside the app data dir -- which remove_asset_by_id() os.remove()s,
    deleting the user's source. A file whose basename collides with a
    top-level cache/ entry (here: the 'thumbnails' dir) must still be copied
    into the internal Assets dir."""
    outside_dir = tempfile.mkdtemp(prefix="sc_outside_")
    try:
        os.makedirs(os.path.join(gl.DATA_PATH, "cache", "thumbnails"), exist_ok=True)
        collision = os.path.join(outside_dir, "thumbnails")  # collides with cache/thumbnails
        Image.new("RGB", (16, 16), (0, 128, 0)).save(collision, format="PNG")

        asset_id = backend.add(collision)
        assert asset_id is not None
        asset = backend.get_by_id(asset_id)
        assert asset["internal-path"].startswith(INTERNAL_ASSETS_DIR + os.sep), (
            f"internal-path must live in the internal Assets dir, never at the "
            f"user's original file: {asset['internal-path']}"
        )
        assert os.path.exists(asset["internal-path"])
    finally:
        shutil.rmtree(outside_dir, ignore_errors=True)
    print("ok: basename collision with cache/ still copies into the Assets dir")


def check_broken_thumbnail_retry(backend: AssetManagerBackend) -> None:
    """rev1 review finding 2: a thumbnail generation that fails once (e.g.
    file still downloading, network mount hiccup) must be retried once the
    source is valid -- the old fallback returned an existing path, which
    fill_missing_thumbnails' exists-check then skipped at every boot."""
    transient = write_bytes("transient.mp4", b"still downloading, not yet a video")
    asset_id = backend.add(transient)
    assert asset_id is not None
    asset = backend.get_by_id(asset_id)
    assert asset["thumbnail"] is None, "generation failure must leave thumbnail unset"

    # The source becomes valid (download finished / mount back).
    make_test_video(asset["internal-path"])

    backend.fill_missing_data()  # the boot-time retry

    assert asset["thumbnail"] is not None, (
        "fill_missing_data must retry a previously failed thumbnail once "
        "the source is valid"
    )
    assert os.path.exists(asset["thumbnail"])
    with Image.open(asset["thumbnail"]) as img:
        img.load()  # must be a real, decodable thumbnail
    print("ok: transient thumbnail failure heals on the next fill_missing_data")


def check_fill_missing_and_reinit(backend: AssetManagerBackend, files: dict) -> None:
    # Null thumbnail on a VALID image asset (poison left by a previously
    # failed run; os.path.exists(None) used to TypeError out of __init__)
    # must be repaired...
    garbage_asset = backend.get_by_sha256(sha256(files["garbage_png"]))
    garbage_asset["thumbnail"] = None
    # ...while a STILL-corrupt video keeps thumbnail None (retryable), and is
    # not wedged to some existing bogus path.
    empty_mp4_asset = backend.get_by_sha256(sha256(files["empty_mp4"]))

    backend.fill_missing_data()  # must not raise

    assert garbage_asset["thumbnail"] is not None, (
        "fill_missing_data must repair a null thumbnail for a decodable-type asset"
    )
    assert empty_mp4_asset["thumbnail"] is None, (
        "a still-corrupt video must keep thumbnail=None (retryable), not get "
        "wedged to an existing path"
    )

    # Poison entry with a null internal-path: the FULL __init__ chain
    # (load_json -> fill_missing_data -> remove_invalid_data -- the app
    # startup path, main.py) must survive it and drop the entry.
    # remove_invalid_data used to TypeError on os.path.exists(None).
    n_valid = len(backend)
    backend.append({
        "name": "null-internal-path-poison",
        "original-path": None,
        "internal-path": None,
        "sha256": "0" * 64,
        "id": "00000000-0000-0000-0000-000000000000",
        "license": {"name": None, "url": None, "author": None},
        "thumbnail": None,
    })
    backend.save_json()

    reborn = AssetManagerBackend()  # must not raise
    assert len(reborn) == n_valid, (
        f"re-init must drop the null-internal-path entry and keep the "
        f"{n_valid} valid assets, got {len(reborn)}"
    )
    assert reborn.get_by_id("00000000-0000-0000-0000-000000000000") is None, (
        "the null-internal-path poison entry must be removed"
    )
    print("ok: fill_missing_data + remove_invalid_data + re-init survive poisoned entries")


def check_copy_failure_soft(backend: AssetManagerBackend) -> None:
    """rev1 review finding 5: copy_asset failures (dest permissions, disk
    full, file deleted between hash and copy) must fail soft with None, not
    raise out of the import worker thread."""
    os.makedirs(INTERNAL_ASSETS_DIR, exist_ok=True)
    probe = fixtures.make_test_png(os.path.join(POISON_DIR, "copy_failure_probe.png"), size=(24, 24))

    os.chmod(INTERNAL_ASSETS_DIR, 0o555)
    try:
        if os.access(INTERNAL_ASSETS_DIR, os.W_OK):
            print("skip: copy-failure check (dir stays writable, e.g. running as root)")
            return
        n_before = len(backend)
        asset_id = backend.add(probe)  # must not raise
        assert asset_id is None, "a failed copy must reject the asset with None"
        assert len(backend) == n_before, "a failed copy must not append a half-imported asset"
    finally:
        os.chmod(INTERNAL_ASSETS_DIR, 0o755)
    print("ok: copy failure fails soft without killing the import worker")


def main() -> None:
    files = make_poison_files()
    check_generate_thumbnail_never_raises(files)
    check_get_thumbnail_no_cache_poison(files)
    check_cache_poison_recovery()
    backend = check_backend_add_batch_continues(files)
    check_basename_collision_still_copied(backend)
    check_broken_thumbnail_retry(backend)
    check_fill_missing_and_reinit(backend, files)
    check_copy_failure_soft(backend)

    # Restore perms so fixtures' atexit rmtree can clean the temp dir.
    if files["unreadable_png"] is not None:
        os.chmod(files["unreadable_png"], 0o644)


if __name__ == "__main__":
    fixtures.start_watchdog(60, label="asset_poison")
    main()
    print("PASS")
