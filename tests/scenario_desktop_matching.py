"""
Desktop-session matching for the window grabber.

XDG_CURRENT_DESKTOP is a colon-separated list of names ("ubuntu:GNOME",
"sway:wlroots:swayfx"). WindowGrabber used to compare it as one whole
string against a fixed list of literals, so a stock Ubuntu session matched
nothing, no integration was constructed, and window-based automatic page
switching was silently dead for the whole session.

Both halves are asserted here:

  * `src.backend.session_info` -- the shared splitter: components are
    lowercased, stripped, order-preserving, and empty when the variable is
    unset or blank.
  * `WindowGrabber.select_integration_class` -- the pure selection, driven
    through the real environment variables. The selection returns the class
    instead of an instance, so nothing here spawns a watcher thread, a DBus
    proxy or an `swaymsg`/`xprop` subprocess.

The KDE-on-Xorg case guards the branch order: the session-type check
outranks the KDE component, and a KDE session on X11 must keep selecting
the xprop integration.
"""
import os

import fixtures  # must be first: isolates DATA_PATH


def stage_env(desktop: str | None, server: str | None) -> None:
    for name, value in (("XDG_CURRENT_DESKTOP", desktop), ("XDG_SESSION_TYPE", server)):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def select(desktop: str | None, server: str | None = "wayland"):
    from src.backend.session_info import desktop_components, session_type
    from src.backend.WindowGrabber.WindowGrabber import select_integration_class

    stage_env(desktop, server)
    return select_integration_class(desktop_components(), session_type())


def check_helper() -> None:
    from src.backend.session_info import desktop_components, session_type

    stage_env("ubuntu:GNOME", "wayland")
    assert desktop_components() == ["ubuntu", "gnome"], desktop_components()
    assert session_type() == "wayland", session_type()

    stage_env(" sway : wlroots : swayfx ", "Wayland")
    assert desktop_components() == ["sway", "wlroots", "swayfx"], desktop_components()
    assert session_type() == "wayland", session_type()

    stage_env("GNOME", "X11")
    assert desktop_components() == ["gnome"], desktop_components()
    assert session_type() == "x11", session_type()

    stage_env(None, None)
    assert desktop_components() == [], desktop_components()
    assert session_type() is None, session_type()

    stage_env("", "")
    assert desktop_components() == [], desktop_components()
    assert session_type() is None, session_type()

    stage_env(":::", None)
    assert desktop_components() == [], desktop_components()

    print("PASS: XDG_CURRENT_DESKTOP splits into lowercased components")


def check_selection() -> None:
    from src.backend.WindowGrabber.Integrations.Gnome import Gnome
    from src.backend.WindowGrabber.Integrations.Hyprland import Hyprland
    from src.backend.WindowGrabber.Integrations.KDE import KDE
    from src.backend.WindowGrabber.Integrations.Sway import Sway
    from src.backend.WindowGrabber.Integrations.X11 import X11

    cases = [
        # (XDG_CURRENT_DESKTOP, XDG_SESSION_TYPE, expected integration)
        ("GNOME", "wayland", Gnome),
        ("ubuntu:GNOME", "wayland", Gnome),
        ("GNOME-Classic:GNOME", "x11", Gnome),
        ("KDE", "wayland", KDE),
        ("sway", "wayland", Sway),
        ("sway:wlroots:swayfx", "wayland", Sway),
        ("Hyprland", "wayland", Hyprland),
        # Session type is the fallback for desktops without an integration.
        ("XFCE", "x11", X11),
        (None, "x11", X11),
        # Branch order: X11 outranks the KDE component.
        ("KDE", "x11", X11),
        # Nothing to grab windows with.
        (None, "wayland", None),
        ("", "", None),
        (None, None, None),
        ("XFCE", "wayland", None),
    ]

    for desktop, server, expected in cases:
        selected = select(desktop, server)
        expected_name = expected.__name__ if expected is not None else "no integration"
        selected_name = selected.__name__ if selected is not None else "no integration"
        assert selected is expected, (
            f"XDG_CURRENT_DESKTOP={desktop!r} XDG_SESSION_TYPE={server!r} must "
            f"select {expected_name}, got {selected_name}"
        )

    print("PASS: every desktop/session combination selects its integration")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_desktop_matching")

    check_helper()
    check_selection()

    print("PASS: scenario_desktop_matching")


if __name__ == "__main__":
    main()
