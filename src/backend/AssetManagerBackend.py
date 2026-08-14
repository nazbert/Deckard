"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

import os
import shutil
import uuid
from typing import Any
from loguru import logger as log
from PIL import Image

from src.backend.DeckManagement.HelperMethods import is_video, is_image, sha256, file_in_dir, download_file, is_svg
from src.backend import settings_store

import globals as gl


class AssetManagerBackend(list):
    # Where the library index lives. Read at import from the surface that owns
    # it, so this class and the store cannot name two different files. The
    # store resolves the path per call and this attribute does not, which
    # holds because the data path settles before this module imports.
    JSON_PATH = settings_store.ASSET_LIBRARY.path()
    def __init__(self):
        self.load_json()

        self.fill_missing_data()

        self.remove_invalid_data()

    def load_json(self):
        # An unreadable library index must not stop app startup. This
        # constructor runs while main builds the global objects, so a raise
        # here stops the app. The store moves a corrupt index to a .corrupt
        # sidecar, logs it, and reads an empty library. The assets stay on
        # disk. An absent index reads as an empty list, which is the shape a
        # fresh install must write.
        self.clear()
        self.extend(settings_store.get().read(settings_store.ASSET_LIBRARY))

    def save_json(self):
        settings_store.get().write(settings_store.ASSET_LIBRARY, list(self))

    def add(self, asset_path: str, licence_name: str = None, licence_url: str = None, author: str = None) -> str | None:
        if not os.path.exists(asset_path):
            log.warning(f"File {asset_path} not found.")
            return None
        
        
        try:
            hash = sha256(asset_path)
        except OSError as e:
            # An unreadable file, a permission error for example. Fail this one
            # asset with a warning and keep the import worker thread alive.
            # Callers handle a None id.
            log.opt(exception=True).warning(f"Could not read asset {asset_path}: {e}")
            return None

        existing = self.get_by_sha256(hash)
        if existing is not None:
            #TODO: It is possible that the some image has the same sha but not the name because it got renamed
            log.warning(f"Tried to add already existing asset. Ignoring. File: {asset_path}")
            return existing["id"]

        # Refuse an undecodable file at import time, before the copy, while
        # the user can see the dialog and retry. This gate covers imports
        # only. A failed decode never deletes an asset already in the library,
        # because the failure can be transient (see remove_invalid_data and
        # fill_missing_thumbnails). save_thumbnail below reuses the decoded
        # image, so an add decodes a video or an svg once.
        decoded = self._decode_for_import(asset_path)
        if decoded is None:
            log.warning(f"Refusing to import undecodable asset {asset_path}")
            return None

        # Copy every asset into the internal folder. internal-path must never
        # point outside the app's data directory, because remove_asset_by_id()
        # calls os.remove() on it. copy_asset() handles the same-file and the
        # name-collision cases.
        try:
            internal_path = self.copy_asset(asset_path)
        except Exception as e:
            # Destination permissions, a full disk, or a file deleted between
            # the hash and the copy. Fail this one asset and keep the import
            # worker thread alive.
            log.opt(exception=True).warning(f"Could not import asset {asset_path}: {e}")
            return None

        thumbnail_path = internal_path

        if is_video(asset_path):
            thumbnail_path = self.save_thumbnail(asset_path, hash, image=decoded)

        if is_svg(asset_path):
            thumbnail_path = self.save_thumbnail(asset_path, hash, image=decoded)


        asset: dict[str, Any] = {
            "name": os.path.splitext(os.path.basename(asset_path))[0],
            "original-path": asset_path,
            "internal-path": internal_path,
            "sha256": hash,
            "id": self.create_unique_uuid(),
            "license": {
                "name": licence_name,
                "url": licence_url,
                "author": author
            },
            "thumbnail": thumbnail_path
        }
        self.append(asset)

        self.save_json()

        return asset["id"]

    def _decode_for_import(self, path: str) -> Image.Image | None:
        # The decode gate for add(). It returns the decoded image, or None for
        # a file that does not decode. Read the sc_broken marker, because
        # generate_thumbnail never raises. It returns the tagged placeholder
        # on a decode failure, so an except clause would always pass.
        thumbnail = gl.media_manager.generate_thumbnail(path)
        if thumbnail.info.get("sc_broken"):
            return None
        return thumbnail

    def save_thumbnail(self, asset_path, asset_hash, image: Image.Image = None):
        thumbnail_path = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "thumbnails", f"{asset_hash}.png")

        if os.path.exists(thumbnail_path):
            return thumbnail_path
        if not (is_video(asset_path) or is_svg(asset_path)):
            return asset_path
        
        os.makedirs(os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "thumbnails"), exist_ok=True)

        # Guard the thumbnail build. It runs on the import worker thread from
        # add() and at app startup from fill_missing_thumbnails, and one
        # corrupt video or svg must kill neither. A failure returns None. The
        # preview then renders the broken marker, and fill_missing_thumbnails
        # retries a None entry on every boot, so a transient failure (a file
        # mid-download, a network mount) heals itself.
        # add() passes its gate decode in image, so an import decodes once.
        try:
            thumbnail = image if image is not None else gl.media_manager.generate_thumbnail(asset_path)
            if thumbnail.info.get("sc_broken"):
                # generate_thumbnail logged the decode failure. Do not write
                # the placeholder, so the file stays retryable.
                return None
            gl.media_manager.save_image_atomic(thumbnail, thumbnail_path)
        except Exception as e:
            log.opt(exception=True).warning(f"Could not create thumbnail for {asset_path}: {e}")
            return None

        return thumbnail_path
    
    def remove_asset_by_id(self, id: str) -> None:
        asset = self.get_by_id(id)
        if asset is None:
            return
        
        internal_path = asset["internal-path"]

        if gl.page_manager is not None:
            gl.page_manager.remove_asset_from_all_pages(internal_path)

        # Guard the delete. A broken asset whose file is already gone must
        # still lose its entry, and must not raise out of the UI.
        try:
            if internal_path is not None and os.path.exists(internal_path):
                os.remove(internal_path)
        except OSError as e:
            log.opt(exception=True).warning(f"Could not delete asset file {internal_path}: {e}")

        self.remove(asset)
        self.save_json()
        
        
    def copy_asset(self, asset_path: str) -> str:
        file_name = os.path.basename(asset_path)
        dst_path = None
        if not file_in_dir(file_name, os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets")):
            dst_path = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets", file_name)
        else:
            log.warning(f"File with same name already exists but sha256 does not match, renaming: {asset_path}")
            original_base, ext = os.path.splitext(os.path.basename(asset_path))
            index = 2
            while file_in_dir(file_name, os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets")):
                file_name = f"{original_base}-{str(index).zfill(2)}{ext}"
                index += 1
            dst_path = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets", file_name)

        if asset_path == dst_path:
            return asset_path

        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.copy(asset_path, dst_path)
        except shutil.SameFileError:
            log.warning(f"File already exists: {dst_path}")
        return dst_path
    
    def create_unique_uuid(self) -> str:
        id = str(uuid.uuid4())
        if self.has_by_id(id):
            # The new uuid collides with an existing asset id.
            log.warning("Congratulations, you already have an asset with this id. This is very rare.")
            return self.create_unique_uuid()
        return id

    def has_by_name(self, name: str) -> bool:
        return self.get_by_name(name) is not None
            
    def has_by_sha256(self, sha256: str) -> bool:
        return self.get_by_sha256(sha256) is not None

    def has_by_id(self, id: str) -> bool:
        return self.get_by_id(id) is not None
    
    def has_by_internal_path(self, internal_path: str) -> bool:
        return self.get_by_internal_path(internal_path) is not None

    def get_by_name(self, name: str) -> dict | None:
        for asset in self:
            if asset["name"] == name:
                return asset
        return None

    def get_by_sha256(self, sha256: str) -> dict | None:
        for asset in self:
            if asset["sha256"] == sha256:
                return asset
        return None

    def get_by_id(self, id: str) -> dict | None:
        for asset in self:
            if asset["id"] == id:
                return asset
        return None

    def get_by_internal_path(self, internal_path: str) -> dict | None:
        for asset in self:
            if asset["internal-path"] == internal_path:
                return asset
        return None

    def get_all(self) -> list:
        return self
    
    def fill_missing_data(self):
        def fill_missing_folders():
            os.makedirs(os.path.join(gl.DATA_PATH, "Assets", "thumbnails"), exist_ok=True)

        def fill_missing_thumbnails():
            for asset in self:
                # A failed run leaves thumbnail null in the json, and
                # os.path.exists(None) raises TypeError and stops app startup.
                # Check for null first.
                if asset.get("thumbnail") is not None:
                    if os.path.exists(asset["thumbnail"]):
                        continue

                # Guard each asset, so one poison entry cannot block the rest
                # of the batch or the startup. On failure the thumbnail stays
                # None, and never an existing path, so the check above retries
                # it on the next boot.
                try:
                    thumbnail_path = self.save_thumbnail(asset["internal-path"], asset["sha256"])
                except Exception as e:
                    log.opt(exception=True).warning(
                        f"Could not restore thumbnail for {asset.get('internal-path')}: {e}")
                    thumbnail_path = None

                asset["thumbnail"] = thumbnail_path

        
        fill_missing_folders()
        fill_missing_thumbnails()

        self.save_json()

    def remove_invalid_data(self):
        # Drop every asset whose internal file is gone. Iterate over a copy:
        # self.remove() during iteration skips the next element. Check
        # internal-path for null too, because os.path.exists(None) raises
        # TypeError and stops app startup.
        for asset in list(self):
            internal_path = asset.get("internal-path")
            if internal_path is None or not os.path.exists(internal_path):
                self.remove(asset)
        self.save_json()

    def _alert_on_main(self, window, message: str, detail: str) -> None:
        # Build the dialog inside the idle callback. This path runs on the
        # Chooser's import worker thread (add_files starts a Thread per drop),
        # and only the main thread may build a GTK object. Call show(window):
        # without a parent, modal=True binds to nothing.
        def show():
            dial = Gtk.AlertDialog(message=message, detail=detail, modal=True)
            dial.show(window)
        GLib.idle_add(show)

    # Both arguments are optional. A drop supplies a local path or a remote
    # url, and the callers pass the Gdk.FileList accessors of Gtk straight
    # through.
    def add_custom_media_set_by_ui(self, url: str | None, path: str | None):
        window = gl.app.main_win if gl.app is not None else None
        if gl.store is not None:
            window = gl.store

        if path is None and url is not None:
            # Lowercase the extension and drop the dot.
            extension = os.path.splitext(url)[1].lower().replace(".", "")
            if extension not in (set(gl.video_extensions) | set(gl.image_extensions) | set(gl.svg_extensions)):

                self._alert_on_main(
                    window,
                    message="The image is invalid.",
                    detail="You can only use urls directly pointing to images (not directly from Google).",
                )
                # Return None, like every other refusal here. The callers test
                # the result for a usable media path.
                return None

            os.makedirs(os.path.join(gl.DATA_PATH, "cache", "downloads"), exist_ok=True)
            # download_file raises on a network failure and on an HTTP error
            # status, instead of writing the error page into the asset cache.
            # Catch it here. A bad link otherwise kills the import worker
            # thread and tells the user nothing. The Chooser drop path runs
            # this on a worker thread; KeyGrid runs it on the GTK main thread.
            try:
                path = download_file(url=url, path=os.path.join(gl.DATA_PATH, "cache", "downloads"))
            except Exception as e:
                log.opt(exception=True).error(f"Could not download asset from {url}: {e}")
                self._alert_on_main(
                    window,
                    message="The download failed.",
                    detail="The image or video could not be downloaded. Check the url and your connection.",
                )
                # Return None. KeyGrid.handle_file_drop stops on None only.
                return None

        if path is None:
            return
        if not os.path.exists(path):
            return
        if not is_video(path) and not is_image(path) and not is_svg(path):
            self._alert_on_main(
                window,
                message="No valid image or video.",
                detail="Only images and videos are supported.",
            )
            return
        asset_id = gl.asset_manager_backend.add(asset_path=path)
        if asset_id is None:
            # add() refuses an undecodable file, and returns None for an
            # unreadable or uncopyable one. Without this dialog the drop shows
            # the user nothing.
            self._alert_on_main(
                window,
                message="No valid image or video.",
                detail="Only images and videos are supported. The file may also be corrupt or unreadable.",
            )
            return

        asset = self.get_by_id(asset_id)
        if asset is None:
            # add() returned this id a few lines above, so nothing reaches
            # here.
            return None

        # Add the asset to the chooser UI while it is open.
        if gl.asset_manager is not None:
            gl.asset_manager.asset_chooser.custom_asset_chooser.add_asset(asset)

        return asset.get("internal-path")