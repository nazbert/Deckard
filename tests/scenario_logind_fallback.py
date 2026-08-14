"""Detector selection for the systemd-logind fallback.

LockScreenManager.setup() matches components of XDG_CURRENT_DESKTOP, and an
else branch picks the logind detector for a desktop no branch matches.
"""
import os

import fixtures  # must be first; isolates DATA_PATH


def make_manager():
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

    # Bypass __init__, which spawns the setup thread at once. This scenario
    # sets the environment first and observes each selection synchronously.
    manager = LockScreenManager.__new__(LockScreenManager)
    manager.locked = False
    manager.detector = None
    return manager


def select_with_env(desktop: str | None):
    if desktop is None:
        os.environ.pop("XDG_CURRENT_DESKTOP", None)
    else:
        os.environ["XDG_CURRENT_DESKTOP"] = desktop
    manager = make_manager()
    manager.setup()
    return manager.detector


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_logind_fallback")

    from src.backend.LockScreenManager.Detectors.Gnome import GnomeLockScreenDetector
    from src.backend.LockScreenManager.Detectors.Logind import LogindLockScreenDetector

    detector = select_with_env("niri")
    assert isinstance(detector, LogindLockScreenDetector), (
        f"niri must select the logind fallback, got {type(detector).__name__}"
    )
    print("PASS: unmatched environment (niri) selects the logind fallback")

    detector = select_with_env("ubuntu:GNOME")
    assert isinstance(detector, GnomeLockScreenDetector), (
        f"ubuntu:GNOME must still select the Gnome detector, "
        f"got {type(detector).__name__}"
    )
    print("PASS: ubuntu:GNOME still selects the Gnome detector (regression guard)")

    detector = select_with_env(None)
    assert isinstance(detector, LogindLockScreenDetector), (
        f"unset XDG_CURRENT_DESKTOP must select the logind fallback, "
        f"got {type(detector).__name__}"
    )
    print("PASS: unset XDG_CURRENT_DESKTOP selects the logind fallback")

    print("PASS: scenario_logind_fallback")


if __name__ == "__main__":
    main()
