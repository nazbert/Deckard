"""Unit scenario for the process-wide image-cache budget.

cache_budget caps the sum of the per-deck native-image caches and enforces it
by cross-cache LRU on a lazily spawned daemon, so no painter thread stalls.
"""
import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import gc
import os
import random
import threading
import time

from loguru import logger as log

import src.backend.DeckManagement.Subclasses.byte_lru_cache as byte_lru_cache
import src.backend.DeckManagement.Subclasses.cache_budget as cache_budget
from src.backend.DeckManagement.Subclasses.byte_lru_cache import ByteLRUCache

MIB = 1024 * 1024


class _FakeClock:
    """Stands in for the time module inside byte_lru_cache.

    Last-use stamps, and so min-age and LRU-head comparisons, become exact
    instead of wall-clock racy. cache_budget keeps the real time for damping.
    """

    def __init__(self, start: float = 10_000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _WarningSink:
    def __init__(self):
        self.messages: list[str] = []
        self._id = None

    def __enter__(self) -> "_WarningSink":
        self._id = log.add(lambda m: self.messages.append(str(m)), level="WARNING")
        return self

    def __exit__(self, *exc) -> None:
        log.remove(self._id)

    def matching(self, needle: str) -> list[str]:
        return [m for m in self.messages if needle in m]


def _set_ceiling(value) -> None:
    if value is None:
        os.environ.pop(cache_budget.ENV_CEILING, None)
    else:
        os.environ[cache_budget.ENV_CEILING] = str(value)


def _fill(cache: ByteLRUCache, prefix: str, count: int, size: int) -> None:
    for i in range(count):
        cache.put((prefix, i), bytes(size))


def check_mid_pass_clear_is_noticed() -> None:
    """A pass must re-read the total after something else frees bytes.

    clear() runs wholesale on a background change or a deck teardown, and every
    pick against the stale-high total re-encodes another deck's entry for
    nothing. This check runs before any put crosses the notify watermark.
    """
    clock = _FakeClock()
    byte_lru_cache.time = clock
    _set_ceiling(0.5)  # 512 KiB -> a 486 KiB target
    old = ByteLRUCache(max_bytes=4 * MIB)
    other = ByteLRUCache(max_bytes=4 * MIB)
    entry = 2 * 1024
    try:
        cache_budget.register(old, label="draining:test", floor_bytes=0)
        cache_budget.register(other, label="cleared:test", floor_bytes=0)

        # Both fills stay under NOTIFY_WATERMARK_BYTES, so no put in this check
        # wakes the daemon.
        _fill(old, "o", 300, entry)      # 600 KiB, the only evictable source
        clock.advance(100.0)
        _fill(other, "c", 150, entry)    # 300 KiB, inside min-age

        # The wholesale free, injected mid-pass at the first re-read boundary.
        real_evict = old.budget_evict_oldest
        calls = {"n": 0}

        def evict_then_clear(want_bytes, min_age_s, floor_bytes):
            got = real_evict(want_bytes, min_age_s, floor_bytes)
            calls["n"] += 1
            if calls["n"] == cache_budget.RECHECK_EVERY_PICKS:
                other.clear()
            return got

        old.budget_evict_oldest = evict_then_clear
        cache_budget._drain_once()

        assert calls["n"] == cache_budget.RECHECK_EVERY_PICKS, (
            f"the pass must re-anchor on the live sum and stop: it made "
            f"{calls['n']} picks, not {cache_budget.RECHECK_EVERY_PICKS} -- "
            f"everything past the clear() was over-eviction"
        )
        assert old.total_bytes == 300 * entry - cache_budget.RECHECK_EVERY_PICKS * entry, (
            f"the draining cache lost more than the picks it should have made: "
            f"{old.total_bytes}"
        )
        assert cache_budget.evictable_bytes() <= cache_budget.ceiling_bytes(), (
            "fixture sanity: the freed bytes must actually have put the sum under "
            "the ceiling, or stopping was not the right call"
        )
    finally:
        cache_budget.unregister(old)
        cache_budget.unregister(other)
        byte_lru_cache.time = time
        _set_ceiling(None)

    print("PASS: a pass re-anchors on the live sum instead of over-evicting "
          "against a stale total")


def check_cross_cache_lru_order() -> None:
    """The manager must prefer the globally oldest head, not each cache's own.

    The warm memo of an idle deck has to yield to a painting deck.
    """
    clock = _FakeClock()
    byte_lru_cache.time = clock
    _set_ceiling(1)  # 1 MiB
    cold = ByteLRUCache(max_bytes=4 * MIB)
    hot = ByteLRUCache(max_bytes=4 * MIB)
    try:
        cache_budget.register(cold, label="cold:test", floor_bytes=0)
        cache_budget.register(hot, label="hot:test", floor_bytes=0)

        _fill(cold, "cold", 64, 16 * 1024)   # 1 MiB, stamped at t0
        clock.advance(100.0)
        _fill(hot, "hot", 64, 16 * 1024)     # 1 MiB, stamped at t0 + 100
        assert cold.total_bytes == MIB and hot.total_bytes == MIB, "fixture sanity: 1 MiB each"

        cache_budget._drain_once()

        assert hot.total_bytes == MIB, (
            f"the freshly-used cache lost {MIB - hot.total_bytes} bytes -- entries "
            f"younger than min_age_s must survive while an older cache still has "
            f"anything to give"
        )
        assert cold.total_bytes < MIB, "the aged cache must have been the one to shed"
        assert cold.total_bytes + hot.total_bytes <= MIB, (
            f"the drain must land at or under the ceiling: "
            f"{cold.total_bytes + hot.total_bytes} > {MIB}"
        )

        # Age the survivor past min_age too. It is then eligible, and a tighter
        # ceiling takes from it rather than stalling.
        clock.advance(100.0)
        _set_ceiling(0.25)
        cache_budget._drain_once()
        assert cold.total_bytes + hot.total_bytes <= 0.25 * MIB, (
            "a lowered ceiling must be enforced against every cache once its "
            "entries are past min-age"
        )
        assert cold.total_bytes == 0, (
            "global LRU order must exhaust the OLDER cache before touching the newer one"
        )
    finally:
        cache_budget.unregister(cold)
        cache_budget.unregister(hot)
        byte_lru_cache.time = time
        _set_ceiling(None)

    print("PASS: cross-cache eviction follows global LRU-head order")


def check_min_age_and_floor() -> None:
    """Age protects a hot working set, and no cache is emptied under playback.

    The floor ends the shed here, not the drain target. The only cache old
    enough to shed cannot reach the target alone, so the pass runs it down to
    its floor and stops with the sum still over the ceiling.
    """
    clock = _FakeClock()
    byte_lru_cache.time = clock
    # A 256 KiB ceiling gives a 249 KiB target, and floor_cap is ceiling //
    # (2 * 2 registrants), so 64 KiB. A 64 KiB floor survives the clamp intact
    # and sits far above the reach of the target for one cache.
    _set_ceiling(0.25)
    cache = ByteLRUCache(max_bytes=4 * MIB)
    other = ByteLRUCache(max_bytes=4 * MIB)
    try:
        floor = 64 * 1024
        cache_budget.register(cache, label="floored:test", floor_bytes=floor)
        cache_budget.register(other, label="other:test", floor_bytes=0)

        _fill(cache, "f", 64, 16 * 1024)     # 1 MiB, aged past min-age below
        clock.advance(100.0)
        _fill(other, "y", 32, 16 * 1024)     # 512 KiB, inside min-age all pass
        cache_budget._drain_once()

        assert cache.total_bytes == floor, (
            f"the drain must come to rest exactly ON the floor: {cache.total_bytes} "
            f"!= {floor} (0 means the floor stop-condition never fired)"
        )
        assert other.total_bytes == 512 * 1024, (
            "entries younger than min_age_s must survive even a pass that cannot "
            "reach its target without them"
        )
        assert cache_budget.evictable_bytes() > cache_budget.ceiling_bytes(), (
            f"fixture sanity: the floor -- not the target -- has to be what ended "
            f"this shed, or nothing ever consults the floor: "
            f"{cache_budget.evictable_bytes()} <= {cache_budget.ceiling_bytes()}"
        )

        # Refill both caches with entries younger than min_age_s. Nothing is
        # evictable, so the pass must warn loudly and stop rather than spin or
        # evict the frames being painted this instant.
        cache.clear()
        other.clear()
        _fill(cache, "young", 64, 16 * 1024)
        _fill(other, "young", 64, 16 * 1024)
        evictions_before = cache_budget.eviction_stats()[0]
        degenerate_before = cache_budget.degenerate_pass_count()
        degenerate = cache_budget._drain_once()
        assert degenerate, "a pass with nothing evictable must report itself as degenerate"
        assert cache_budget.eviction_stats()[0] == evictions_before, (
            "nothing may be evicted when every entry is younger than min_age_s"
        )
        # Counted, not read out of the log. The degenerate warning is rate
        # limited to one per WAKE_INTERVAL_S, and that limiter is shared with
        # the live daemon, which can burn it between any two statements here.
        assert cache_budget.degenerate_pass_count() > degenerate_before, (
            "the degenerate case must be counted (and, rate limiter permitting, "
            "warn loudly) rather than pass silently"
        )
        assert cache.total_bytes == MIB and other.total_bytes == MIB, (
            "a degenerate pass must leave every cache untouched"
        )

        # The line itself, pinned apart from the pass. Asked for repeatedly
        # because the shared limiter lets the daemon take the grant this
        # scenario just armed, though not twenty times running.
        emitted: list = []
        with _WarningSink() as sink:
            for _ in range(20):
                cache_budget._last_degenerate_warn_ts = 0.0
                cache_budget._warn_degenerate(2 * MIB, MIB)
                emitted = sink.matching("nothing is evictable")
                if emitted:
                    break
        assert emitted, (
            f"degenerate pressure must be loud; got {sink.messages!r}"
        )
        assert cache_budget.ENV_CEILING in emitted[0], (
            f"the warning has to name the knob that fixes it: {emitted[0]!r}"
        )
    finally:
        cache_budget.unregister(cache)
        cache_budget.unregister(other)
        byte_lru_cache.time = time
        _set_ceiling(None)

    print("PASS: min-age protects a hot working set, floors hold, degenerate pressure warns and stops")


def check_lifecycle() -> None:
    """The budget must track reality with no bookkeeping of its own.

    A clear() is visible at once, and a dead cache costs nothing. The registry
    is weak, so a torn-down deck needs no unregister call.
    """
    cache = ByteLRUCache(max_bytes=4 * MIB)
    cache_budget.register(cache, label="lifecycle:test")
    _fill(cache, "l", 16, 16 * 1024)
    assert cache_budget.totals().get("lifecycle") == 256 * 1024, (
        f"totals() must reflect what is held: {cache_budget.totals()!r}"
    )

    cache.clear()
    assert cache_budget.totals().get("lifecycle") == 0, (
        "clear() must return its bytes to the budget instantly -- there is no "
        "counter for the manager to keep coherent, it sums the caches"
    )

    del cache
    gc.collect()
    assert "lifecycle" not in cache_budget.totals(), (
        "a dropped cache must fall out of the weak registry -- a strong ref here "
        "would keep a torn-down deck's caches (and everything they reference) alive"
    )

    print("PASS: clear() is visible immediately and a dropped cache leaves the registry")


class _BoomThreading:
    """Stands in for the threading module inside one _ensure_thread() call.

    Only Thread is looked up there, so a two-line shim covers it. Swapping the
    module reference keeps the failure injection out of the real threading
    module, which every other thread in this process uses at the same time.
    """

    @staticmethod
    def Thread(*args, **kwargs):
        raise RuntimeError("can't start new thread")


def check_thread_latch_survives_failed_spawn() -> None:
    """A failed daemon spawn must not leave the started latch standing.

    _ensure_thread() is the only place that creates the daemon, and register()
    swallows what escapes it, so a latched failure kills enforcement silently
    for the life of the process.
    """
    saved_started = cache_budget._thread_started
    saved_threading = cache_budget.threading
    try:
        cache_budget._thread_started = False
        cache_budget.threading = _BoomThreading
        with _WarningSink() as sink:
            cache_budget._ensure_thread()
        assert not cache_budget._thread_started, (
            "a spawn that raised must release the latch so the next register() "
            "retries; latched, nothing ever enforces the ceiling again"
        )
        assert sink.matching("could not start"), (
            f"a failed spawn must say so; got {sink.messages!r}"
        )
    finally:
        cache_budget.threading = saved_threading
        # Restore rather than leave False. The daemon spawned by the first
        # register() of this scenario is still running, and the storm check
        # below needs that one, not a second.
        cache_budget._thread_started = saved_started

    print("PASS: a failed budget-thread spawn releases the latch instead of "
          "disabling enforcement forever")


def check_env_contract() -> None:
    """A malformed value must degrade with a warning and never raise.

    DeckController.__init__ reads this and swallows an exception as a failed
    deck initialization.
    """
    saved = os.environ.get(cache_budget.ENV_CEILING)
    try:
        _set_ceiling(None)
        assert cache_budget.ceiling_bytes() == cache_budget.default_ceiling_bytes()

        _set_ceiling(8)
        assert cache_budget.ceiling_bytes() == 8 * MIB, "the env value must be honored, re-read per call"

        cache_budget._warned_ceiling_values.discard("two-fifty-six")
        _set_ceiling("two-fifty-six")
        with _WarningSink() as sink:
            assert cache_budget.ceiling_bytes() == cache_budget.default_ceiling_bytes(), (
                "a malformed ceiling must fall back to the default, not raise"
            )
            assert sink.matching("malformed"), f"expected a warning, got {sink.messages!r}"
            # Read on every pass, so the warning must not become 60 s log spam.
            cache_budget.ceiling_bytes()
            assert len(sink.matching("malformed")) == 1, (
                "a malformed ceiling must warn once per distinct value, not per read"
            )

        # The values float() accepts that int() cannot take. Each parses, and
        # nan even survives the mb <= 0 sign test, because every nan comparison
        # is False. Without an explicit finiteness test each one reaches
        # int(mb * MiB) and raises, ValueError for nan and OverflowError for
        # inf. On the daemon that is a warning every 5 s with enforcement
        # silently off. On the __init__ path it is a lost deck.
        for hostile in ("nan", "inf", "-inf", "1e400"):
            cache_budget._warned_ceiling_values.discard(hostile)
            _set_ceiling(hostile)
            with _WarningSink() as sink:
                assert cache_budget.ceiling_bytes() == cache_budget.default_ceiling_bytes(), (
                    f"a non-finite ceiling ({hostile!r}) must fall back to the default, "
                    f"not raise"
                )
                assert sink.matching("malformed"), (
                    f"a non-finite ceiling ({hostile!r}) must warn like any other "
                    f"malformed value; got {sink.messages!r}"
                )

        # 0 disables global eviction. The local caps still bound each cache, so
        # the sum stays bounded by the sum of the local caps.
        _set_ceiling(0)
        cache = ByteLRUCache(max_bytes=4 * MIB)
        try:
            cache_budget.register(cache, label="disabled:test", min_age_s=0.0, floor_bytes=0)
            _fill(cache, "d", 128, 16 * 1024)  # 2 MiB, far over any sane ceiling
            evictions_before = cache_budget.eviction_stats()[0]
            cache_budget._drain_once()
            assert cache.total_bytes == 2 * MIB, "a disabled budget must never evict"
            assert cache_budget.eviction_stats()[0] == evictions_before
        finally:
            cache_budget.unregister(cache)
    finally:
        if saved is None:
            os.environ.pop(cache_budget.ENV_CEILING, None)
        else:
            os.environ[cache_budget.ENV_CEILING] = saved

    print("PASS: DECKARD_IMAGE_CACHE_MB parsing (default / explicit / malformed / disable)")


def check_bound_under_concurrent_load() -> None:
    """The bound proof over four caches, four threads and one 1 MiB ceiling.

    Overshoot is bounded by put rate times wake latency, the trade for never
    stalling the writer, so the in-storm assertion carries slack. Sampling
    starts once enforcement engages, because a cold start is not yet bounded.
    """
    _set_ceiling(1)
    ceiling = MIB
    slack = 2 * MIB
    caches = [ByteLRUCache(max_bytes=4 * MIB) for _ in range(4)]
    for i, cache in enumerate(caches):
        # min_age 0 and a tiny floor. This check is about the bound, and the
        # age and floor protections have their own check above.
        cache_budget.register(cache, label=f"hammer{i}:test", min_age_s=0.0, floor_bytes=4096)

    stop = threading.Event()
    errors: list = []
    peak = {"bytes": 0}
    violations: list = []

    def hammer(cache, seed: int) -> None:
        rng = random.Random(seed)
        n = 0
        try:
            while not stop.is_set():
                n += 1
                cache.put((seed, n), bytes(rng.randint(1024, 8 * 1024)))
                # Exactly what ByteLRUCache.put does in production once the
                # watermark is wired, an Event.set() and nothing more.
                if n % 8 == 0:
                    cache_budget.notify_grew()
                time.sleep(0.002)
        except Exception as e:
            errors.append(e)

    def poll() -> None:
        try:
            while not stop.is_set():
                total = cache_budget.evictable_bytes()
                peak["bytes"] = max(peak["bytes"], total)
                if total > ceiling + slack:
                    violations.append(total)
                time.sleep(0.005)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(c, i), name=f"hammer-{i}", daemon=True)
               for i, c in enumerate(caches)]
    poller = threading.Thread(target=poll, name="budget-poller", daemon=True)
    try:
        for t in threads:
            t.start()
        evictions_before = cache_budget.eviction_stats()[0]
        engaged = fixtures.wait_until(
            lambda: (cache_budget.eviction_stats()[0] > evictions_before
                     and cache_budget.evictable_bytes() <= ceiling + slack),
            timeout=20)
        assert engaged, (
            f"the budget never engaged under load: the sum sat at "
            f"{cache_budget.evictable_bytes() // 1024} KiB against a "
            f"{ceiling // 1024} KiB ceiling after "
            f"{cache_budget.eviction_stats()[0] - evictions_before} evictions"
        )
        poller.start()
        time.sleep(2.0)
        stop.set()
        for t in threads + [poller]:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads + [poller]), "hammer threads wedged"
        assert not errors, f"hammering raised: {errors!r}"
        assert not violations, (
            f"the sum exceeded ceiling + overshoot slack "
            f"({(ceiling + slack) // 1024} KiB) {len(violations)} time(s); "
            f"worst {max(violations) // 1024} KiB"
        )
        assert peak["bytes"] > ceiling // 2, (
            f"fixture sanity: the storm never filled the caches (peak "
            f"{peak['bytes'] // 1024} KiB) -- the bound would be vacuous"
        )

        # Quiescent now. One more wake, then the sum must be at or under the
        # ceiling, with no slack.
        cache_budget.notify_grew()
        settled = fixtures.wait_until(
            lambda: cache_budget.evictable_bytes() <= ceiling, timeout=10)
        assert settled, (
            f"the budget failed to settle under the ceiling once puts stopped: "
            f"{cache_budget.evictable_bytes() // 1024} KiB > {ceiling // 1024} KiB"
        )
        assert cache_budget.eviction_stats()[0] > 0, (
            "fixture sanity: the ceiling should have forced real evictions"
        )
        print(f"PASS: Σ image caches stayed within the ceiling + overshoot bound under "
              f"concurrent load (peak {peak['bytes'] // 1024} KiB, ceiling "
              f"{ceiling // 1024} KiB) and settled to "
              f"{cache_budget.evictable_bytes() // 1024} KiB")
    finally:
        stop.set()
        for cache in caches:
            cache_budget.unregister(cache)
        _set_ceiling(None)


def main() -> None:
    fixtures.start_watchdog(120, label="scenario_cache_budget")

    # The deterministic checks run first, on a fake clock and a synchronous
    # _drain_once(). The daemon spawned by the first register() acts only on a
    # wake, and the one check that counts individual picks runs before any fill
    # crosses the notify watermark, so nothing has woken it when it matters.
    check_mid_pass_clear_is_noticed()
    check_cross_cache_lru_order()
    check_min_age_and_floor()
    check_lifecycle()
    check_thread_latch_survives_failed_spawn()
    check_env_contract()
    check_bound_under_concurrent_load()

    print("PASS: scenario_cache_budget")


if __name__ == "__main__":
    main()
