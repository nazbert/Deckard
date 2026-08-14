"""
Scenario for the tier-mixing guard.

The unit tier and the integration tier install different, incompatible gl.*
graphs, so each installer raises RuntimeError when the other tier is already
live. This scenario sets the fixtures tier flags directly to drive both
directions in one process, which no ordinary scenario does.
"""
import fixtures


def test_integration_then_stub_raises() -> None:
    fixtures._install_integration_globals()
    try:
        fixtures.install_stub_globals()
    except RuntimeError as e:
        assert "INTEGRATION tier is already installed" in str(e), str(e)
        print("PASS: install_stub_globals() refuses when integration tier is live")
    else:
        raise AssertionError("install_stub_globals() must refuse after the integration tier")


def test_stub_then_integration_raises() -> None:
    # Reset the tier flags so this direction starts clean in the same process.
    fixtures._integration_globals_installed = False
    fixtures._stub_globals_installed = False

    fixtures.install_stub_globals()
    try:
        fixtures._install_integration_globals()
    except RuntimeError as e:
        assert "UNIT tier" in str(e), str(e)
        print("PASS: _install_integration_globals() refuses when unit tier is live")
    else:
        raise AssertionError("the integration installer must refuse after the unit tier")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_tier_guard")
    test_integration_then_stub_raises()
    test_stub_then_integration_raises()
    print("ALL PASS: scenario_tier_guard")


if __name__ == "__main__":
    main()
