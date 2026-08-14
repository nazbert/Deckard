"""Pins the fuzzy-search contract the seven search call sites rely on.

rapidfuzz returns a float in [0, 100] where fuzzywuzzy returned a rounded int,
so the result shape, the thresholds and the int sort comparator are pinned.
"""
from functools import lru_cache

import fixtures  # imported first, per the harness contract

from rapidfuzz import fuzz

# Asset-like names, in the shape the choosers see, as lowered basenames.
CORPUS = [
    "volume_up",
    "volume_down",
    "volume_mute",
    "brightness_up",
    "spotify_logo",
    "zzz_unrelated_qqq",
]
VOLUME_FAMILY = {"volume_up", "volume_down", "volume_mute"}

# Every threshold used at a call site, so a rapidfuzz bump that shifts the scale
# is caught here rather than as a search that silently returns nothing.
THRESHOLDS = (20, 40, 50)


def test_ratio_returns_float_in_range() -> None:
    for query in ("volume_up", "volume", "brightness"):
        for name in CORPUS:
            score = fuzz.ratio(query, name)
            assert isinstance(score, float), f"{query}/{name}: {type(score)} is not float"
            assert 0.0 <= score <= 100.0, f"{query}/{name}: {score} out of [0, 100]"
    print("PASS: fuzz.ratio returns floats in [0, 100]")


def test_exact_match_and_unrelated_scores() -> None:
    for name in CORPUS:
        assert fuzz.ratio(name, name) == 100.0, f"{name} did not score 100 against itself"

    unrelated = fuzz.ratio("brightness", "volume_down")
    assert unrelated < min(THRESHOLDS), f"unrelated pair scored {unrelated}, not below {min(THRESHOLDS)}"

    # The related family must clear the strictest threshold and an unrelated
    # name must fall under the loosest, so the filters still discriminate.
    for name in VOLUME_FAMILY:
        score = fuzz.ratio("volume", name)
        assert score > max(THRESHOLDS), f"{name} scored {score}, below the 50 chooser threshold"
    assert fuzz.ratio("volume", "brightness_up") < 40, "brightness_up survives the FlowBox threshold"

    print("PASS: exact matches score 100 and unrelated names stay under every call-site threshold")


def test_ranking_order() -> None:
    query = "volume_up"
    scores = {name: fuzz.ratio(query, name) for name in CORPUS}
    ranked = sorted(CORPUS, key=lambda name: -scores[name])

    assert ranked[0] == "volume_up", f"exact match is not first: {ranked}"
    assert set(ranked[:3]) == VOLUME_FAMILY, f"the volume family is not the top 3: {ranked}"

    worst_related = min(scores[name] for name in VOLUME_FAMILY)
    best_unrelated = max(scores[name] for name in CORPUS if name not in VOLUME_FAMILY)
    assert worst_related > best_unrelated, (
        f"related {worst_related} does not outrank unrelated {best_unrelated}"
    )
    print("PASS: ranking puts the exact hit first and the whole related family above the rest")


def test_sort_comparator_still_returns_int() -> None:
    # Mirrors the -1, 0 and 1 comparators in the choosers and the action
    # chooser. They compare float scores and must hand GTK an int.
    def sort_func(name1: str, name2: str, search: str) -> int:
        fuzz1 = fuzz.ratio(name1, search)
        fuzz2 = fuzz.ratio(name2, search)
        if fuzz1 > fuzz2:
            return -1
        if fuzz1 < fuzz2:
            return 1
        return 0

    assert sort_func("volume_up", "spotify_logo", "volume_up") == -1
    assert sort_func("spotify_logo", "volume_up", "volume_up") == 1
    assert sort_func("volume_up", "volume_up", "volume_up") == 0
    for pair in (("volume_up", "spotify_logo"), ("spotify_logo", "volume_up"), ("volume_up", "volume_up")):
        result = sort_func(pair[0], pair[1], "volume")
        assert isinstance(result, int) and not isinstance(result, bool), f"{pair}: {type(result)}"
    print("PASS: float scores still produce int-returning GTK sort comparators")


def test_threshold_boundary_is_rounded() -> None:
    # Pairs whose exact indel ratio is a whole number sitting on a call-site
    # threshold. fuzzywuzzy returned int(round(...)) and let them through, and
    # the rapidfuzz float can land one ULP below, so the store filter and both
    # action-chooser filters compare round(score). Without that, typing n in the
    # action chooser dropped Next Song and p in the store dropped OS Plugin.
    exact = [
        ("n", "next song", 20),
        ("p", "os plugin", 20),
        ("ad", "set default device", 20),
        ("ab", "abcdefgh", 40),
        ("abc", "abcdefghi", 50),
    ]
    for query, name, expected in exact:
        raw = fuzz.ratio(query, name)
        assert round(raw) == expected, f"{query}/{name}: round({raw}) != {expected}"
        # An exact threshold must never come back above it, which would mean
        # rapidfuzz changed the scale rather than its rounding.
        assert raw <= expected, f"{query}/{name}: {raw} overshoots the exact {expected}"
        assert round(raw) >= expected, (
            f"{query}/{name}: rounded score {round(raw)} would be filtered out at {expected}"
        )

    print("PASS: exact-threshold scores survive the filters once rounded")


def test_cached_static_wrapper() -> None:
    # The same shape as PageSelector.calc_ratio. A staticmethod keys the cache
    # on the strings only, never on self.
    class Selector:
        @staticmethod
        @lru_cache(maxsize=1000)
        def calc_ratio(str1, str2) -> float:
            return fuzz.ratio(str1.lower(), str2.lower())

    Selector.calc_ratio.cache_clear()
    first = Selector.calc_ratio("Volume_Up", "volume up")
    second = Selector.calc_ratio("Volume_Up", "volume up")
    assert first == second

    info = Selector.calc_ratio.cache_info()
    assert info.misses == 1, f"expected 1 miss, got {info.misses}"
    assert info.hits == 1, f"expected 1 hit, got {info.hits}"

    # Two instances must share the one cache entry, which is what the
    # staticmethod buys. A bound method would key on self and miss every time.
    Selector().calc_ratio("Volume_Up", "volume up")
    Selector().calc_ratio("Volume_Up", "volume up")
    info = Selector.calc_ratio.cache_info()
    assert info.misses == 1, f"instances added cache misses: {info.misses}"
    assert info.hits == 3, f"expected 3 hits, got {info.hits}"
    print("PASS: the staticmethod + lru_cache wrapper caches on the strings alone")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_fuzzy_ranking")
    test_ratio_returns_float_in_range()
    test_exact_match_and_unrelated_scores()
    test_ranking_order()
    test_sort_comparator_still_returns_int()
    test_threshold_boundary_is_rounded()
    test_cached_static_wrapper()
    print("ALL PASS: scenario_fuzzy_ranking")


if __name__ == "__main__":
    main()
