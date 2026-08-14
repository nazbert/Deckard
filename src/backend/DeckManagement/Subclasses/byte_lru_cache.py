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

---

The byte-capped LRU shared by both native-image caches.

EncodedImageCache (pixel-hash keys plus a doorkeeper) and NativeTileCache
(frame-identity keys plus a kill switch) need the same OrderedDict-as-LRU and
the same byte accounting. This module holds that core once. The subclasses
supply only their admission policy and their teardown bookkeeping.

The paint path runs on the sole device writer's thread, which is why this
class has this shape. Its cost there:

    get() hit   one dict lookup, one move_to_end, one float store.
    put()       the same, plus the local-cap eviction loop, plus one
                Event.set() per about 1 MiB admitted (see cache_budget's
                wake damping). Nothing outside this instance's lock.
    eviction    popitem scale, one lock, never nested with another.

Instances also implement cache_budget.BudgetParticipant. A process-wide
manager uses it to compare LRU heads across caches and to shed from the
globally-oldest one without holding two cache locks at once. See
cache_budget's module docstring for the ceiling, the overshoot bound, and the
disabled and degenerate behaviors.
"""
import threading
import time
from collections import OrderedDict

from src.backend.DeckManagement.Subclasses import cache_budget

# How long an evicted key stays in the thrash tripwire ring, and how many keys
# the ring holds. Both hold bookkeeping only, one key and one float each. The
# ring makes a re-admission shortly after a global eviction countable, which
# is the only field signal that separates a ceiling binding against a live
# working set from one trimming cold entries.
THRASH_WINDOW_S = 30.0
THRASH_RING_SIZE = 256


class ByteLRUCache:
    """LRU of immutable bytes values, capped by total byte size.

    This is the shared core of EncodedImageCache (pixel-hash keys plus a
    doorkeeper) and NativeTileCache (frame-identity keys plus a kill switch).
    The core is an OrderedDict whose iteration order is the LRU order, through
    a move_to_end on every hit and every put, plus exact byte accounting and
    one instance lock. A subclass adds its admission policy through _admit()
    and its teardown bookkeeping through _on_clear_locked(). Neither overrides
    get or put.
    """

    def __init__(self, max_bytes: int) -> None:
        # A cap of 0 or less disables the cache. get() always misses and put()
        # stores nothing, so callers fall back to their uncached path with no
        # extra branching. See NativeTileCache's env kill switch.
        self._max_bytes = max(0, max_bytes)
        self._lock = threading.Lock()
        # Values must be immutable bytes. This cache hands them out by
        # reference, so refcounting keeps a paint that already holds one alive
        # across an eviction. Eviction is a cost concern here, never a
        # correctness one.
        self._entries: "OrderedDict[object, bytes]" = OrderedDict()
        # Maps each key to its last-use monotonic time. Same key set as
        # _entries, and always mutated under _lock alongside it. Keep the
        # stamps out of band and never in a (data, ts) tuple inside _entries.
        # _entries values then stay the raw bytes object that put() received,
        # which callers and pinned scenarios assert identity on across a hit,
        # and a hit costs one float store instead of a tuple allocation.
        self._stamps: dict[object, float] = {}
        self._total_bytes = 0

        # The thrash tripwire holds the keys the budget evicted recently, not
        # the ones the local cap evicted, and counts how many came straight
        # back. Only budget_evict_oldest() fills it, so a cache under no
        # global pressure carries an empty ring and put() pays one if for it.
        self._recent_evicted: "OrderedDict[object, float]" = OrderedDict()
        self._thrash_hits = 0

        # Bytes admitted since the last budget notification. This damps the
        # wakes. The hysteresis bounds eviction churn and not wake churn.
        # Without a watermark, warm-up wakes the budget daemon at paint rate,
        # and every wake sums every registrant.
        self._bytes_since_notify = 0

    # Public API. Both subclasses share it byte for byte.

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
                self._stamps[key] = time.monotonic()
            return data

    def put(self, key, data: bytes) -> None:
        if self._max_bytes <= 0:
            return
        notify = False
        with self._lock:
            if key not in self._entries and not self._admit(key):
                return
            if self._recent_evicted and self._was_recently_evicted(key):
                self._thrash_hits += 1
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= len(old)
            self._entries[key] = data
            self._stamps[key] = time.monotonic()
            self._total_bytes += len(data)
            self._bytes_since_notify += len(data)
            while self._total_bytes > self._max_bytes and self._entries:
                self._pop_oldest_locked()
            if self._bytes_since_notify >= cache_budget.NOTIFY_WATERMARK_BYTES:
                self._bytes_since_notify = 0
                notify = True
        # Outside the lock, and only past the watermark. This Event.set() is
        # the entire cost of the global ceiling to a painter thread.
        if notify:
            cache_budget.notify_grew()

    def clear(self) -> None:
        """Drops every cached entry.

        Callers use it wherever the encoded content is orphaned wholesale. A
        background content change orphans every entry, because each one is
        keyed against the old background's pixels and frames. A rotation
        change and a deck teardown do the same. A torn-down deck's caches must
        not keep a dead controller's composited frames until LRU eviction
        reaches them.
        """
        with self._lock:
            self._entries.clear()
            self._stamps.clear()
            self._total_bytes = 0
            self._on_clear_locked()

    # Subclass hooks.

    def _admit(self, key) -> bool:
        """Whether a not-yet-cached key earns a real cache slot on this put().

        The caller holds _lock. The default admits on first sighting.
        EncodedImageCache overrides this with its doorkeeper.
        """
        return True

    def _on_clear_locked(self) -> None:
        """Extra teardown a subclass needs inside clear()'s critical section.
        The caller holds _lock. It does nothing by default."""
        pass

    # Internals.

    def _pop_oldest_locked(self) -> int:
        """Drops the least-recently-used entry and returns its byte size.
        The caller holds _lock."""
        key, evicted = self._entries.popitem(last=False)
        self._stamps.pop(key, None)
        self._total_bytes -= len(evicted)
        return len(evicted)

    def _was_recently_evicted(self, key) -> bool:
        """Thrash check. True when the budget shed key inside the tripwire
        window. The caller holds _lock. This drops an expired entry when it
        meets one, and insert keeps the ring FIFO-bounded, so it never walks.
        """
        stamp = self._recent_evicted.get(key)
        if stamp is None:
            return False
        del self._recent_evicted[key]
        return (time.monotonic() - stamp) <= THRASH_WINDOW_S

    def _note_evicted_locked(self, key) -> None:
        self._recent_evicted[key] = time.monotonic()
        self._recent_evicted.move_to_end(key)
        while len(self._recent_evicted) > THRASH_RING_SIZE:
            self._recent_evicted.popitem(last=False)

    # cache_budget.BudgetParticipant. These let a process-wide manager compare
    # LRU heads across caches and shed from the globally-oldest one. Each takes
    # only this cache's lock, for a popitem-scale critical section, so no
    # painter thread pays cross-cache work.

    def budget_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def budget_head_ts(self) -> float | None:
        """Last-use monotonic of the LRU-oldest entry, or None when empty.
        It is O(1), because _entries iteration order is LRU order and the
        first key is the head."""
        with self._lock:
            for key in self._entries:
                return self._stamps.get(key, 0.0)
            return None

    def budget_evict_oldest(self, want_bytes: int, min_age_s: float, floor_bytes: int) -> int:
        """Sheds one entry, the LRU head, and returns the bytes freed.

        Returns 0 when this cache is at or below floor_bytes, when it is
        empty, or when its head is younger than min_age_s. It sheds one entry
        per call, because the manager re-picks the globally oldest head after
        every eviction, and that granularity makes the cross-cache merge order
        exact. want_bytes is a hint for a future batching policy and not
        license to bulk-shed.
        """
        with self._lock:
            if self._total_bytes <= floor_bytes:
                return 0
            head = next(iter(self._entries), None)
            if head is None:
                return 0
            if (time.monotonic() - self._stamps.get(head, 0.0)) < min_age_s:
                return 0
            freed = self._pop_oldest_locked()
            self._note_evicted_locked(head)
            return freed

    def budget_take_thrash_count(self) -> int:
        """Re-admissions of budget-evicted keys since the last call. The
        budget daemon reads and reports them, because the put path must not
        do I/O."""
        with self._lock:
            hits = self._thrash_hits
            self._thrash_hits = 0
            return hits
