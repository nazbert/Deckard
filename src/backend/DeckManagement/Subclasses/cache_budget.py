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

Process-wide budget for the native-image caches.

Each deck's encode_memo and native_tile_cache carries its own byte cap, but
nothing caps their sum. Total image-cache RAM then scales with deck count, and
eviction stays per-silo, so a cold deck's full memo yields no byte to a hot one
however hard the hot one thrashes. This module adds the aggregate, a ceiling
over the sum of the evictable caches and a cross-cache LRU that sheds from
whichever cache owns the globally-oldest entry.

What a painter thread pays
    Nothing but its own per-cache lock, plus one Event.set() per about 1 MiB
    of admitted bytes. There is no global lock, no cross-cache walk and no
    cross-deck coordination on the paint path. That constraint shaped every
    other decision here. Caches keep their own exact LRU, and this module only
    compares their heads.

The overshoot bound
    Enforcement is deferred, so the ceiling does not bound the sum at every
    instant. The real bound is

        ceiling + (put rate x wake latency)

    Painters keep putting between the put that crosses the ceiling and the
    scheduling of the daemon. A painter can complete a whole tick of up to
    key-count puts in that window. Native key JPEGs run about 5-20 KiB, and
    the growing put fires the notify itself, so the window is one scheduler
    latency plus the damping interval and not the 60 s periodic pass. That is
    KBs to low MBs in practice. Once puts stop, the steady state settles at or
    below the ceiling. Strict synchronous enforcement is rejected, because it
    puts cross-cache work on the sole device writer's thread.

When it does nothing
    - A ceiling of 0 (DECKARD_IMAGE_CACHE_MB=0, or negative) disables global
      eviction. Every cache's own cap still applies, so the sum of the local
      caps still bounds the total.
    - A malformed ceiling warns once per distinct value and uses the default.
      The DeckController.__init__ path reads this, so it must never raise.
    - Degenerate pressure means the total is over the ceiling and every
      registrant sits at its floor or is entirely younger than its min-age.
      The pass warns loudly and stops. Reaching that state means the live
      working set is physically larger than the ceiling, so an eviction only
      re-encodes the frames the painter draws right now. The sum of the local
      caps still bounds the total, the operator gets a log line naming the
      knob, and the thrash tripwire counts any key that comes straight back.

Why eviction is never a correctness risk
    Cache values are immutable bytes handed out by reference, so refcounting
    keeps any paint that already holds one alive across an eviction. An
    eviction of the wrong entry costs one re-encode and never a wrong or torn
    frame. That is why there is no pin API.
"""
import math
import os
import threading
import time
from weakref import WeakSet

from loguru import logger as log

# Tunables.

ENV_CEILING = "DECKARD_IMAGE_CACHE_MB"

# Default ceiling of MemTotal/64, clamped. That is 256 MiB on any host with
# 16 GiB or more. It does not bind on a typical single-deck rig, whose local
# caps sum to 96 MiB, so the default-on deliverable is attribution. The
# mem_telemetry columns and the eviction counters make the ceiling tunable
# against field data, and one env var arms the enforcement mechanism.
DEFAULT_CEILING_MB = 256
MIN_DEFAULT_CEILING_MB = 64
MEM_TOTAL_DIVISOR = 64

# Per-registrant defaults, overridable at register().
DEFAULT_MIN_AGE_S = 2.0
DEFAULT_FLOOR_BYTES = 4 * 1024 * 1024
# Upper bound on a retuned min-age (set_min_age). It also installs when the
# right value is not yet knowable, see
# DeckController.refresh_tile_cache_min_age. Over-protecting a cache costs at
# most a stale entry surviving a pass, while under-protecting it costs the
# re-encode the cache exists to remove.
MAX_MIN_AGE_S = 30.0

# Evict down to this fraction of the ceiling, so a hot loop that keeps
# crossing the line doesn't get one eviction pass per put.
TARGET_FRACTION = 0.95

# The periodic pass self-heals drift, e.g. a cache that shrank or a registrant
# that died. It is also the beat the census and telemetry reads ride on.
WAKE_INTERVAL_S = 60.0
# Wake damping. The hysteresis bounds eviction churn and not wake churn.
# Without it, warm-up wakes the daemon at paint rate, and each wake sums every
# registrant.
MIN_WAKE_INTERVAL_S = 0.05
# Back off harder after a pass that found nothing evictable, that is
# everything under min-age or everything at floor. Notifications keep arriving
# at paint rate, and this daemon can provably do nothing about them until
# entries age.
DEGENERATE_BACKOFF_S = 5.0
# A put must grow its own cache by this much before it is worth a wake.
NOTIFY_WATERMARK_BYTES = 1024 * 1024

# Entries one pass sheds before it yields. Each pick is a head scan over every
# registrant plus a per-cache lock. An unbounded pass against a large deficit,
# such as a ceiling lowered under a warm multi-deck rig with tens of thousands
# of native key JPEGs, is one long uninterruptible burst contending with every
# painter's put. Capped, the same work happens in MIN_WAKE_INTERVAL_S-spaced
# slices, because the pass re-arms the wake before it returns and the next one
# continues where this one stopped. The cap is sized so the common case, a
# page change of a few hundred entries, is never split.
MAX_PICKS_PER_PASS = 2000

# How often, in picks, a pass re-reads the live sum instead of trusting its
# own running subtraction. A clear() elsewhere in the process, from a
# background change or a deck teardown, drops a whole cache at once and frees
# bytes the pass cannot see. Every pick made against the stale-high total
# after that evicts another deck's entry for no reason. The re-read costs one
# budget_bytes() lock per registrant, once per this many evictions.
RECHECK_EVERY_PICKS = 64

LOG_INTERVAL_S = 5.0

# Module state.

_lock = threading.Lock()
# Every registrant, held weakly. A cache dies with the DeckController or media
# asset that owns it, and this registry must not keep either alive. The label
# lives on the cache object and not in a side dict, because a dict of cache to
# label holds a strong ref and defeats the WeakSet, the same way the lane
# registry in event_dispatch.py documents. A pass snapshots the set as a list
# under _lock before it iterates, because a WeakSet tolerates a GC-driven
# removal mid-iteration but not a concurrent add.
_registry: "WeakSet" = WeakSet()

_wake = threading.Event()
_thread_started = False

_evictions = 0
_evicted_bytes = 0
_degenerate_passes = 0

_default_ceiling_cache: int | None = None
_warned_ceiling_values: set = set()


# Ceiling.

def _mem_total_bytes() -> int | None:
    """MemTotal from /proc/meminfo, or None. mem_telemetry.py sets the house
    precedent for /proc reads."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_ceiling_bytes() -> int:
    """RAM-derived default. The value is cached, because MemTotal does not
    change and every ceiling_bytes() call reads it."""
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
    """Process-wide ceiling on the sum of the evictable caches, read from
    DECKARD_IMAGE_CACHE_MB.

    0 or a negative value disables global eviction. Every cache's own local
    cap still applies, so the sum of the local caps still bounds the total. A
    malformed value degrades to the default and warns once per distinct value,
    because every pass reads this. It must never raise out of
    DeckController.__init__, where DeckManager reports it as "Failed to
    initialize deck".

    Malformed includes the values float() accepts and int() cannot take:
    "nan", "inf", and any overflowing literal such as "1e400". Those parse and
    then raise two lines down, ValueError for int(nan) and OverflowError for
    int(inf). From the daemon that reads as a "pass failed" log line every 5 s
    forever with enforcement silently off, and from DeckController.__init__ as
    a lost deck.

    Every call re-reads os.environ instead of a snapshot taken at import, the
    same as native_tile_cache_max_bytes(). The env legs of the scenarios must
    be able to change it inside one process.
    """
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


# Registration.

def register(cache, *, label: str, evictable: bool = True,
             min_age_s: float = DEFAULT_MIN_AGE_S,
             floor_bytes: int = DEFAULT_FLOOR_BYTES) -> None:
    """Enrols cache in the process-wide budget. Idempotent.

    An evictable registrant must implement the whole participant surface,
    which is budget_bytes, budget_head_ts and budget_evict_oldest. An
    accounting-only registrant (evictable=False) needs only budget_bytes() and
    never sheds. It sits in the registry so its footprint shows up in totals()
    and in the telemetry CSV.

    label has the form group:instance, e.g. encode_memo:AB123. totals() sums
    by group, and logs name the instance.

    This never raises. It runs from DeckController.__init__, where DeckManager
    reports any exception as "Failed to initialize deck" and skips the whole
    device. A telemetry and housekeeping feature must not cost a user a deck.
    """
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
    """Drops cache from the registry.

    The call is optional. A dropped cache falls out of the WeakSet on its own,
    and a cleared one reports 0 bytes. The media holders still call it from
    close(), so a torn-down reader stops counting the instant it closes rather
    than at the next GC.
    """
    try:
        with _lock:
            _registry.discard(cache)
    except Exception as e:
        log.warning(f"cache-budget: could not unregister: {e}")


def set_min_age(cache, min_age_s: float) -> None:
    """Retunes a registrant's min-age protection in place.

    Group-A entries are keyed per frame, so a given entry is re-touched once
    per content-loop period and not once per tick. A flat 2 s leaves a playing
    video's frame set eligible for eviction exactly one loop before it is
    needed again, which silently reinstates the per-frame encode the frame
    identity cache exists to avoid. The tile cache therefore tracks the active
    loop duration.
    """
    try:
        cache.budget_min_age_s = float(min_age_s)
    except Exception as e:
        log.warning(f"cache-budget: could not set min_age: {e}")


def notify_grew() -> None:
    """A cache calls this after it releases its own lock and after it grows
    past its notify watermark. It is one Event.set(), because the painter
    thread never does budget work of its own."""
    _wake.set()


# Introspection.

def _snapshot() -> list:
    with _lock:
        return list(_registry)


def totals() -> dict[str, int]:
    """Maps each label group to the bytes it holds, summed across instances.
    mem_telemetry and the scenarios read it."""
    out: dict[str, int] = {}
    for cache in _snapshot():
        group = str(getattr(cache, "budget_label", "?")).split(":", 1)[0]
        try:
            out[group] = out.get(group, 0) + int(cache.budget_bytes())
        except Exception:
            continue
    return out


def evictable_bytes() -> int:
    """Total bytes over the evictable registrants. The ceiling governs this
    quantity."""
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

    The read takes _lock, because the writes do. Any thread can drive
    _drain_once(). The daemon drives it, and the scenarios call it directly,
    so a += on a module global is a read-modify-write that two passes
    interleave, and telemetry then reports a torn pair.
    """
    with _lock:
        return _evictions, _evicted_bytes


def degenerate_pass_count() -> int:
    """Passes that found real pressure but nothing to evict.

    The count includes the ones whose warning the log rate-limiter swallowed,
    which is one line per WAKE_INTERVAL_S shared by every caller of
    _drain_once(). It is monotonic for the life of the process. The scenarios
    assert on it rather than on the log, which cannot separate "not
    degenerate" from "throttled".
    """
    with _lock:
        return _degenerate_passes


# The budget thread.

def _ensure_thread() -> None:
    """Spawns the budget daemon on the first register(), once per process.

    One caller claims the latch under the lock, so exactly one caller spawns,
    and it releases the latch again when the spawn fails. This is the only
    place that creates the daemon, and register() swallows what escapes it. A
    latch left standing over a failed start(), such as a thread-limit
    RuntimeError under memory pressure, leaves enforcement dead for the life
    of the process with nothing to retry it. Released, the next registrant
    tries again.
    """
    global _thread_started
    if _thread_started:
        return
    with _lock:
        if _thread_started:
            return
        _thread_started = True
    try:
        threading.Thread(target=_budget_loop, name="cache_budget", daemon=True).start()
    except Exception as e:
        with _lock:
            _thread_started = False
        log.warning(f"cache-budget: could not start the budget thread: {e}")


def _budget_loop() -> None:
    # A daemon thread, abandoned at quit like every other housekeeping thread.
    # app.py joins non-daemon threads only, so there is no shutdown ceremony.
    next_allowed = 0.0
    while True:
        _wake.wait(timeout=WAKE_INTERVAL_S)
        # Damp before the clear, so the pass about to run absorbs the
        # notifications that arrive during the damping sleep.
        delay = next_allowed - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        _wake.clear()
        try:
            degenerate = _drain_once()
        except Exception as e:
            # One bad pass costs one pass. This thread spawns exactly once and
            # never respawns, so an escaping exception ends budget enforcement
            # for the life of the process.
            log.warning(f"cache-budget: pass failed: {e}")
            degenerate = True
        next_allowed = time.monotonic() + (
            DEGENERATE_BACKOFF_S if degenerate else MIN_WAKE_INTERVAL_S
        )


_last_log_ts = 0.0
_last_degenerate_warn_ts = 0.0


def _drain_once() -> bool:
    """One enforcement pass.

    Returns True when the pass made no progress, that is when nothing was
    evictable, and the caller must back off harder. The scenarios also drive
    it synchronously, which is why the whole pass is a plain function with no
    thread affinity of its own.
    """
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
    # The sum of the floors must never exceed half the ceiling. Otherwise a
    # many-deck rig, and every small test ceiling, leaves nothing evictable
    # and this manager goes silently inert.
    floor_cap = max(0, ceiling // (2 * len(caches)))

    # The per-pass skip set holds a cache that is at its floor, entirely
    # younger than its min-age, or empty. Such a cache is out for the rest of
    # this pass, so every loop iteration either strictly decreases total or
    # grows skip, and both are finite. The periodic re-read below can push
    # total back up, because painters keep putting, so MAX_PICKS_PER_PASS and
    # not that argument alone makes termination unconditional. The ids are
    # stable here, because caches holds strong references for the duration.
    skip: set = set()
    freed = 0
    evicted = 0
    picks = 0
    while total > target:
        if picks >= MAX_PICKS_PER_PASS:
            # Slice the burst (see MAX_PICKS_PER_PASS) and re-arm the wake, so
            # the next pass picks up where this one stopped. That pass is one
            # damping interval away and not one 60 s periodic away. This pass
            # still reports progress, so the caller uses the short interval
            # and not the degenerate backoff.
            _wake.set()
            break
        picks += 1
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
            # The head is too young, or the cache is at its floor. Take
            # nothing more from it this pass.
            skip.add(id(pick))
            continue
        total -= got
        freed += got
        evicted += 1
        if picks % RECHECK_EVERY_PICKS == 0:
            # Re-anchor on the live sum. total is a running subtraction, and a
            # clear() elsewhere, from a background change or a deck teardown,
            # frees bytes it cannot see. A long pass then keeps shedding other
            # decks' entries against a total that is already stale-high.
            total = evictable_bytes()

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
    """Over the ceiling with nothing evictable.

    Every registrant sits at its floor or is entirely younger than its
    min-age. That requires a live working set physically larger than the
    ceiling protects, so an eviction only re-encodes the frames the painter
    draws this instant. Stop, and be loud about it. The sum of the local caps
    still bounds the total, and the operator needs to raise the ceiling.

    The count is kept unconditionally, before the log rate-limiter. The
    limiter stops a daemon that backs off every 5 s from repeating itself 12
    times a minute, which also means the log cannot answer whether a pass was
    degenerate.
    """
    global _degenerate_passes
    with _lock:
        _degenerate_passes += 1

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
    """Thrash tripwire. A key that comes straight back after the budget shed
    it means the ceiling binds against a live working set.

    The failure mode is a re-encode per frame and never corruption, but it
    must never be silent. The cache counts it, which is a dict lookup on the
    put path, and this function reports it, so the put path never does I/O.
    """
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
