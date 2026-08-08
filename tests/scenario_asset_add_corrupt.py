"""
Corrupt files are refused at asset import time (gl#197).

Upstream gates add() on an exception-based is_decodable; our
generate_thumbnail never raises (#112 -- it returns a placeholder tagged
`sc_broken` on any decode failure), so a straight port would be a silent
always-True. The adapted gate (_decode_for_import) must key off the
sc_broken marker, run BEFORE the copy (no partial import), and surface as
the same None sentinel the existing add() failure paths use so
add_custom_media_set_by_ui can raise its AlertDialog.

Contract under test:
  (a) a truncated PNG (valid magic, cut body -- the lazy-decode shape) and
      a garbage-bytes .mp4 are refused: add() returns None, nothing is
      appended, no file lands in the internal Assets dir, Assets.json is
      unchanged;
  (b) a valid PNG still adds (gate must not break the happy path), and a
      valid video decodes exactly ONCE per add (the gate's decode is reused
      for the thumbnail, not repeated);
  (c) _decode_for_import is keyed off the sc_broken marker directly -- a
      tagged placeholder means None, an untagged image is returned,
      regardless of exceptions;
  (d) add_custom_media_set_by_ui shows an AlertDialog on the refusal (the
      drop must not silently do nothing), with the corrupt case covered in
      the dialog text.

Deliberately ABSENT (transient-failure policy, restated from the plan):
no startup pass deletes undecodable files -- an asset on a not-yet-mounted
dir must never cost the user their files. Pinned in scenario_asset_poison.
"""
import fixtures  # noqa: F401  (must be first -- see fixtures.py docstring)

import json
import os
import types

import cv2
import numpy as np

import globals as gl
from PIL import Image

from src.backend.MediaManager import MediaManager

gl.media_manager = MediaManager()

import src.backend.AssetManagerBackend as amb_mod  # noqa: E402
from src.backend.AssetManagerBackend import AssetManagerBackend  # noqa: E402


WORK_DIR = os.path.join(gl.DATA_PATH, "incoming")
INTERNAL_ASSETS_DIR = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets")


def make_corrupt_files() -> dict:
    os.makedirs(WORK_DIR, exist_ok=True)

    # Truncated png: valid magic + IHDR so Image.open() succeeds, but the
    # pixel data is cut -- decode only fails at .load() time.
    full = fixtures.make_test_png(os.path.join(WORK_DIR, "_full.png"), size=(128, 128))
    with open(full, "rb") as f:
        data = f.read()
    os.remove(full)
    truncated = os.path.join(WORK_DIR, "truncated.png")
    with open(truncated, "wb") as f:
        f.write(data[: max(64, len(data) // 2)])

    garbage_mp4 = os.path.join(WORK_DIR, "garbage.mp4")
    with open(garbage_mp4, "wb") as f:
        f.write(b"\x00\x01\x02\x03 definitely not ffmpeg-decodable")

    return {"truncated_png": truncated, "garbage_mp4": garbage_mp4}


def _library_state(backend: AssetManagerBackend) -> tuple[list, list[str]]:
    files = sorted(os.listdir(INTERNAL_ASSETS_DIR)) if os.path.isdir(INTERNAL_ASSETS_DIR) else []
    with open(backend.JSON_PATH) as f:
        return json.load(f), files


def check_corrupt_refused_no_partial_copy(backend: AssetManagerBackend, files: dict) -> None:
    for name, path in files.items():
        json_before, files_before = _library_state(backend)
        n_before = len(backend)

        asset_id = backend.add(path)  # must not raise

        assert asset_id is None, f"{name}: undecodable file must be refused with None, got {asset_id!r}"
        assert len(backend) == n_before, f"{name}: refused file must not append an asset"
        json_after, files_after = _library_state(backend)
        assert json_after == json_before, f"{name}: Assets.json changed over a refused add"
        assert files_after == files_before, (
            f"{name}: partial copy left in the Assets dir: "
            f"{sorted(set(files_after) - set(files_before))}"
        )
    print("ok: corrupt files are refused before the copy, library untouched")


def check_valid_still_adds(backend: AssetManagerBackend) -> None:
    valid = fixtures.make_test_png(os.path.join(WORK_DIR, "valid.png"), size=(64, 64))
    asset_id = backend.add(valid)
    assert asset_id is not None, "the gate must not refuse a valid image"
    asset = backend.get_by_id(asset_id)
    assert os.path.isfile(asset["internal-path"])
    assert asset["internal-path"].startswith(INTERNAL_ASSETS_DIR + os.sep)
    print("ok: a valid PNG still adds through the gate")


def check_decode_for_import_keys_off_sc_broken(backend: AssetManagerBackend) -> None:
    """The required adaptation (#112): the gate must read the sc_broken
    marker, not rely on exceptions -- generate_thumbnail never raises."""
    real_generate = gl.media_manager.generate_thumbnail
    try:
        broken = Image.new("RGB", (8, 8))
        broken.info["sc_broken"] = True
        gl.media_manager.generate_thumbnail = lambda path: broken
        assert backend._decode_for_import("whatever.png") is None, (
            "a tagged placeholder must read as not decodable"
        )

        ok = Image.new("RGB", (8, 8))
        gl.media_manager.generate_thumbnail = lambda path: ok
        assert backend._decode_for_import("whatever.png") is ok, (
            "an untagged image must read as decodable and be RETURNED, so "
            "add() can reuse it instead of decoding a second time"
        )
    finally:
        gl.media_manager.generate_thumbnail = real_generate
    print("ok: _decode_for_import keys off the sc_broken marker directly")


def make_test_video(path: str, n_frames: int = 8, size=(48, 32), fps: int = 10) -> None:
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    assert writer.isOpened(), f"could not open test video writer for {path}"
    frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    for i in range(n_frames):
        frame[:, :] = (i % 255, 60, 120)
        writer.write(frame)
    writer.release()


def check_video_add_decodes_once(backend: AssetManagerBackend) -> None:
    """#197 cost contract: the gate's decode IS the thumbnail decode -- a
    video add must run generate_thumbnail exactly once, not once for the
    gate and again inside save_thumbnail."""
    video = os.path.join(WORK_DIR, "decode_once.mp4")
    make_test_video(video)

    real_generate = gl.media_manager.generate_thumbnail
    calls: list[str] = []

    def counting_generate(path):
        calls.append(path)
        return real_generate(path)

    gl.media_manager.generate_thumbnail = counting_generate
    try:
        asset_id = backend.add(video)
    finally:
        gl.media_manager.generate_thumbnail = real_generate

    assert asset_id is not None, "a valid video must add through the gate"
    assert len(calls) == 1, (
        f"a video add must decode the file exactly once, got {len(calls)}: {calls}"
    )
    asset = backend.get_by_id(asset_id)
    assert asset["thumbnail"] is not None and os.path.isfile(asset["thumbnail"]), (
        "the reused gate decode must still produce a real thumbnail file"
    )
    print("ok: a valid video add decodes the file exactly once")


def check_ui_add_shows_alert_dialog(backend: AssetManagerBackend, files: dict) -> None:
    """A refused drop/import must tell the user (AlertDialog), not silently
    do nothing. Gtk/GLib are stubbed at the module level -- the harness is
    headless and only the dialog CONSTRUCTION is under test."""
    dialogs: list[dict] = []
    shown: list[tuple] = []
    built_before_idle: list[int] = []

    class FakeAlertDialog:
        def __init__(self, **kwargs):
            dialogs.append(kwargs)

        def show(self, *a, **k):
            shown.append(a)

    def fake_idle_add(fn, *a):
        # Record how many dialogs exist when the callback is SCHEDULED: the
        # calling (worker) thread must not have built any GTK object --
        # construction belongs inside the main-thread callback (this path
        # runs on the Chooser's bare import thread, see Chooser.add_files).
        built_before_idle.append(len(dialogs))
        return fn(*a)

    real_gtk, real_glib = amb_mod.Gtk, amb_mod.GLib
    real_app = gl.app
    amb_mod.Gtk = types.SimpleNamespace(AlertDialog=FakeAlertDialog)
    amb_mod.GLib = types.SimpleNamespace(idle_add=fake_idle_add)
    gl.app = types.SimpleNamespace(main_win=None)
    gl.asset_manager_backend = backend
    try:
        result = backend.add_custom_media_set_by_ui(url=None, path=files["garbage_mp4"])
    finally:
        amb_mod.Gtk, amb_mod.GLib = real_gtk, real_glib
        gl.app = real_app

    assert result is None, f"a refused add must yield no media path, got {result!r}"
    assert dialogs, "the refusal must surface an AlertDialog, not silently do nothing"
    assert built_before_idle and built_before_idle[-1] == len(dialogs) - 1, (
        "the AlertDialog must be constructed INSIDE the idle callback (this "
        "path runs on the Chooser's worker thread), not on the calling thread"
    )
    assert shown, "the dialog must be shown, not just constructed"
    text = " ".join(str(v) for v in dialogs[-1].values()).lower()
    assert "corrupt" in text, f"the dialog text must cover the corrupt case: {dialogs[-1]}"
    print("ok: a refused UI add raises an AlertDialog covering corrupt files")


def main() -> None:
    files = make_corrupt_files()
    backend = AssetManagerBackend()
    gl.asset_manager_backend = backend

    check_corrupt_refused_no_partial_copy(backend, files)
    check_valid_still_adds(backend)
    check_decode_for_import_keys_off_sc_broken(backend)
    check_video_add_decodes_once(backend)
    check_ui_add_shows_alert_dialog(backend, files)


if __name__ == "__main__":
    fixtures.start_watchdog(60, label="asset_add_corrupt")
    main()
    print("PASS")
