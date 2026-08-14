"""Disabling autostart must win over the racing async portal callback.

A portal failure callback must not re-install the flatpak entry after a
disable. A native install never calls the portal and installs the native entry.
"""
import os
import tempfile

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)
import globals as gl

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class FakeXdp:
    """Stands in for gi.repository.Xdp and records request_background calls.

    The scenario fires their async callbacks by hand. finish() always fails,
    which is the path that re-installs the entry.
    """

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

    home = tempfile.mkdtemp(prefix="sc_autostart_home_")
    os.environ["HOME"] = home  # read at call time by setup_autostart_desktop_entry
    path = os.path.join(home, ".config", "autostart", "Deckard.desktop")

    # 1. Flatpak disable, then the async portal failure lands.
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

    # 2. Flatpak stale enable callback against a newer disable.
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

    # 3. Native install uses no portal, and installs the native entry.
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

    # 4. legacy pre-rename autostart entries are removed on every setup.
    autostart_dir = os.path.join(home, ".config", "autostart")
    os.makedirs(autostart_dir, exist_ok=True)
    for legacy in autostart.LEGACY_AUTOSTART_NAMES:
        with open(os.path.join(autostart_dir, legacy), "w") as f:
            f.write("[Desktop Entry]\n")
    # an unrelated entry that must survive
    with open(os.path.join(autostart_dir, "opendeck.desktop"), "w") as f:
        f.write("[Desktop Entry]\n")

    autostart.setup_autostart(True)  # runs remove_legacy_autostart_entries()
    for legacy in autostart.LEGACY_AUTOSTART_NAMES:
        assert not os.path.exists(os.path.join(autostart_dir, legacy)), (
            f"legacy autostart entry {legacy} not removed"
        )
    assert os.path.exists(os.path.join(autostart_dir, "opendeck.desktop")), (
        "unrelated autostart entry removed"
    )
    print("PASS: legacy autostart entries removed, unrelated kept")

    print("PASS: scenario_autostart_disable")


if __name__ == "__main__":
    main()
