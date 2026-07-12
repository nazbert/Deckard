"""
Regression test for issue #54 (check_required_version off-by-one):

The store's minimum-app-version gate existed as four near-identical copies
(StorePage, PluginPage, StorePreview, PluginPreview), every one comparing
with a strict `min_version < app_version` -- an asset requiring EXACTLY the
running app version was flagged incompatible (red border in the store).

Now there is a single implementation (StoreData.is_min_app_version_satisfied,
inclusive `<=`); StorePreview delegates to it and the dead/duplicate copies
on StorePage / PluginPage / PluginPreview are gone, so the logic cannot
diverge again. Headless: no GTK widgets are instantiated -- class-level
checks and unbound calls only (the method never touches self).
"""
import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from packaging import version

from src.windows.Store.StoreData import is_min_app_version_satisfied


def test_helper_gate_semantics() -> None:
    app = version.parse(gl.app_version)

    assert is_min_app_version_satisfied(None) is True, "no requirement -> compatible"
    assert is_min_app_version_satisfied("0.0.1") is True, "older requirement -> compatible"
    assert is_min_app_version_satisfied(gl.app_version) is True, (
        f"an asset requiring exactly the running version ({gl.app_version}) "
        "must be compatible -- this was the off-by-one"
    )
    newer = f"{app.major + 1}.0.0"
    assert is_min_app_version_satisfied(newer) is False, "newer requirement -> incompatible"

    # A garbage version string from a remote catalog must not raise out of
    # the preview build; fail open like the None case, with a warning.
    assert is_min_app_version_satisfied("not-a-version") is True


def test_preview_delegates_to_helper() -> None:
    from src.windows.Store.Preview import StorePreview

    # The method never dereferences self -- call it unbound so no GTK widget
    # has to exist. Equality must now pass (strict `<` returned False here).
    assert StorePreview.check_required_version(None, gl.app_version) is True
    assert StorePreview.check_required_version(None, None) is True
    app = version.parse(gl.app_version)
    assert StorePreview.check_required_version(None, f"{app.major + 1}.0.0") is False


def test_duplicate_copies_are_gone() -> None:
    from src.windows.Store.StorePage import StorePage
    from src.windows.Store.Plugins.PluginPage import PluginPage, PluginPreview
    from src.windows.Store.Preview import StorePreview

    assert "check_required_version" not in vars(StorePage), (
        "StorePage must no longer carry its own copy of the version gate"
    )
    assert "check_required_version" not in vars(PluginPage), (
        "PluginPage must no longer carry its own copy of the version gate"
    )
    assert "check_required_version" not in vars(PluginPreview), (
        "PluginPreview must inherit StorePreview's gate, not duplicate it"
    )
    assert PluginPreview.check_required_version is StorePreview.check_required_version


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_version_gate")
    test_helper_gate_semantics()
    test_preview_delegates_to_helper()
    test_duplicate_copies_are_gone()
    print("scenario_store_version_gate: PASS")


if __name__ == "__main__":
    main()
