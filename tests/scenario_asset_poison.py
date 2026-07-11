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
  3. AssetManagerBackend.add() survives every poison file (unreadable ->
     warning + None; corrupt video -> asset added with an existing
     thumbnail path), and a VALID asset added AFTER the poison batch still
     lands -- one poison file must not block the batch.
  4. fill_missing_data() survives a null-thumbnail entry (poison left by a
     previously failed run) and a corrupt-video entry with a missing
     thumbnail, and a full backend re-init over the poisoned json (the app
     startup path, main.py) does not raise.

The GTK half (Preview.set_image broken-image marker, Chooser.build()'s
guaranteed set_loading(False)) is not scenario-testable headless -- see the
MR's manual test steps.
"""
import fixtures  # noqa: F401  (must be first -- see fixtures.py docstring)

import os

import globals as gl
from PIL import Image

from src.backend.MediaManager import MediaManager
from src.backend.DeckManagement.HelperMethods import sha256

gl.media_manager = MediaManager()

from src.backend.AssetManagerBackend import AssetManagerBackend  # noqa: E402


POISON_DIR = os.path.join(gl.DATA_PATH, "poison")


def write_bytes(name: str, data: bytes) -> str:
    os.makedirs(POISON_DIR, exist_ok=True)
    path = os.path.join(POISON_DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


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


def check_backend_add_batch_continues(files: dict) -> AssetManagerBackend:
    backend = AssetManagerBackend()
    gl.asset_manager_backend = backend

    # Corrupt image: no decode happens at add() time -- must land as an asset.
    garbage_id = backend.add(files["garbage_png"])  # must not raise
    assert garbage_id is not None, "corrupt png must still be importable (decode fails only at preview time)"

    # 0-byte video: add() runs save_thumbnail -> generate_thumbnail; must not
    # raise, must fall back to an EXISTING path for the thumbnail.
    empty_mp4_id = backend.add(files["empty_mp4"])
    assert empty_mp4_id is not None, "0-byte mp4 must not abort the import"
    empty_mp4_asset = backend.get_by_id(empty_mp4_id)
    assert empty_mp4_asset["thumbnail"] is not None
    assert os.path.exists(empty_mp4_asset["thumbnail"]), (
        "the fallback thumbnail path must exist on disk (Preview marks it broken)"
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


def check_fill_missing_and_reinit(backend: AssetManagerBackend, files: dict) -> None:
    # Simulate poison left by a previously failed run: a null thumbnail
    # (os.path.exists(None) used to TypeError out of __init__) and a corrupt
    # video whose thumbnail file went missing.
    assert len(backend) > 0
    backend[0]["thumbnail"] = None

    empty_mp4_asset = backend.get_by_sha256(sha256(files["empty_mp4"]))
    if empty_mp4_asset is not None:
        empty_mp4_asset["thumbnail"] = os.path.join(POISON_DIR, "does_not_exist.png")

    backend.fill_missing_data()  # must not raise
    for asset in backend:
        assert asset.get("thumbnail") is not None, (
            f"fill_missing_data must repair null thumbnails ({asset.get('internal-path')})"
        )
    backend.save_json()

    # App-startup path (main.py): a fresh backend over the poisoned json runs
    # load_json + fill_missing_data + remove_invalid_data in __init__.
    reborn = AssetManagerBackend()  # must not raise
    assert len(reborn) == len(backend), (
        f"re-init must keep all {len(backend)} assets, got {len(reborn)}"
    )
    print("ok: fill_missing_data + backend re-init survive poisoned entries")


def main() -> None:
    files = make_poison_files()
    check_generate_thumbnail_never_raises(files)
    check_get_thumbnail_no_cache_poison(files)
    backend = check_backend_add_batch_continues(files)
    check_fill_missing_and_reinit(backend, files)

    # Restore perms so fixtures' atexit rmtree can clean the temp dir.
    if files["unreadable_png"] is not None:
        os.chmod(files["unreadable_png"], 0o644)


if __name__ == "__main__":
    fixtures.start_watchdog(60, label="asset_poison")
    main()
    print("PASS")
