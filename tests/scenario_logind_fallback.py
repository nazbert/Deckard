"""
Detector selection for the systemd-logind fallback.

LockScreenManager.setup() picks a DE-specific detector by matching the
components of XDG_CURRENT_DESKTOP (the value is a colon-separated
list like "ubuntu:GNOME", so component matching, not whole-string equality).
The setup appends an `else:` fallback -- the logind detector -- for
environments no DE branch matches (Niri, Sway, river, ...).

Three selections, driven deterministically by calling setup() directly:

  1. XDG_CURRENT_DESKTOP=niri        -> LogindLockScreenDetector (new).
  2. XDG_CURRENT_DESKTOP=ubuntu:GNOME -> GnomeLockScreenDetector (the
     component matching must survive the new else: branch).
  3. XDG_CURRENT_DESKTOP unset       -> LogindLockScreenDetector.

Detector construction is exception-contained by design (the Gio subscribe
paths swallow bus failures and stay inert), so these assertions hold whether
or not the harness host actually runs logind or a session bus.
"""
import os

import fixtures  # must be first: isolates DATA_PATH


def make_manager():
    from src.backend.LockScreenManager.LockScreenManager import LockScreenManager

    # Bypass __init__: it spawns the setup thread immediately, and this
    # scenario needs to set the env first and observe each selection
    # synchronously.
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
