"""
Desktop-session identification, shared by every consumer that adapts to the
running desktop (window grabbing, lock-screen detection).

``XDG_CURRENT_DESKTOP`` is a colon-separated *list* of names ordered most
specific first -- "ubuntu:GNOME", "GNOME-Classic:GNOME",
"sway:wlroots:swayfx" -- so a whole-string comparison matches only the
handful of desktops that happen to publish a single name, and silently
misses stock distro sessions. Matching happens per component; this module is
the single place that knows how to split them.

Stdlib only on purpose: both consumers are constructed early and one of them
(the lock screen manager) runs its detection on a worker thread, so this must
stay importable without pulling in ``globals`` or GTK.
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
