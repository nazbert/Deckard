"""
Scenario (#136): the AssetManager asset choosers' search behaviour, pinned as
pure logic.

`IconChooserPage`, `WallpaperChooserPage` and `SDPlusBarWallpaperChooserPage`
each carried their own ~20-line copy of `filter_func`/`sort_func` (identical
`fuzz.ratio` scoring keyed on the asset's `path`). They now share one
implementation on `GenericAssetChooserPage`, built on the module-level
`asset_matches_search` / `compare_assets` helpers.

What must not drift, for ALL THREE asset types:

  1. Name keying -- the search matches the file's basename WITHOUT its
     directory or extension, and every type keys on the same `path`
     attribute (`ASSET_PATH_ATTR`).
  2. Empty query -- keeps every asset and sorts plain (case-sensitive)
     alphabetically by that name, so "Zebra" precedes "apple".
  3. Non-empty query -- keeps only names scoring >= 50 and orders by
     DESCENDING score; equal scores tie (0), which leaves the input order
     alone because DynamicFlowBox sorts with `sorted` (stable).
  4. The comparator returns `int` -- GTK's sort contract -- even though
     rapidfuzz scores are floats.
  5. The three chooser classes resolve to the SAME bound implementation, so
     a future edit cannot silently diverge one asset type from the others.

The end-to-end orderings are asserted through `functools.cmp_to_key`, exactly
the way `DynamicFlowBox.sort_items` applies the comparator.

Pure functions: no GTK widget, no display, no deck.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import functools
import types

from src.windows.AssetManager.GenericAssetChooser import (
    GenericAssetChooserPage,
    SEARCH_SCORE_THRESHOLD,
    asset_display_name,
    asset_matches_search,
    compare_assets,
)
from src.windows.AssetManager.IconPacks.Icons.IconChooser import IconChooserPage
from src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper.SDPlusBarWallpaperChooser import (
    SDPlusBarWallpaperChooserPage,
)
from src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser import WallpaperChooserPage


CHOOSER_CLASSES = {
    "icons": IconChooserPage,
    "wallpapers": WallpaperChooserPage,
    "sd+bar wallpapers": SDPlusBarWallpaperChooserPage,
}


def asset(path: str):
    """An asset stand-in: the choosers only ever read `.path` off one."""
    return types.SimpleNamespace(path=path)


# Deliberately mixed directories, extensions and capitalisation.
CORPUS = [
    asset("/packs/icons/volume_up.png"),
    asset("/packs/icons/volume_down.svg"),
    asset("/other/brightness.jpeg"),
    asset("/packs/icons/Zebra.png"),
    asset("apple.gif"),
]
NAMES = ["volume_up", "volume_down", "brightness", "Zebra", "apple"]

# rapidfuzz scores for the queries below (recomputed here as documentation,
# asserted as orderings rather than as raw values so a scoring bump surfaces
# as a ranking change, not as a brittle float mismatch):
#   "volume" -> volume_up 80.0, volume_down 70.6, apple 36.4, Zebra 18.2,
#               brightness 12.5
#   "bright" -> brightness 75.0, Zebra 36.4, everything else 0.0


def make_page(cls, search: str):
    """A chooser instance reduced to what filter_func/sort_func read."""
    page = cls.__new__(cls)
    page.search_entry = types.SimpleNamespace(get_text=lambda: search)
    return page


def names_of(items) -> list[str]:
    return [asset_display_name(item) for item in items]


def apply_chooser(page, items) -> list[str]:
    """filter + sort exactly as DynamicFlowBox.get_items_to_show does."""
    kept = [item for item in items if page.filter_func(item)]
    ordered = sorted(kept, key=functools.cmp_to_key(page.sort_func))
    return names_of(ordered)


def test_display_name_strips_dir_and_extension() -> None:
    assert asset_display_name(asset("/packs/icons/volume_up.png")) == "volume_up"
    assert asset_display_name(asset("apple.gif")) == "apple"
    assert asset_display_name(asset("/no/extension/here")) == "here"
    # Only the LAST extension goes (upstream behaviour: os.path.splitext).
    assert asset_display_name(asset("/a/archive.tar.gz")) == "archive.tar"
    print("PASS: the search name is the basename without its extension")


def test_all_three_types_key_on_path() -> None:
    for label, cls in CHOOSER_CLASSES.items():
        assert cls.ASSET_PATH_ATTR == "path", f"{label} keys on {cls.ASSET_PATH_ATTR!r}"
    print("PASS: all three asset types key the search on .path")


def test_all_three_types_share_one_implementation() -> None:
    filters = {cls.filter_func for cls in CHOOSER_CLASSES.values()}
    sorts = {cls.sort_func for cls in CHOOSER_CLASSES.values()}
    assert filters == {GenericAssetChooserPage.filter_func}, (
        f"asset types no longer share one filter_func: {filters}")
    assert sorts == {GenericAssetChooserPage.sort_func}, (
        f"asset types no longer share one sort_func: {sorts}")
    print("PASS: the three chooser classes share one filter_func/sort_func")


def test_empty_query_keeps_everything_and_sorts_alphabetically() -> None:
    expected = ["Zebra", "apple", "brightness", "volume_down", "volume_up"]
    for label, cls in CHOOSER_CLASSES.items():
        page = make_page(cls, "")
        got = apply_chooser(page, CORPUS)
        assert got == expected, f"{label}: empty-query order {got} != {expected}"
    # Case-sensitivity is the load-bearing detail: "Zebra" sorts BEFORE
    # "apple" because the empty-query branch compares raw names.
    assert compare_assets(asset("Zebra.png"), asset("apple.png"), "") == -1
    # An unchanged name pair ties, so `sorted` keeps the input order.
    assert compare_assets(asset("/a/x.png"), asset("/b/x.svg"), "") == 0
    print("PASS: an empty query keeps every asset and sorts it alphabetically")


def test_query_filters_below_threshold() -> None:
    for label, cls in CHOOSER_CLASSES.items():
        page = make_page(cls, "volume")
        kept = names_of([i for i in CORPUS if page.filter_func(i)])
        assert kept == ["volume_up", "volume_down"], f"{label}: kept {kept}"

        page = make_page(cls, "bright")
        kept = names_of([i for i in CORPUS if page.filter_func(i)])
        assert kept == ["brightness"], f"{label}: kept {kept}"

        # No match at all -> empty grid (not "everything", the empty-query path).
        page = make_page(cls, "zzzz")
        assert [i for i in CORPUS if page.filter_func(i)] == [], f"{label}: zzzz matched"
    assert SEARCH_SCORE_THRESHOLD == 50, "the chooser threshold moved"
    print("PASS: a query keeps only names scoring at least 50")


def test_query_orders_by_descending_score() -> None:
    for label, cls in CHOOSER_CLASSES.items():
        page = make_page(cls, "volume")
        got = apply_chooser(page, CORPUS)
        assert got == ["volume_up", "volume_down"], f"{label}: order {got}"

    # The comparator ranks the closer name first regardless of input order,
    # and is antisymmetric.
    up, down = asset("volume_up.png"), asset("volume_down.png")
    assert compare_assets(up, down, "volume") == -1
    assert compare_assets(down, up, "volume") == 1
    # Directory and extension never enter the score.
    assert compare_assets(asset("/deep/dir/volume_up.png"),
                          asset("volume_up.svg"), "volume") == 0
    print("PASS: a query orders assets by descending fuzzy score")


def test_comparator_returns_int() -> None:
    pairs = [(CORPUS[0], CORPUS[1]), (CORPUS[1], CORPUS[0]), (CORPUS[0], CORPUS[0])]
    for search in ("", "volume", "zzzz"):
        for a, b in pairs:
            result = compare_assets(a, b, search)
            assert isinstance(result, int) and not isinstance(result, bool), (
                f"comparator returned {type(result).__name__} for {search!r} "
                f"-- GTK's sort contract wants an int")
    print("PASS: the sort comparator returns ints (GTK's sort contract)")


def test_helpers_and_methods_agree() -> None:
    """The bound methods must be exactly the module helpers over the search
    entry -- no per-class fixup sneaking back in."""
    for search in ("", "volume", "bright", "zzzz"):
        page = make_page(IconChooserPage, search)
        for item in CORPUS:
            assert page.filter_func(item) == asset_matches_search(item, search)
        for a in CORPUS:
            for b in CORPUS:
                assert page.sort_func(a, b) == compare_assets(a, b, search)
    print("PASS: the chooser methods are the shared helpers over the search entry")


def main() -> int:
    fixtures.start_watchdog(60, label="scenario_asset_chooser_logic")

    test_display_name_strips_dir_and_extension()
    test_all_three_types_key_on_path()
    test_all_three_types_share_one_implementation()
    test_empty_query_keeps_everything_and_sorts_alphabetically()
    test_query_filters_below_threshold()
    test_query_orders_by_descending_score()
    test_comparator_returns_int()
    test_helpers_and_methods_agree()

    print("ALL PASS: scenario_asset_chooser_logic")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
