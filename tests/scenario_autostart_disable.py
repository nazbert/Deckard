"""
Regression test for "disabling autostart can asynchronously re-install a
broken flatpak-style autostart entry" (gl#42).

The state machine under test (autostart.py):

  * setup_autostart(False) removes ~/.config/autostart/Deckard.desktop
    synchronously, but the portal request it also fires completes LATER, and
    its failure callback used to call setup_autostart_desktop_entry() with the
    defaults (enable=True, native=False) -- re-copying the flatpak .desktop
    (exec'ing /app/bin/launch.sh, broken outside the sandbox) right back after
    the removal. Disable must be authoritative over that racing async writer.

  * Native installs must never go through the portal at all (the portal is
    flatpak plumbing; on native it just fails asynchronously and triggered the
    same broken fallback), and the entry they install must be the NATIVE
    desktop file, not the flatpak one.

  * Calls are serialized by generation: a stale request's callback (e.g. an
    enable superseded by a disable) must not clobber the newer call's state.

The portal is replaced by a fake whose finish() always fails, so the scenario
can deliver the async failure at a chosen point -- exactly the race the issue
describes. HOME is redirected to a temp dir so ~/.config/autostart is never
the real one.
"""
import os
import tempfile

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)
import globals as gl

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class FakeXdp:
    """Stands in for gi.repository.Xdp: records request_background calls and
    lets the scenario fire their async callbacks by hand, with a finish()
    that always fails -- the exact path that used to re-install the entry."""

    class BackgroundFlags:
        AUTOSTART = "autostart"
        ACTIVATABLE = "activatable"

    class Portal:
        instances = []

        def __init__(self):
            self.requests = []  # (flag, callback)
            FakeXdp.Portal.instances.append(self)

        @classmethod
        def new(cls):
            return cls()

        def request_background(self, parent, reason, cmd, flag, cancellable, callback, user_data):
            self.requests.append((flag, callback))

        def request_background_finish(self, result):
            raise RuntimeError("portal request failed (simulated)")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_autostart_disable")
    gl.MAIN_PATH = REPO_ROOT  # source dir of flatpak/autostart*.desktop

    import autostart
    autostart.Xdp = FakeXdp
    autostart.IS_MAC = False

    home = tempfile.mkdtemp(prefix="sc_autostart_home_")
    os.environ["HOME"] = home  # read at call time by setup_autostart_desktop_entry
    path = os.path.join(home, ".config", "autostart", "Deckard.desktop")

    # --- 1. flatpak: disable, then the async portal failure lands -------
    autostart.is_flatpak = lambda: True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nExec=/app/bin/launch.sh -b\n")

    autostart.setup_autostart(False)
    assert not os.path.exists(path), "disable must remove the entry synchronously"

    assert FakeXdp.Portal.instances, "flatpak path must go through the portal"
    portal = FakeXdp.Portal.instances[-1]
    assert portal.requests, "flatpak path must issue a portal request"
    _, callback = portal.requests[-1]
    callback(portal, object(), None)  # async failure arrives AFTER the removal
    assert not os.path.exists(path), (
        "the portal's failure callback re-installed the autostart entry after "
        "disable -- disable must be authoritative"
    )
    print("PASS: flatpak disable survives the async portal failure")

    # --- 2. flatpak: stale enable callback vs newer disable -------------
    autostart.setup_autostart(True)
    enable_portal = FakeXdp.Portal.instances[-1]
    _, enable_callback = enable_portal.requests[-1]

    autostart.setup_autostart(False)              # newer call wins
    enable_callback(enable_portal, object(), None)  # stale failure lands last
    assert not os.path.exists(path), (
        "a stale enable request's failure callback re-installed the entry "
        "after a newer disable -- calls must be serialized"
    )
    print("PASS: stale enable callback superseded by newer disable")

    # --- 3. native: no portal at all; correct (native) entry content ----
    autostart.is_flatpak = lambda: False
    FakeXdp.Portal.instances.clear()

    autostart.setup_autostart(True)
    assert not FakeXdp.Portal.instances, "native installs must not touch the portal"
    assert os.path.exists(path), "native enable must install the entry"
    with open(path) as f:
        content = f.read()
    assert "/app/bin/launch.sh" not in content, (
        f"native entry execs the flatpak launcher: {content!r}"
    )

    autostart.setup_autostart(False)
    assert not FakeXdp.Portal.instances, "native disable must not touch the portal"
    assert not os.path.exists(path), "native disable must remove the entry"
    print("PASS: native path never touches the portal; entry content is native")

    print("PASS: scenario_autostart_disable")


if __name__ == "__main__":
    main()
