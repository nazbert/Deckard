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
    """LRU of encoded (device-native) key images, capped by total byte size.
    Thread-safe; values must be immutable bytes.

    The LRU/byte-accounting core lives in `ByteLRUCache`; this class is that
    core plus a doorkeeper.

    Admission into the real cache is gated by a small doorkeeper ring
    (mem-plan P2.5): a key only earns a cache slot on its SECOND sighting.
    Looping content (any video/GIF background -- the overwhelmingly common
    case) repeats the same small set of keys every cycle and is fully warmed
    by the second or third wrap. High-entropy content (background video
    noise, or any source whose composited hash never repeats) never gets a
    second sighting and so never displaces a real, reusable entry -- it
    costs one small bookkeeping slot instead of a full cache slot.

    No "volatile" flag and no caller-side plumbing: put()'s one caller only
    ever sees the already-composited image, so there is nothing for a caller
    to tell this cache that this class can't already infer from repetition.
    """

    # Ring size independent of the byte-size cap above: this bounds
    # bookkeeping entries (a hashable key each), not cached pixel data. 512
    # is generously larger than any single loop's distinct-key count at
    # today's content sizes, so a full loop's keys are still in the ring
    # (and thus admitted) by the time it repeats.
    DOORKEEPER_SIZE = 512

    def __init__(self, max_bytes: int):
        super().__init__(max_bytes)
        # Doorkeeper bookkeeping: a bounded FIFO of recently-seen keys (set
        # for O(1) membership, deque to know which to evict from the set
        # once the ring is full).
        self._doorkeeper_seen: set = set()
        self._doorkeeper_order: "deque" = deque()

    def _admit(self, key) -> bool:
        """Doorkeeper check-and-record, called with `_lock` already held.
        Returns True once `key` has been seen before (this sighting is its
        second or later -- let it into the real cache); records a first
        sighting and returns False otherwise. A False return means put()
        spends a bookkeeping slot instead of a real cache slot."""
        if key in self._doorkeeper_seen:
            return True
        self._doorkeeper_seen.add(key)
        self._doorkeeper_order.append(key)
        if len(self._doorkeeper_order) > self.DOORKEEPER_SIZE:
            oldest = self._doorkeeper_order.popleft()
            self._doorkeeper_seen.discard(oldest)
        return False

    def _on_clear_locked(self) -> None:
        """clear() (plan P1.3 close() step 7 / P2.5) also resets the
        doorkeeper: stale "seen" bookkeeping from the old content must not
        let one of its keys skip straight past admission if it ever
        coincidentally recurred under the new content."""
        self._doorkeeper_seen.clear()
        self._doorkeeper_order.clear()
