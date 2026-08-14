"""One contender process for scenario_instance_gate.

Name registration is one-shot per process, so a contention leg needs several
processes. The environment picks the mode. Every mode prints to stdout.
"""
import os
import sys

# Point at the parent's data directory before import globals, which resolves
# and creates the directory from argv at import time. A fallback here writes
# the user's own Deckard data, and a mode that ends in os._exit runs no
# cleanup hook.
_DATA_PATH = os.environ.get("DECKARD_GATE_DATA")
if not _DATA_PATH:
    raise SystemExit(
        "instance_gate_child.py refuses to run without DECKARD_GATE_DATA: "
        "without it `import globals` would resolve the user's real data "
        "directory and create it"
    )
sys.argv = [sys.argv[0], "--data", _DATA_PATH, "--devel",
            "--skip-load-hardware-decks"]

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import time  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import appinfo  # noqa: E402
import globals as gl  # noqa: E402  (imported for its argv-driven side effects)

from src.backend import instance_gate  # noqa: E402

assert gl.DATA_PATH == _DATA_PATH, (
    f"globals resolved {gl.DATA_PATH!r} instead of the parent's "
    f"{_DATA_PATH!r} -- this child would be writing outside the test"
)

MODE = os.environ["DECKARD_GATE_MODE"]
APP_ID = os.environ.get("DECKARD_GATE_APP_ID", appinfo.APP_ID)


def say(line: str) -> None:
    print(line, flush=True)


def wait_for_barrier() -> None:
    """Hold until the shared wall-clock start, if the parent set one.

    The barrier reads the wall clock because monotonic clocks do not compare
    across processes. Several children must reach register() in one millisecond.
    """
    start_at = os.environ.get("DECKARD_GATE_START_AT")
    if not start_at:
        return
    target = float(start_at)
    while time.time() < target:
        time.sleep(0.001)


def run_gate(app: Gio.Application, close_running: bool = False):
    published: list[str] = []
    try:
        decision = instance_gate.establish(
            app, publish=lambda: published.append("published"),
            close_running=close_running)
    except instance_gate.LaunchAborted as e:
        say(f"VERDICT aborted {type(e).__name__}")
        say(f"REASON {e}")
        return None
    say(f"VERDICT {decision.value}")
    say(f"PUBLISHED {len(published)}")
    say(f"NON_UNIQUE {bool(app.get_flags() & Gio.ApplicationFlags.NON_UNIQUE)}")
    return decision


def mode_establish() -> None:
    app = Gio.Application(application_id=APP_ID)
    wait_for_barrier()
    decision = run_gate(app)
    if decision is instance_gate.Decision.PRIMARY:
        # Hold the name while the other contenders decide. A primary that exits
        # at once lets the next one win, and the leg passes while it proves
        # nothing. Dispatch instead of sleep, because joining an application as
        # its remote needs an answer from the owner.
        hold = float(os.environ.get("DECKARD_GATE_HOLD", "0"))
        if hold > 0:
            loop = GLib.MainLoop()
            GLib.timeout_add(int(hold * 1000), lambda: (loop.quit(), False)[1])
            loop.run()


def mode_activate() -> None:
    app = Gio.Application(application_id=APP_ID)
    decision = run_gate(app)
    if decision is instance_gate.Decision.REMOTE:
        app.activate()
        say("ACTIVATED")


# Hold the connection and its callback for the process lifetime. GDBus drops a
# filter when the Python wrapper it was added through is collected, and the
# connection under it is a singleton that stays alive. A dropped wrapper leaves
# a process on the bus that reports nothing, and reports it silently.
_WIRE_WATCH: list = []


def watch_wire() -> None:
    """Report every action Activate that arrives, from GDBus's worker thread.

    The action handler runs on the main context, so a child that never reaches
    its loop cannot say what arrived. A connection filter runs on the reader.
    """
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def on_message(_connection, message, incoming, *_user_data):
        if incoming and message.get_interface() == "org.gtk.Actions" \
                and message.get_member() == "Activate":
            body = message.get_body()
            say(f"WIRE-ACTIVATE {body.unpack()[0] if body else '?'}")
        return message

    _WIRE_WATCH.append((connection, on_message))
    connection.add_filter(on_message)


def _become_primary_loop(app_id: str, answer_quit: bool,
                         use_gate: bool = True) -> None:
    watch_wire()
    app = Gio.Application(application_id=app_id)
    if use_gate:
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=False)
        if decision is not instance_gate.Decision.PRIMARY:
            say(f"VERDICT {decision.value}")
            raise SystemExit(f"expected to be the primary, got {decision.value}")
    else:
        # Register without the gate, which stands in for a build that predates
        # the gate. The gate under the pre-rename name would probe the very
        # name it is claiming.
        app.register(None)
        if app.get_is_remote():
            raise SystemExit(f"{app_id} was already owned")

    if answer_quit:
        delay = float(os.environ.get("DECKARD_GATE_QUIT_DELAY", "0"))

        def on_quit(*_args):
            say("QUIT-RECEIVED")
            if delay > 0:
                # Answer the call, then take a moment to go, like an instance
                # with a teardown. The waiting launch polls for the release
                # instead of taking the reply as one.
                GLib.timeout_add(int(delay * 1000), lambda: os._exit(0))
                return
            # The app has the same shape. The process ends inside the handler,
            # so the caller gets no reply and the name goes with the connection.
            # The waiting launch sees the release, not an answer.
            os._exit(0)

        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", on_quit)
        app.add_action(action)

    say("READY")
    # A real instance owns the name without dispatching for the whole of its
    # boot. It registered, so it answers nothing on the main context, including
    # the quit action above.
    time.sleep(float(os.environ.get("DECKARD_GATE_DISPATCH_DELAY", "0")))
    GLib.MainLoop().run()


def mode_primary_quit() -> None:
    _become_primary_loop(APP_ID, answer_quit=True)


def mode_primary_deaf() -> None:
    _become_primary_loop(APP_ID, answer_quit=False)


def mode_old_name() -> None:
    _become_primary_loop(appinfo.OLD_APP_ID, answer_quit=True, use_gate=False)


MODES = {
    "establish": mode_establish,
    "activate": mode_activate,
    "primary-quit": mode_primary_quit,
    "primary-deaf": mode_primary_deaf,
    "old-name": mode_old_name,
}

if __name__ == "__main__":
    MODES[MODE]()
