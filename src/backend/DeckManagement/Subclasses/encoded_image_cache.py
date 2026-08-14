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
from collections import deque

from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache


class EncodedImageCache(ByteLRUCache):
    """LRU of encoded, device-native key images, capped by total byte size.

    It is thread-safe, and values must be immutable bytes. ByteLRUCache holds
    the LRU and byte-accounting core, and this class is that core plus a
    doorkeeper.

    A small doorkeeper ring gates admission into the real cache. A key earns a
    cache slot on its second sighting. Looping content, which is any video or
    GIF background and the common case, repeats the same small key set every
    cycle and warms fully by the second or third wrap. High-entropy content,
    such as background video noise or any source whose composited hash never
    repeats, gets no second sighting and displaces no reusable entry. It costs
    one small bookkeeping slot instead of a full cache slot.

    There is no "volatile" flag and no caller-side plumbing. put()'s one
    caller sees only the already-composited image, so a caller can tell this
    cache nothing the class cannot infer from repetition.
    """

    # The ring size is independent of the byte cap above, because the ring
    # bounds bookkeeping entries, one hashable key each, and not cached pixel
    # data. 512 is well above the distinct-key count of a single loop at
    # today's content sizes, so a full loop's keys are still in the ring, and
    # therefore admitted, by the time the loop repeats.
    DOORKEEPER_SIZE = 512

    def __init__(self, max_bytes: int):
        super().__init__(max_bytes)
        # Doorkeeper bookkeeping, a bounded FIFO of recently-seen keys. The
        # set gives O(1) membership, and the deque names which key to evict
        # from the set once the ring is full.
        self._doorkeeper_seen: set = set()
        self._doorkeeper_order: "deque" = deque()

    def _admit(self, key) -> bool:
        """Doorkeeper check and record. The caller holds _lock.

        Returns True when the ring already holds key, that is on its second or
        later sighting, and the key then enters the real cache. Otherwise it
        records a first sighting and returns False, and put() spends a
        bookkeeping slot instead of a real cache slot.
        """
        if key in self._doorkeeper_seen:
            return True
        self._doorkeeper_seen.add(key)
        self._doorkeeper_order.append(key)
        if len(self._doorkeeper_order) > self.DOORKEEPER_SIZE:
            oldest = self._doorkeeper_order.popleft()
            self._doorkeeper_seen.discard(oldest)
        return False

    def _on_clear_locked(self) -> None:
        """clear() also resets the doorkeeper. Stale "seen" bookkeeping from
        the old content must not let one of its keys skip admission when that
        key recurs by coincidence under the new content."""
        self._doorkeeper_seen.clear()
        self._doorkeeper_order.clear()
