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
import os
import uuid
import cv2
from loguru import logger as log
from PIL import Image, ImageDraw, ImageSequence

import os, psutil
process = psutil.Process()

from src.backend.DeckManagement.HelperMethods import is_svg, sha256, file_in_dir, svg_to_pil


import globals as gl

class MediaManager:
    def __init__(self):
        pass

    def get_fallback_thumbnail(self) -> Image.Image:
        """The in-memory "broken image" placeholder for a file that does not
        decode. img.info["sc_broken"] tags it, so a caller tells it apart from
        a real thumbnail and never writes it to the on-disk cache. A corrupt
        file must stay retryable."""
        img = Image.new("RGBA", (250, 180), (58, 58, 58, 255))
        draw = ImageDraw.Draw(img)
        # A "broken image" glyph, drawn as a frame with a diagonal cross.
        draw.rectangle((95, 60, 155, 120), outline=(170, 170, 170, 255), width=3)
        draw.line((95, 60, 155, 120), fill=(170, 170, 170, 255), width=3)
        draw.line((155, 60, 95, 120), fill=(170, 170, 170, 255), width=3)
        img.info["sc_broken"] = True
        return img

    @staticmethod
    def save_image_atomic(image: Image.Image, path: str) -> None:
        """Crash-safe image save. Write a unique temp file in the same
        directory, then os.replace() it into place. A crash or a full disk
        mid-save must leave no half-written file at path."""
        tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            image.save(tmp_path, format="PNG")
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def get_thumbnail(self, file_path):
        # Guard the whole body. sha256() raises on an unreadable file (chmod
        # 000), Image.open raises on a poisoned cache entry, and .thumbnail()
        # forces a lazy decode. Every caller is a UI path that needs an image
        # back rather than an exception.
        try:
            hash = sha256(file_path)

            thumbnail_dir = os.path.join(gl.DATA_PATH, "cache", "thumbnails")
            thumbnail_path = os.path.join(thumbnail_dir, f"{hash}.png")

            os.makedirs(thumbnail_dir, exist_ok=True)


            # Check for a cached thumbnail.
            cached = file_in_dir(f"{hash}.png", thumbnail_dir)
            if cached is None:
                cached = False

            if cached:
                try:
                    with Image.open(thumbnail_path) as img:
                        img.thumbnail((250, 250), resample=Image.Resampling.LANCZOS)
                        return img.copy()
                except Exception as e:
                    # A poisoned cache entry, from a crash mid-write for
                    # example. A bad cache file must not pin a valid source
                    # file to the broken placeholder, so drop the entry and
                    # fall through to a fresh generation.
                    log.opt(exception=True).warning(
                        f"Poisoned thumbnail cache entry for {file_path}, regenerating: {e}")
                    try:
                        os.remove(thumbnail_path)
                    except OSError:
                        pass

            thumbnail = self.generate_thumbnail(file_path)
            thumbnail.thumbnail((250, 250), resample=Image.Resampling.LANCZOS)
            if not thumbnail.info.get("sc_broken"):
                # Never cache the placeholder. The cache key is the file's
                # content hash, so a cached placeholder stays even after a
                # transient failure such as a permission error.
                self.save_image_atomic(thumbnail, thumbnail_path)
            return thumbnail
        except Exception as e:
            log.opt(exception=True).warning(f"Could not create thumbnail for {file_path}: {e}")
            return self.get_fallback_thumbnail()

    def generate_thumbnail(self, file_path):
        # This never raises. One corrupt or unreadable file must not kill the
        # import worker thread, the Custom Assets build or app startup
        # (AssetManagerBackend.fill_missing_thumbnails). A decode failure logs
        # the path and returns the tagged fallback placeholder.
        try:
            extension = os.path.splitext(file_path)[1].lower()
            if extension in (".jpg", ".jpeg", ".png"):
                thumbnail = self.generate_image_thumbnail(file_path)
            elif extension == ".gif":
                thumbnail = self.generate_gif_thumbnail(file_path)
            elif is_svg(file_path):
                thumbnail = self.generate_svg_thumbnail(file_path)
            else:
                thumbnail = self.generate_video_thumbnail(file_path)

            if thumbnail is None:
                raise ValueError("decoder returned no image")
            # Image.open is lazy. Force the decode here, so a truncated file
            # raises inside this guard and not later in the caller.
            thumbnail.load()
            return thumbnail
        except Exception as e:
            # Pass exception=True, so the logs separate a programming error in
            # the decode chain from a poison file.
            log.opt(exception=True).warning(f"Could not generate thumbnail for {file_path}: {e}")
            return self.get_fallback_thumbnail()

    def generate_video_thumbnail(self, video_path: str) -> Image.Image:
        cap = cv2.VideoCapture(video_path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 1])
        try:
            if not cap.isOpened():
                raise ValueError(f"could not open video: {video_path}")
            cap.set(cv2.CAP_PROP_POS_FRAMES, 1)
            ret, frame = cap.read()
        finally:
            cap.release()

        if not ret or frame is None:
            # For a 0-byte or corrupt video cap.read() returns (False, None),
            # and cv2.cvtColor(None) raises an opaque cv2.error.
            raise ValueError(f"could not read a frame from video: {video_path}")

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(frame_rgb)
        return pil_image
    
    def generate_svg_thumbnail(self, file_path):
        return svg_to_pil(file_path, 1024)

    def generate_image_thumbnail(self, file_path):
        return Image.open(file_path)
    
    def generate_gif_thumbnail(self, file_path):
        # Same as the video path, plus transparency support.
        gif = Image.open(file_path)
        iterator = ImageSequence.Iterator(gif)
        n_frames = 0
        for frame in iterator: n_frames += 1 #TODO: Find a better way to do this
        frame = iterator[n_frames // 2] # A GIF often starts with an empty frame
        frame = frame.convert("RGBA")

        gif = None
        iterator = None
        n_frames = None

        return frame