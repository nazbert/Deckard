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

Process-wide budget for the native-image caches (gl#142).

Each deck's `encode_memo` and `native_tile_cache` is byte-capped on its own,
but nothing capped their SUM: total image-cache RAM scaled with deck count,
and eviction was per-silo, so a cold deck's full memo never yielded a byte to
a hot one no matter how hard the hot one was thrashing. This module adds the
missing aggregate: a ceiling over Σ(evictable caches) and a cross-cache LRU
that sheds from whichever cache owns the globally-oldest entry.

WHAT A PAINTER THREAD PAYS
    Nothing but its own per-cache lock, plus one `Event.set()` per ~1 MiB of
    admitted bytes. There is no global lock, no cross-cache walk, and no
    cross-deck coordination anywhere on the paint path -- that constraint is
    what shaped every other decision here. Caches keep their own exact LRU;
    this module only compares their heads.

THE OVERSHOOT BOUND (the "provably bounded" claim, stated honestly)
    Enforcement is DEFERRED, so the sum is not bounded by the ceiling at
    every instant -- it is bounded by

        ceiling + (put rate x wake latency)

    Between the put that crosses the ceiling and the daemon being scheduled,
    painters keep putting: a painter can complete a whole tick of up to
    key-count puts in that window. With ~5-20 KiB native key JPEGs, and the
    notify fired on the growing put itself (so the window is one scheduler
    latency plus the damping interval, not the 60 s periodic pass), that is
    KBs to low MBs in practice. Steady state, once puts stop, settles at or
    below the ceiling. Strict synchronous enforcement was rejected: it would
    put cross-cache work on the sole device writer's thread.

WHEN IT DOES NOTHING
    - Ceiling 0 (`DECKARD_IMAGE_CACHE_MB=0`, or negative): global eviction
      is disabled entirely. Every cache's own cap still applies, so the sum
      is still bounded -- by Σ(local caps), which is what shipped before this
      module existed.
    - Malformed ceiling: warns once per distinct value and uses the default.
      This is read on the DeckController.__init__ path; it must never raise.
    - Degenerate pressure (over the ceiling, but every registrant is at its
      floor or entirely younger than its min-age): the pass warns loudly and
      STOPS. Getting here means the live working set is physically larger
      than the ceiling, so evicting anyway would only re-encode the frames
      being painted right now. Σ(local caps) still bounds the sum, the
      operator gets a log line naming the knob, and the thrash tripwire
      counts any key that comes straight back.

WHY EVICTION IS NEVER A CORRECTNESS RISK
    Cache values are immutable `bytes` handed out by reference, so
    refcounting keeps any paint that already holds one alive across an
    eviction. Evicting the wrong entry costs one re-encode, never a wrong or
    torn frame. That is why there is no pin API.
"""
import math
import os
import threading
import time
from weakref import WeakSet

from loguru import logger as log

# --------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------- #

ENV_CEILING = "DECKARD_IMAGE_CACHE_MB"

# Default ceiling: MemTotal/64, clamped. 256 MiB on any host with >= 16 GiB.
# Deliberately NON-BINDING for a typical single-deck rig (one deck's local
# caps sum to 96 MiB), so the default-on deliverable is attribution -- the
# mem_telemetry columns and eviction counters that make the ceiling tunable
# against field data -- with the enforcement mechanism fully built and armed
# by one env var (plan-142 §2).
DEFAULT_CEILING_MB = 256
MIN_DEFAULT_CEILING_MB = 64
MEM_TOTAL_DIVISOR = 64

# Per-registrant defaults, overridable at register().
DEFAULT_MIN_AGE_S = 2.0
DEFAULT_FLOOR_BYTES = 4 * 1024 * 1024

# Evict down to this fraction of the ceiling, so a hot loop that keeps
# crossing the line doesn't get one eviction pass per put.
TARGET_FRACTION = 0.95

# Periodic pass: self-heals drift (a cache that shrank, a registrant that
# died) and is the beat the census/telemetry reads ride on.
WAKE_INTERVAL_S = 60.0
# Wake damping: hysteresis bounds eviction churn, not WAKE churn -- without
# this, warm-up would wake the daemon at paint rate and each wake would sum
# every registrant.
MIN_WAKE_INTERVAL_S = 0.05
# After a pass that found nothing evictable (everything under min-age, or
# everything at floor), back off harder: notifications keep arriving at
# paint rate and there is provably nothing this daemon can do about them
# until entries age.
DEGENERATE_BACKOFF_S = 5.0
# A put must grow its own cache by this much before it is worth a wake.
NOTIFY_WATERMARK_BYTES = 1024 * 1024

LOG_INTERVAL_S = 5.0

# --------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------- #

_lock = threading.Lock()
# Every registrant, weakly: a cache dies with the DeckController (or media
# asset) that owns it, and this registry must not be what keeps either
# alive. The label lives ON the cache object rather than in a side dict --
# a dict[cache, label] would hold a strong ref and defeat the WeakSet
# exactly the way plan-178's lane registry documents (event_dispatch.py).
# Snapshotted as a list under _lock before iteration: WeakSet tolerates
# GC-driven removal mid-iteration, but not a concurrent add.
_registry: "WeakSet" = WeakSet()

_wake = threading.Event()
_thread_started = False

_evictions = 0
_evicted_bytes = 0

_default_ceiling_cache: int = None
_warned_ceiling_values: set = set()


# --------------------------------------------------------------------- #
# Ceiling
# --------------------------------------------------------------------- #

def _mem_total_bytes() -> int:
    """MemTotal from /proc/meminfo, or None. House precedent for /proc
    reads: mem_telemetry.py."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_ceiling_bytes() -> int:
    """RAM-derived default. Cached: MemTotal does not change, and this is
    read on every ceiling_bytes() call."""
    global _default_ceiling_cache
    if _default_ceiling_cache is None:
        total = _mem_total_bytes()
        if not total:
            _default_ceiling_cache = DEFAULT_CEILING_MB * 1024 * 1024
        else:
            _default_ceiling_cache = max(
                MIN_DEFAULT_CEILING_MB * 1024 * 1024,
                min(DEFAULT_CEILING_MB * 1024 * 1024, total // MEM_TOTAL_DIVISOR),
            )
    return _default_ceiling_cache


def ceiling_bytes() -> int:
    """Process-wide ceiling on the sum of the EVICTABLE caches, from
    DECKARD_IMAGE_CACHE_MB. 0 (or negative) disables global eviction --
    every cache's own local cap still applies, so the sum stays bounded by
    Σ(local caps). A malformed value degrades to the default with a warning
    (once per distinct value; this is read on every pass): it must never
    raise out of DeckController.__init__, where DeckManager would swallow it
    as "Failed to initialize deck".

    "Malformed" includes the values float() ACCEPTS but int() cannot take:
    "nan", "inf", and any overflowing literal ("1e400"). Those parse fine and
    then explode two lines down (int(nan) raises ValueError, int(inf) raises
    OverflowError) -- from the daemon that would read as a "pass failed" log
    line every 5 s forever with enforcement silently off, and from
    DeckController.__init__ as a lost deck.

    Re-read from os.environ on every call rather than snapshotted at import,
    mirroring native_tile_cache_max_bytes() -- the env legs of the scenarios
    have to be able to change it inside one process."""
    raw = os.environ.get(ENV_CEILING)
    if raw is None:
        return default_ceiling_bytes()
    try:
        mb = float(raw)
        usable = math.isfinite(mb)
    except ValueError:
        usable = False
    if not usable:
        if raw not in _warned_ceiling_values:
            _warned_ceiling_values.add(raw)
            log.warning(
                f"Ignoring malformed {ENV_CEILING}={raw!r}; using the default "
                f"{default_ceiling_bytes() // (1024 * 1024)} MiB"
            )
        return default_ceiling_bytes()
    if mb <= 0:
        return 0
    return int(mb * 1024 * 1024)


# --------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------- #

def register(cache, *, label: str, evictable: bool = True,
             min_age_s: float = DEFAULT_MIN_AGE_S,
             floor_bytes: int = DEFAULT_FLOOR_BYTES) -> None:
    """Enrols `cache` in the process-wide budget. Idempotent.

    An evictable registrant must implement the whole participant surface
    (`budget_bytes`, `budget_head_ts`, `budget_evict_oldest`); an
    accounting-only one (`evictable=False`) needs only `budget_bytes()` and
    is never asked to shed -- it is in the registry so its footprint shows
    up in `totals()` and the telemetry CSV.

    `label` is `group:instance` (e.g. `encode_memo:AB123`): `totals()` sums
    by group, logs name the instance.

    Never raises. This runs from DeckController.__init__, where any
    exception is swallowed by DeckManager as "Failed to initialize deck" and
    the whole device is silently skipped -- a telemetry/housekeeping feature
    must not be able to cost a user their deck."""
    try:
        cache.budget_label = label
        cache.budget_evictable = bool(evictable)
        cache.budget_min_age_s = float(min_age_s)
        cache.budget_floor_bytes = int(floor_bytes)
        with _lock:
            _registry.add(cache)
        _ensure_thread()
    except Exception as e:
        log.warning(f"cache-budget: could not register {label!r}: {e}")


def unregister(cache) -> None:
    """Drops `cache` from the registry. Optional -- a dropped cache falls
    out of the WeakSet on its own, and a cleared one reports 0 bytes -- but
    the media holders call it from close() so a torn-down reader stops being
    counted the instant it is closed rather than at the next GC."""
    try:
        with _lock:
            _registry.discard(cache)
    except Exception as e:
        log.warning(f"cache-budget: could not unregister: {e}")


def set_min_age(cache, min_age_s: float) -> None:
    """Retunes a registrant's min-age protection in place.

    Group-A entries are keyed PER FRAME, so a given entry is re-touched once
    per content-loop period, not once per tick: a flat 2 s would leave a
    playing video's frame set eligible for eviction exactly one loop before
    it is needed again, silently reinstating the per-frame encode the frame
    identity cache exists to avoid. The tile cache therefore tracks the
    active loop duration (plan-142 §3.4)."""
    try:
        cache.budget_min_age_s = float(min_age_s)
    except Exception as e:
        log.warning(f"cache-budget: could not set min_age: {e}")


def notify_grew() -> None:
    """Called (after releasing its own lock) by a cache that has grown past
    its notify watermark. Just an Event.set(): the painter thread never does
    any budget work of its own."""
    _wake.set()


# --------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------- #

def _snapshot() -> list:
    with _lock:
        return list(_registry)


def totals() -> dict[str, int]:
    """label GROUP -> bytes currently held, summed across instances. Read by
    mem_telemetry and by the scenarios."""
    out: dict[str, int] = {}
    for cache in _snapshot():
        group = str(getattr(cache, "budget_label", "?")).split(":", 1)[0]
        try:
            out[group] = out.get(group, 0) + int(cache.budget_bytes())
        except Exception:
            continue
    return out


def evictable_bytes() -> int:
    """Σ bytes over the evictable registrants -- the quantity the ceiling
    governs."""
    total = 0
    for cache in _snapshot():
        if not getattr(cache, "budget_evictable", False):
            continue
        try:
            total += int(cache.budget_bytes())
        except Exception:
            continue
    return total


def eviction_stats() -> tuple[int, int]:
    """(cumulative entries evicted by the budget, cumulative bytes freed).
    Both are monotonic for the life of the process.

    Read under `_lock` because the writes are: `_drain_once()` is documented
    as drivable from any thread (the daemon drives it, the scenarios call it
    directly), so `+=` on a module global is a read-modify-write two passes
    can interleave and a torn pair is what telemetry would then report."""
    with _lock:
        return _evictions, _evicted_bytes


# --------------------------------------------------------------------- #
# The budget thread
# --------------------------------------------------------------------- #

def _ensure_thread() -> None:
    global _thread_started
    if _thread_started:
        return
    with _lock:
        if _thread_started:
            return
        _thread_started = True
    threading.Thread(target=_budget_loop, name="cache_budget", daemon=True).start()


def _budget_loop() -> None:
    # Daemon, abandoned at quit like every other housekeeping thread
    # (app.py joins non-daemon threads only) -- no shutdown ceremony.
    next_allowed = 0.0
    while True:
        _wake.wait(timeout=WAKE_INTERVAL_S)
        # Damp BEFORE clearing, so notifications that arrive during the
        # damping sleep are subsumed by the pass that is about to run.
        delay = next_allowed - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _wake.clear()
        try:
            degenerate = _drain_once()
        except Exception as e:
            # One bad pass must cost one pass: this thread is spawned
            # exactly once and never respawned, so an escaping exception
            # would end budget enforcement for the life of the process.
            log.warning(f"cache-budget: pass failed: {e}")
            degenerate = True
        next_allowed = time.monotonic() + (
            DEGENERATE_BACKOFF_S if degenerate else MIN_WAKE_INTERVAL_S
        )


_last_log_ts = 0.0
_last_degenerate_warn_ts = 0.0


def _drain_once() -> bool:
    """One enforcement pass. Returns True when the pass could not make
    progress (nothing evictable) and the caller should back off harder.

    Also driven synchronously by the scenarios, which is why the whole pass
    is a plain function with no thread affinity of its own."""
    ceiling = ceiling_bytes()
    caches = [c for c in _snapshot() if getattr(c, "budget_evictable", False)]
    _report_thrash(caches)
    if ceiling <= 0 or not caches:
        return True

    total = 0
    for cache in caches:
        total += int(cache.budget_bytes())
    if total <= ceiling:
        return False

    before = total
    target = int(ceiling * TARGET_FRACTION)
    # Σ(floors) must never be able to exceed half the ceiling, or a
    # many-deck rig (and every small test ceiling) would leave nothing
    # evictable and this manager would be silently inert.
    floor_cap = max(0, ceiling // (2 * len(caches)))

    # Per-pass skip set: a cache that is at its floor, entirely younger than
    # its min-age, or empty is out for the rest of this pass. Every loop
    # iteration therefore either strictly decreases `total` or grows `skip`
    # -- both finite, so the drain provably terminates. ids are stable here:
    # `caches` holds strong references for the duration.
    skip: set = set()
    freed = 0
    evicted = 0
    while total > target:
        pick = None
        pick_ts = None
        for cache in caches:
            if id(cache) in skip:
                continue
            head_ts = cache.budget_head_ts()
            if head_ts is None:
                skip.add(id(cache))
                continue
            if pick_ts is None or head_ts < pick_ts:
                pick, pick_ts = cache, head_ts
        if pick is None:
            break
        floor = min(int(getattr(pick, "budget_floor_bytes", DEFAULT_FLOOR_BYTES)), floor_cap)
        got = pick.budget_evict_oldest(
            total - target,
            float(getattr(pick, "budget_min_age_s", DEFAULT_MIN_AGE_S)),
            floor,
        )
        if got <= 0:
            # Head too young, or at floor: nothing more to take from this
            # one this pass.
            skip.add(id(pick))
            continue
        total -= got
        freed += got
        evicted += 1

    global _evictions, _evicted_bytes
    with _lock:
        _evictions += evicted
        _evicted_bytes += freed

    if evicted:
        _log_evictions(evicted, freed, before, total, ceiling)
        return False

    _warn_degenerate(before, ceiling)
    return True


def _log_evictions(evicted: int, freed: int, before: int, after: int, ceiling: int) -> None:
    global _last_log_ts
    now = time.monotonic()
    if now - _last_log_ts < LOG_INTERVAL_S:
        return
    _last_log_ts = now
    mb = 1024 * 1024
    log.info(
        f"cache-budget: evicted {evicted} entries / {freed / mb:.1f} MiB "
        f"(sum {before / mb:.1f}->{after / mb:.1f} of {ceiling / mb:.1f} MiB)"
    )


def _warn_degenerate(total: int, ceiling: int) -> None:
    """Over the ceiling with nothing evictable: every registrant is at its
    floor or entirely younger than its min-age. That requires a live working
    set physically larger than the ceiling protects, so evicting anyway
    would just re-encode the frames being painted this instant. Stop, and
    be loud about it -- the local caps still bound the sum at Σ(local caps),
    and the operator needs to raise the ceiling."""
    global _last_degenerate_warn_ts
    now = time.monotonic()
    if now - _last_degenerate_warn_ts < WAKE_INTERVAL_S:
        return
    _last_degenerate_warn_ts = now
    mb = 1024 * 1024
    log.warning(
        f"cache-budget: {total / mb:.1f} MiB of image caches over a {ceiling / mb:.1f} MiB "
        f"ceiling, but nothing is evictable (every cache at its floor or younger "
        f"than its min-age). The live working set is larger than the ceiling; "
        f"raise {ENV_CEILING} or reduce the number of decks/pages in play."
    )


def _report_thrash(caches: list) -> None:
    """Thrash tripwire: a key that came straight back after the budget shed
    it means the ceiling is binding against a live working set -- the
    failure mode is a re-encode per frame, never corruption, but it must
    never be silent. Counted on the cache (a dict lookup on the put path),
    reported here so the put path never does I/O."""
    for cache in caches:
        take = getattr(cache, "budget_take_thrash_count", None)
        if take is None:
            continue
        try:
            hits = take()
        except Exception:
            continue
        if hits:
            log.warning(
                f"cache-budget: {getattr(cache, 'budget_label', '?')} re-admitted "
                f"{hits} key(s) shortly after the budget evicted them -- the ceiling "
                f"is thrashing against a live working set (raise {ENV_CEILING})"
            )
