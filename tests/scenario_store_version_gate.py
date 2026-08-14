"""
Regression test for the minimum-app-version gate.

StoreData.is_min_app_version_satisfied is the one implementation, and it
compares inclusively, so an asset requiring exactly the running version is
compatible. StorePreview delegates to it.
"""

# No GTK widget is built here, because the method never touches self.
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
    # the preview build. It fails open like the None case, with a warning.
    assert is_min_app_version_satisfied("not-a-version") is True


def test_verdict_matches_runtime_gate_on_suffixed_versions() -> None:
    """The store badge must agree with the runtime plugin loader.

    PluginBase.is_minimum_version_ok compares base versions, with the pre-
    release, post-release, dev and local suffixes stripped.
    """
    # A raw parsed compare diverges on a pre-release build, where an asset
    # pinned to the release loads at runtime but reads as incompatible.
    running = version.parse(gl.app_version)
    base = running.base_version  # e.g. "1.5.0" for a "1.5.0-beta.15" build

    def runtime_gate_says(minimum: str) -> bool:
        # Mirror of PluginBase.is_minimum_version_ok / _get_parsed_base_version.
        if minimum is None:
            return True
        min_base = version.parse(version.parse(minimum).base_version)
        app_base = version.parse(base)
        return app_base >= min_base

    for minimum in (
        base,             # the plain release, where a beta build diverges
        f"{base}.post1",  # post-release suffix
        f"{base}rc1",     # pre-release suffix on the same base
        "0.0.1",
        f"{version.parse(base).major + 1}.0.0",  # genuinely newer -> incompatible
    ):
        assert is_min_app_version_satisfied(minimum) == runtime_gate_says(minimum), (
            f"store badge and runtime gate disagree on min={minimum!r} "
            f"(running {gl.app_version!r})"
        )

    # Spell out the concrete case, so a regression names itself. On a
    # pre-release build, requiring exactly the release must display as
    # compatible, because it loads at runtime.
    if running.is_prerelease:
        assert is_min_app_version_satisfied(base) is True, (
            f"on pre-release build {gl.app_version!r}, an asset requiring the "
            f"release {base!r} must display compatible -- the runtime loader loads it"
        )


def test_preview_delegates_to_helper() -> None:
    from src.windows.Store.Preview import StorePreview

    # The method never dereferences self, so an unbound call needs no GTK
    # widget. Equality must pass, where a strict comparison returns False.
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
    test_verdict_matches_runtime_gate_on_suffixed_versions()
    test_preview_delegates_to_helper()
    test_duplicate_copies_are_gone()
    print("scenario_store_version_gate: PASS")


if __name__ == "__main__":
    main()
