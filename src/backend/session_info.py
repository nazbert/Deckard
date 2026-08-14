"""Desktop-session identification for window grabbing and lock-screen detection.

XDG_CURRENT_DESKTOP holds a colon-separated list of names, most specific first
("ubuntu:GNOME", "sway:wlroots:swayfx"), so callers must match one component.
A whole-string comparison misses stock distro sessions.
This module stays stdlib-only, because the lock screen manager detects on a
worker thread, before globals and GTK are safe to import.
"""
import os


def desktop_components() -> list[str]:
    """The lowercased components of XDG_CURRENT_DESKTOP, most specific
    first. Empty when the variable is unset or blank."""
    raw = os.getenv("XDG_CURRENT_DESKTOP") or ""
    return [part.strip().lower() for part in raw.split(":") if part.strip()]


def session_type() -> str | None:
    """The lowercased XDG_SESSION_TYPE ("wayland", "x11", "tty"), or None
    when unset or blank."""
    value = os.getenv("XDG_SESSION_TYPE")
    if value is None:
        return None
    return value.strip().lower() or None
