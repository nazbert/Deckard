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
import threading
from collections import OrderedDict


class EncodedImageCache:
    """LRU of encoded (device-native) key images, capped by total byte size.
    Thread-safe; values must be immutable bytes."""

    def __init__(self, max_bytes: int):
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: "OrderedDict[object, bytes]" = OrderedDict()
        self._total_bytes = 0

    def get(self, key) -> bytes | None:
        with self._lock:
            data = self._entries.get(key)
            if data is not None:
                self._entries.move_to_end(key)
            return data

    def put(self, key, data: bytes) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= len(old)
            self._entries[key] = data
            self._total_bytes += len(data)
            while self._total_bytes > self._max_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._total_bytes -= len(evicted)
