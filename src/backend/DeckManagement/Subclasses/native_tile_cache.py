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
import os

from loguru import logger as log

from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache

DEFAULT_MAX_MB = 64


def native_tile_cache_max_bytes() -> int:
    """Byte cap for a deck's NativeTileCache, from
    DECKARD_NATIVE_TILE_CACHE_MB (0 disables the whole frame-identity path,
    falling playback back to the pixel-hash encode memo). A malformed value
    degrades to the default with a warning -- it must never raise out of
    DeckController.__init__, where DeckManager would swallow it as "Failed to
    initialize deck" and silently skip the whole device."""
    raw = os.environ.get("DECKARD_NATIVE_TILE_CACHE_MB")
    if raw is None:
        return DEFAULT_MAX_MB * 1024 * 1024
    try:
        mb = float(raw)
    except ValueError:
        log.warning(
            f"Ignoring malformed DECKARD_NATIVE_TILE_CACHE_MB={raw!r}; "
            f"using the default {DEFAULT_MAX_MB}"
        )
        return DEFAULT_MAX_MB * 1024 * 1024
    if mb < 0:
        return 0
    return int(mb * 1024 * 1024)


class NativeTileCache(ByteLRUCache):
    """LRU of encoded (device-native) background key tiles, keyed by FRAME
    IDENTITY -- (video md5, frame index, key index, rotation, quality,
    native format) -- instead of by composited pixels. Capped by total byte
    size; thread-safe; values must be immutable bytes.

    A bare key over a video background composites to exactly the shared
    background tile, so its native bytes are a pure function of that tuple:
    the tile never has to be serialized or hashed to know which encoded
    frame belongs on the device. A looping video therefore pays its encodes
    once, on the first playthrough, and every later loop is a dict lookup.

    Deliberately WITHOUT EncodedImageCache's doorkeeper: that admits a key
    only on its second sighting, which protects a pixel-hash namespace where
    high-entropy content produces keys that never repeat. Identity keys are
    drawn from a finite, guaranteed-repeating space ((frames x keys) for the
    loaded video), so admitting on first sighting is exactly what makes the
    second loop encode-free, and the byte cap is what bounds it.

    Kept separate from `encode_memo` rather than sharing its namespace:
    identity keys and pixel-hash keys would otherwise collide in one key
    space, and the two want independent sizing.

    Everything else -- byte accounting, LRU order, `clear()`, the `<= 0`
    kill switch, and the budget-participant surface -- is `ByteLRUCache`
    exactly; this class is that core with the default (first-sighting)
    admission policy and no extra teardown bookkeeping.
    """

