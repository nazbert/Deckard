"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import math
import os

from loguru import logger as log

from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache

DEFAULT_MAX_MB = 64


def native_tile_cache_max_bytes() -> int:
    """Byte cap for a deck's NativeTileCache, read from
    DECKARD_NATIVE_TILE_CACHE_MB.

    0 disables the frame-identity path and falls playback back to the
    pixel-hash encode memo. A malformed value degrades to the default.
    """
    raw = os.environ.get("DECKARD_NATIVE_TILE_CACHE_MB")
    if raw is None:
        return DEFAULT_MAX_MB * 1024 * 1024
    try:
        mb = float(raw)
        # isfinite, not just float(): "nan", "inf" and an overflowing literal
        # such as "1e400" parse and pass the sign test below, because every
        # nan comparison is False, and then int() raises ValueError for nan
        # and OverflowError for inf. This function must never raise out of
        # DeckController.__init__, where DeckManager reports it as "Failed to
        # initialize deck" and skips the whole device.
        usable = math.isfinite(mb)
    except ValueError:
        usable = False
    if not usable:
        log.warning(
            f"Ignoring malformed DECKARD_NATIVE_TILE_CACHE_MB={raw!r}; "
            f"using the default {DEFAULT_MAX_MB}"
        )
        return DEFAULT_MAX_MB * 1024 * 1024
    if mb < 0:
        return 0
    return int(mb * 1024 * 1024)


class NativeTileCache(ByteLRUCache):
    """LRU of encoded, device-native background key tiles.

    The key is the frame identity: video md5, frame index, key index,
    rotation, quality and native format. It is not the composited pixels. A
    bare key over a video background composites to the shared background tile,
    so its native bytes are a pure function of that tuple, and the tile needs
    no serialization and no hash. A looping video pays its encodes on the
    first playthrough, and every later loop is a dict lookup.

    This class keeps ByteLRUCache's default first-sighting admission and adds
    no doorkeeper. EncodedImageCache's doorkeeper protects a pixel-hash
    namespace where high-entropy content produces keys that never repeat.
    Identity keys come from a finite, repeating space, the frames times the
    keys of the loaded video, so first-sighting admission is what makes the
    second loop encode-free, and the byte cap bounds it.

    Keep this separate from encode_memo. In one shared key space the two kinds
    of key collide, and the two need independent sizing.
    """

