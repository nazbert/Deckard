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

The byte-capped LRU shared by both native-image caches (gl#142).

`EncodedImageCache` (pixel-hash keys + doorkeeper) and `NativeTileCache`
(frame-identity keys + kill switch) were the same OrderedDict-as-LRU with the
same byte accounting, twice. This is that core once; the subclasses supply
only their admission policy and their teardown bookkeeping.

Cost on the paint path, which is the sole device writer's thread and the
reason this class exists in this shape:

    get() hit   -- one dict lookup, one move_to_end, one float store.
    put()       -- the same, plus the local-cap eviction loop, plus one
                   `Event.set()` per ~1 MiB admitted (see cache_budget's
                   wake damping). Nothing outside this instance's lock.
    eviction    -- `popitem`-scale, one lock, never nested with another.

Last-use stamps live OUT OF BAND, in a parallel `dict[key, float]`, never as
`(data, ts)` tuples inside `_entries`. `_entries` values stay the exact
`bytes` object that was put -- callers (and the pinned scenarios) introspect
them and assert identity across a hit -- and a cache hit allocates nothing.

Instances additionally implement `cache_budget.BudgetParticipant`, which is
how a process-wide manager compares LRU heads across caches and sheds from
the globally-oldest one without ever holding two cache locks at once. See
`cache_budget`'s module docstring for the ceiling, the overshoot bound, and
the disabled/degenerate behaviors.
"""
import threading
import time
from collections import OrderedDict

from src.backend.DeckManagement.Subclasses import cache_budget

# How long an evicted key stays in the thrash tripwire ring, and how many
# keys the ring holds. Both are bookkeeping-only (a key + a float each): the
# ring exists so a re-admission shortly after a GLOBAL eviction is countable,
# which is the only way a ceiling that binds against a live working set is
# distinguishable in the field from one that is merely trimming cold entries
# (plan-142 §3.4).
THRASH_WINDOW_S = 30.0
THRASH_RING_SIZE = 256


class ByteLRUCache:
    """LRU of immutable `bytes` values, capped by total byte size.

    The shared core of `EncodedImageCache` (pixel-hash keys + doorkeeper) and
    `NativeTileCache` (frame-identity keys + kill switch): an `OrderedDict`
    whose iteration order IS the LRU order (`move_to_end` on every hit and
    every put), exact byte accounting, and one instance lock. Subclasses add
    their own admission policy via `_admit()` and their own teardown
    bookkeeping via `_on_clear_locked()`; neither overrides `get`/`put`.

    Values must be immutable bytes: they are handed out by reference, so
    refcounting -- not this cache -- is what keeps a paint that already holds
    one alive across an eviction. That is why eviction is never a correctness
    concern here, only a cost one (plan-142 §3.4).

    LAST-USE STAMPS ARE OUT OF BAND -- a parallel `dict[key, float]`, never
    a `(data, ts)` tuple inside `_entries`. Two reasons, in order of
    importance: `_entries` values stay the raw `bytes` object that was put
    (callers and pinned scenarios introspect them and assert identity across
    a hit), and a hit costs one float store into an existing dict slot rather
    than a fresh tuple allocation on the paint path.

    The `budget_*` methods implement the `cache_budget.BudgetParticipant`
    protocol: they let a process-wide manager compare LRU heads across caches
    and shed from the globally-oldest one. Every one of them takes only THIS
    cache's lock, for a `popitem`-scale critical section -- no painter thread
    ever pays cross-cache work (plan-142 §3.1).
    """

    def __init__(self, max_bytes: int) -> None:
        # <= 0 disables: get() always misses and put() stores nothing, so
        # callers fall back to whatever their uncached path is with no extra
        # branching of their own (see NativeTileCache's env kill switch).
        self._max_bytes = max(0, max_bytes)
        self._lock = threading.Lock()
        self._entries: "OrderedDict[object, bytes]" = OrderedDict()
        # key -> last-use monotonic. Same key set as _entries, always
        # mutated under _lock alongside it.
        self._stamps: dict[object, float] = {}
        self._total_bytes = 0

        # Thrash tripwire: keys the BUDGET (not the local cap) evicted
        # recently, and how many of them came straight back. Populated only
        # by budget_evict_oldest(), so a cache under no global pressure
        # carries an empty ring and put() pays a single `if` for it.
        self._recent_evicted: "OrderedDict[object, float]" = OrderedDict()
        self._thrash_hits = 0

        # Bytes admitted since the last budget notification (wake damping,
        # plan-142 §3.2). Hysteresis bounds eviction churn, not WAKE churn:
        # without a watermark, warm-up would wake the budget daemon at paint
        # rate and every wake would sum every registrant.
        self._bytes_since_notify = 0

    # --- public API (byte-identical across both subclasses) ---------------

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
        # Outside the lock, and only past the watermark: the entire cost of
        # the global ceiling to a painter thread is this Event.set().
        if notify:
            cache_budget.notify_grew()

    def clear(self) -> None:
        """Drops every cached entry. Called wherever the content the entries
        were encoded from is orphaned wholesale: a background content change
        (every entry is keyed against the OLD background's pixels/frames), a
        rotation change, or a deck teardown -- a torn-down deck's caches must
        not keep referencing a dead controller's composited frames until LRU
        eviction eventually gets around to them."""
        with self._lock:
            self._entries.clear()
            self._stamps.clear()
            self._total_bytes = 0
            self._on_clear_locked()

    # --- subclass hooks ---------------------------------------------------

    def _admit(self, key) -> bool:
        """Whether a not-yet-cached `key` earns a real cache slot on this
        put(). Called with `_lock` held. Default: admit on first sighting --
        `EncodedImageCache` overrides this with its doorkeeper."""
        return True

    def _on_clear_locked(self) -> None:
        """Extra teardown a subclass needs inside clear()'s critical
        section. Called with `_lock` held; no-op by default."""
        pass

    # --- internals --------------------------------------------------------

    def _pop_oldest_locked(self) -> int:
        """Drops the least-recently-used entry, returning its byte size.
        Called with `_lock` held."""
        key, evicted = self._entries.popitem(last=False)
        self._stamps.pop(key, None)
        self._total_bytes -= len(evicted)
        return len(evicted)

    def _was_recently_evicted(self, key) -> bool:
        """Thrash check, called with `_lock` held: was `key` shed by the
        budget within the tripwire window? Expired entries are dropped as
        they are encountered, and the ring is FIFO-bounded on insert, so
        this never walks."""
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

    # --- cache_budget.BudgetParticipant -----------------------------------

    def budget_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def budget_head_ts(self) -> float | None:
        """Last-use monotonic of the LRU-oldest entry, or None if empty.
        O(1): `_entries` iteration order is LRU order, so the first key is
        the head."""
        with self._lock:
            for key in self._entries:
                return self._stamps.get(key, 0.0)
            return None

    def budget_evict_oldest(self, want_bytes: int, min_age_s: float, floor_bytes: int) -> int:
        """Sheds ONE entry -- the LRU head -- and returns the bytes freed,
        or 0 if this cache is at/below `floor_bytes`, empty, or its head is
        younger than `min_age_s`.

        One entry per call by design: the manager re-picks the globally
        oldest head after every eviction, and that granularity is what makes
        the cross-cache merge order exact. `want_bytes` is a hint for a
        future batching policy, deliberately not license to bulk-shed
        (plan-142 §3.1)."""
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
        """Re-admissions of budget-evicted keys since the last call. Read
        (and reported) by the budget daemon rather than logged inline: the
        put path must not do I/O."""
        with self._lock:
            hits = self._thrash_hits
            self._thrash_hits = 0
            return hits
