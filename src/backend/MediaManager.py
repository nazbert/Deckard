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
# Import Python modules
import os
import cv2
from loguru import logger as log
from PIL import Image, ImageDraw, ImageSequence

import os, psutil
process = psutil.Process()

# Import own modules
from src.backend.DeckManagement.HelperMethods import is_svg, sha256, file_in_dir, svg_to_pil


# Import globals
import globals as gl

class MediaManager:
    def __init__(self):
        pass

    def get_fallback_thumbnail(self) -> Image.Image:
        """
        In-memory "broken image" placeholder returned instead of raising when
        a file cannot be decoded (#112). Tagged via img.info["sc_broken"] so
        callers can tell it apart from a real thumbnail and never persist it
        (a corrupt file must stay retryable, not poison the on-disk cache).
        """
        img = Image.new("RGBA", (250, 180), (58, 58, 58, 255))
        draw = ImageDraw.Draw(img)
        # Simple "broken image" glyph: a frame with a diagonal cross
        draw.rectangle((95, 60, 155, 120), outline=(170, 170, 170, 255), width=3)
        draw.line((95, 60, 155, 120), fill=(170, 170, 170, 255), width=3)
        draw.line((155, 60, 95, 120), fill=(170, 170, 170, 255), width=3)
        img.info["sc_broken"] = True
        return img

    def get_thumbnail(self, file_path):
        # Guarded whole (#112): sha256() raises on unreadable files
        # (chmod 000), Image.open raises on a poisoned cache entry, and the
        # .thumbnail() calls force lazy decodes -- every caller here is a UI
        # path that must get *an* image back, never an exception.
        try:
            hash = sha256(file_path)

            thumbnail_dir = os.path.join(gl.DATA_PATH, "cache", "thumbnails")
            thumbnail_path = os.path.join(thumbnail_dir, f"{hash}.png")

            os.makedirs(thumbnail_dir, exist_ok=True)


            # Check if thumbnail has already been cached:
            cached = file_in_dir(f"{hash}.png", thumbnail_dir)
            if cached is None:
                cached = False

            if cached:
                with Image.open(thumbnail_path) as img:
                    img.thumbnail((250, 250), resample=Image.Resampling.LANCZOS)
                    return img.copy()
            else:
                thumbnail = self.generate_thumbnail(file_path)
                thumbnail.thumbnail((250, 250), resample=Image.Resampling.LANCZOS)
                if not thumbnail.info.get("sc_broken"):
                    # Never cache the placeholder -- the cache is keyed by the
                    # file's content hash, so a cached placeholder would stick
                    # even for transient failures (e.g. permissions).
                    thumbnail.save(thumbnail_path)
                return thumbnail
        except Exception as e:
            log.warning(f"Could not create thumbnail for {file_path}: {e}")
            return self.get_fallback_thumbnail()

    def generate_thumbnail(self, file_path):
        # Never raises (#112): one corrupt/unreadable file must not kill the
        # import worker thread, the Custom Assets build or app startup
        # (AssetManagerBackend.fill_missing_thumbnails). On any decode failure
        # this logs the path and returns the tagged fallback placeholder.
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
            # Image.open is lazy -- force the decode NOW so a truncated file
            # raises inside this guard instead of later in the caller.
            thumbnail.load()
            return thumbnail
        except Exception as e:
            log.warning(f"Could not generate thumbnail for {file_path}: {e}")
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
            # 0-byte/corrupt video: cap.read() returns (False, None) and
            # cv2.cvtColor(None) would raise an opaque cv2.error.
            raise ValueError(f"could not read a frame from video: {video_path}")

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_image = Image.fromarray(frame_rgb)
        return pil_image
    
    def generate_svg_thumbnail(self, file_path):
        return svg_to_pil(file_path, 1024)

    def generate_image_thumbnail(self, file_path):
        return Image.open(file_path)
    
    def generate_gif_thumbnail(self, file_path):
        # This is the same as load_video but with transparency support
        gif = Image.open(file_path)
        iterator = ImageSequence.Iterator(gif)
        n_frames = 0
        for frame in iterator: n_frames += 1 #TODO: Find a better way to do this
        frame = iterator[n_frames // 2] # Gifs tend to have a empty frame at the beginning
        frame = frame.convert("RGBA")

        gif = None
        iterator = None
        n_frames = None

        return frame