"""
Scenario (#69 tier-mixing guard): the unit tier (install_stub_globals /
make_stub_controller) and the integration tier (make_headless_controller /
_install_integration_globals) install different, incompatible gl.* graphs.
Mixing them in one process used to be silently order-dependent. Each installer
must now refuse loudly (RuntimeError) when the OTHER tier is already live.

This scenario proves both directions fire. It manipulates fixtures' internal
tier flags directly to test the second direction in the same process without a
real DeckController -- something no normal scenario does (normal scenarios pick
one tier and never touch these flags).
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
