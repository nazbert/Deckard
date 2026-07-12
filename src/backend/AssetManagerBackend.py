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
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Adw, GLib

# Import Python modules
import json
import os
import shutil
import uuid
from loguru import logger as log
from PIL import Image

# Import own modules
from src.backend.DeckManagement.HelperMethods import is_video, is_image, sha256, file_in_dir, create_empty_json, download_file, is_svg
from src.backend.atomic_json import atomic_write_json

# Import globals
import globals as gl


class AssetManagerBackend(list):
    JSON_PATH = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets.json")
    def __init__(self):
        self.load_json()

        self.fill_missing_data()

        self.remove_invalid_data()

    def load_json(self):
        # Create file if it does not exist
        create_empty_json(self.JSON_PATH)
        # Load json file
        with open(self.JSON_PATH, "r") as f:
            self.clear()
            content = json.load(f)
            self.extend(content)

    def save_json(self):
        atomic_write_json(self.JSON_PATH, list(self))

    def add(self, asset_path: str, licence_name: str = None, licence_url: str = None, author: str = None) -> str:
        if not os.path.exists(asset_path):
            log.warning(f"File {asset_path} not found.")
            return
        
        
        try:
            hash = sha256(asset_path)
        except OSError as e:
            # Unreadable file (e.g. permissions): fail this one asset with a
            # warning instead of killing the import worker thread (#112).
            # Callers already handle a None id.
            log.opt(exception=True).warning(f"Could not read asset {asset_path}: {e}")
            return None

        if self.has_by_sha256(hash):
            #TODO: It is possible that the some image has the same sha but not the name because it got renamed
            log.warning(f"Tried to add already existing asset. Ignoring. File: {asset_path}")
            id = self.get_by_sha256(hash)["id"]
            asset = self.get_by_id(id)
            return id
        
        # Copy the asset into the internal folder -- ALWAYS (#112 rev1). The
        # old `if not file_in_dir(basename, DATA_PATH/cache)` skip was a
        # non-recursive top-level name match that nothing legitimately hits
        # (url downloads land in cache/downloads/), but a basename collision
        # would have left internal-path pointing at the user's ORIGINAL file
        # outside the app dir -- which remove_asset_by_id() later os.remove()s.
        # internal-path must never point outside the app's data dir.
        # copy_asset() already handles same-file and name-collision cases.
        try:
            internal_path = self.copy_asset(asset_path)
        except Exception as e:
            # Dest permissions, disk full, file deleted between hash and copy:
            # fail this one asset, don't kill the import worker thread.
            log.opt(exception=True).warning(f"Could not import asset {asset_path}: {e}")
            return None

        thumbnail_path = internal_path
        
        if is_video(asset_path):
            thumbnail_path = self.save_thumbnail(asset_path, hash)

        if is_svg(asset_path):
            thumbnail_path = self.save_thumbnail(asset_path, hash)
            

        asset = {
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

        # Save json
        self.save_json()

        # Return id of added asset
        return asset["id"]
    
    def save_thumbnail(self, asset_path, asset_hash):
        thumbnail_path = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "thumbnails", f"{asset_hash}.png")

        if os.path.exists(thumbnail_path):
            return thumbnail_path
        if not (is_video(asset_path) or is_svg(asset_path)):
            return asset_path
        
        # Create missing directories
        os.makedirs(os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "thumbnails"), exist_ok=True)

        # Create thumbnail. Guarded (#112): this runs on the import worker
        # thread (add) AND at app startup (fill_missing_thumbnails) -- one
        # corrupt video/svg must not kill either. On failure return None
        # (#112 rev1): Preview renders the broken marker for a None thumbnail,
        # and fill_missing_thumbnails retries None entries on every boot, so
        # a TRANSIENT failure (file mid-download, network mount hiccup) heals
        # itself instead of wedging the asset until delete+re-import.
        try:
            thumbnail = gl.media_manager.generate_thumbnail(asset_path)
            if thumbnail.info.get("sc_broken"):
                # generate_thumbnail already logged the decode failure; don't
                # persist the placeholder (keeps the file retryable).
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

        gl.page_manager.remove_asset_from_all_pages(internal_path)

        # Guarded (#112 rev1): deleting a broken asset whose file already
        # vanished must still remove the entry, not raise out of the UI.
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
            # Ensure the dst dir is available
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            # Copy file into internal asset dir
            shutil.copy(asset_path, dst_path)
        except shutil.SameFileError:
            log.warning(f"File already exists: {dst_path}")
        return dst_path
    
    def create_unique_uuid(self) -> str:
        id = str(uuid.uuid4())
        if self.has_by_id(id):
            # For the unlike case that the id is already used
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

    def get_by_name(self, name: str) -> dict:
        for asset in self:
            if asset["name"] == name:
                return asset
            
    def get_by_sha256(self, sha256: str) -> dict:
        for asset in self:
            if asset["sha256"] == sha256:
                return asset
            
    def get_by_id(self, id: str) -> dict:
        for asset in self:
            if asset["id"] == id:
                return asset
            
    def get_by_internal_path(self, internal_path: str) -> dict:
        for asset in self:
            if asset["internal-path"] == internal_path:
                return asset
            
    def get_all(self) -> list:
        return self
    
    def fill_missing_data(self):
        def fill_missing_folders():
            os.makedirs(os.path.join(gl.DATA_PATH, "Assets", "thumbnails"), exist_ok=True)

        def fill_missing_thumbnails():
            for asset in self:
                # A previously failed run can leave thumbnail as null in the
                # json -- os.path.exists(None) would TypeError and take app
                # startup down with it (#112), so null-check first.
                if asset.get("thumbnail") is not None:
                    if os.path.exists(asset["thumbnail"]):
                        continue

                # Create thumbnail -- per-asset guard so one poison entry
                # cannot block the rest of the batch (or startup). On failure
                # the thumbnail stays None (NOT some existing path): the
                # exists-check above must retry it on the next boot (#112 rev1).
                try:
                    thumbnail_path = self.save_thumbnail(asset["internal-path"], asset["sha256"])
                except Exception as e:
                    log.opt(exception=True).warning(
                        f"Could not restore thumbnail for {asset.get('internal-path')}: {e}")
                    thumbnail_path = None

                asset["thumbnail"] = thumbnail_path

        
        fill_missing_folders()
        fill_missing_thumbnails()

        # Save
        self.save_json()

    def remove_invalid_data(self):
        ## Remove assets that have been deleted internally.
        # Iterate over a copy -- self.remove() during iteration skips the
        # element after each removal. Null-safe internal-path (#112 rev1):
        # os.path.exists(None) is a TypeError that would kill app startup
        # right after the fill_missing_data guard.
        for asset in list(self):
            internal_path = asset.get("internal-path")
            if internal_path is None or not os.path.exists(internal_path):
                self.remove(asset)
        self.save_json()

    def add_custom_media_set_by_ui(self, url: str, path: str):
        window = gl.app.main_win
        if gl.store is not None:
            window = gl.store
            
        if path is None and url is not None:
            # Lower domain and remove point
            extension = os.path.splitext(url)[1].lower().replace(".", "")
            if extension not in (set(gl.video_extensions) | set(gl.image_extensions) | set(gl.svg_extensions)):

                # Not a valid url
                dial = Gtk.AlertDialog(
                    message="The image is invalid.",
                    detail="You can only use urls directly pointing to images (not directly from Google).",
                    modal=True
                )
                GLib.idle_add(dial.show)
                return -1

            os.makedirs(os.path.join(gl.DATA_PATH, "cache", "downloads"), exist_ok=True)
            # Download file from url
            path = download_file(url=url, path=os.path.join(gl.DATA_PATH, "cache", "downloads"))

        if path == None:
            return
        if not os.path.exists(path):
            return
        if not is_video(path) and not is_image(path) and not is_svg(path):
            dial = Gtk.AlertDialog(
                    message="No valid image or video.",
                    detail="Only images and videos are supported.",
                    modal=True
                )
            GLib.idle_add(dial.show)
            return
        asset_id = gl.asset_manager_backend.add(asset_path=path)
        if asset_id == None:
            return
        
        asset = self.get_by_id(asset_id)
        # Add to asset chooser ui if opened
        if gl.asset_manager is not None:
            gl.asset_manager.asset_chooser.custom_asset_chooser.add_asset(asset)

        return asset.get("internal-path")