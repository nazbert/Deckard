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
import threading
from collections import OrderedDict

from loguru import logger as log

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


class NativeTileCache:
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
    """

    def __init__(self, max_bytes: int):
        # <= 0 disables: get() always misses and put() stores nothing, so
        # callers fall back to the pixel-hash path with no extra branching
        # of their own (env kill-switch, see native_tile_cache_max_bytes).
        self._max_bytes = max(0, max_bytes)
        self._lock = threading.Lock()
        self._entries: "OrderedDict[object, bytes]" = OrderedDict()
        self._total_bytes = 0

    @property
    def enabled(self) -> bool:
        return self._max_bytes > 0

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key) -> bytes | None:
        if self._max_bytes <= 0:
            return None
        with self._lock:
            data = self._entries.get(key)
            if data is not None:
                self._entries.move_to_end(key)
            return data

    def put(self, key, data: bytes) -> None:
        if self._max_bytes <= 0:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= len(old)
            self._entries[key] = data
            self._total_bytes += len(data)
            while self._total_bytes > self._max_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._total_bytes -= len(evicted)

    def clear(self) -> None:
        """Drops every entry. Called wherever the encode memo is cleared: a
        background content change orphans every entry (they are all keyed
        against the OLD video's frames), and a torn-down deck must not keep
        referencing encoded frames from a dead controller until LRU eviction
        eventually gets around to them."""
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0
