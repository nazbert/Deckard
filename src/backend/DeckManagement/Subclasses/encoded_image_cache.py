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
from collections import OrderedDict, deque


class EncodedImageCache:
    """LRU of encoded (device-native) key images, capped by total byte size.
    Thread-safe; values must be immutable bytes.

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
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entries: "OrderedDict[object, bytes]" = OrderedDict()
        self._total_bytes = 0
        # Doorkeeper bookkeeping: a bounded FIFO of recently-seen keys (set
        # for O(1) membership, deque to know which to evict from the set
        # once the ring is full).
        self._doorkeeper_seen: set = set()
        self._doorkeeper_order: "deque" = deque()

    def get(self, key) -> bytes | None:
        with self._lock:
            data = self._entries.get(key)
            if data is not None:
                self._entries.move_to_end(key)
            return data

    def put(self, key, data: bytes) -> None:
        with self._lock:
            if key not in self._entries and not self._admit(key):
                # First sighting of a key that isn't already cached: record
                # it in the doorkeeper only, don't spend a real cache slot.
                return
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= len(old)
            self._entries[key] = data
            self._total_bytes += len(data)
            while self._total_bytes > self._max_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._total_bytes -= len(evicted)

    def _admit(self, key) -> bool:
        """Doorkeeper check-and-record, called with `_lock` already held.
        Returns True once `key` has been seen before (this sighting is its
        second or later -- let it into the real cache); records a first
        sighting and returns False otherwise."""
        if key in self._doorkeeper_seen:
            return True
        self._doorkeeper_seen.add(key)
        self._doorkeeper_order.append(key)
        if len(self._doorkeeper_order) > self.DOORKEEPER_SIZE:
            oldest = self._doorkeeper_order.popleft()
            self._doorkeeper_seen.discard(oldest)
        return False

    def clear(self) -> None:
        """Drops every cached entry (plan P1.3 close() step 7 / P2.5): a
        torn-down deck's encode memo must not keep referencing composited
        frames from a dead controller until LRU eviction eventually gets
        around to it -- and a background content change (P2.5) orphans
        every entry the exact same way, since they're all keyed against the
        OLD background's composited pixels/hashes. Also resets the
        doorkeeper: stale "seen" bookkeeping from the old content must not
        let one of its keys skip straight past admission if it ever
        coincidentally recurred under the new content."""
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0
            self._doorkeeper_seen.clear()
            self._doorkeeper_order.clear()
