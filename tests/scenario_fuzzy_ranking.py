"""
Scenario (#170 rapidfuzz swap): pins the fuzzy-search contract the app's seven
search call sites rely on, now that they import `rapidfuzz.fuzz` instead of
`fuzzywuzzy.fuzz`.

fuzzywuzzy returned a rounded `int`; rapidfuzz returns a `float` in [0, 100].
Every consumer was audited to be float-safe, so what actually has to keep
holding is the *shape* of the results, not their exact values:

  1. `fuzz.ratio` returns a float in [0, 100].
  2. An exact match scores 100; clearly unrelated strings score below every
     threshold the call sites use (20 in StorePageSection/ActionChooser, 40 in
     CustomAssets/FlowBox, 50 in the Icon/Wallpaper/SDPlusBar choosers and
     PageSelector).
  3. Ranking a small asset-like corpus puts the exact hit first and the whole
     related family above every unrelated entry.
  4. A GTK sort comparator built on those float scores still returns `int`
     (GTK's sort contract), even though the scores it compares are floats.
  5. The `@staticmethod` + `@lru_cache` wrapper shape used by
     `PageSelector.calc_ratio` still caches (it keys on the strings only).
  6. A score that is *mathematically* exactly a call-site threshold survives
     the filter. rapidfuzz computes an exact 20 as 19.999999999999996 (one ULP
     low), so the two `>= 20` filters compare `round(score)` -- fuzzywuzzy's
     own `intr()` semantics -- instead of the raw float.

Pure functions: no GTK, no globals install, no deck.
"""
from functools import lru_cache

import fixtures  # imported first, per the harness contract

from rapidfuzz import fuzz

# Asset-like names, in the shape the choosers actually see (basenames, lowered).
CORPUS = [
    "volume_up",
    "volume_down",
    "volume_mute",
    "brightness_up",
    "spotify_logo",
    "zzz_unrelated_qqq",
]
VOLUME_FAMILY = {"volume_up", "volume_down", "volume_mute"}

# Every threshold used at a call site, so a rapidfuzz bump that shifts the
# scale gets caught here rather than as "search silently returns nothing".
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

    # The related family must clear the strictest threshold, and an unrelated
    # name must fall under the loosest -- i.e. the filters still discriminate.
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
    # Mirrors the -1/0/1 comparators in IconChooser/WallpaperChooser/FlowBox/
    # ActionChooser: they compare float scores but must hand GTK an int.
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
    # threshold. fuzzywuzzy returned int(round(...)) and let them through;
    # rapidfuzz's float can land one ULP below (19.999999999999996 for an exact
    # 20), so StorePageSection.filter_func and both ActionChooser filters
    # compare round(score). Without that, typing "n" in the action chooser
    # dropped "Next Song", and "p" in the store dropped "OS Plugin".
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
        # An exact threshold must never come back *above* it -- that would mean
        # rapidfuzz changed the scale, not just its rounding.
        assert raw <= expected, f"{query}/{name}: {raw} overshoots the exact {expected}"
        assert round(raw) >= expected, (
            f"{query}/{name}: rounded score {round(raw)} would be filtered out at {expected}"
        )

    print("PASS: exact-threshold scores survive the filters once rounded")


def test_cached_static_wrapper() -> None:
    # Same shape as PageSelector.calc_ratio: a staticmethod so the cache keys
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

    # Two instances must share the one cache entry -- that's the point of the
    # staticmethod. A bound method would key on self and miss every time.
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
