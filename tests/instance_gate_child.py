"""One contender, driven from its own process, for scenario_instance_gate.

NOT a scenario (no ``scenario_`` prefix, so the runner does not glob it): it is
the other process a uniqueness test needs. Registration is one-shot per
process and the daemon answers a second request for a name the SAME connection
already owns with ALREADY_OWNER -- so two GApplications inside one interpreter
cannot contend, and every leg about contention has to be several processes.

Configured entirely through the environment, because the data-dir isolation
below rewrites argv before anything can parse it. DECKARD_GATE_DATA is
mandatory and points at a directory the parent owns: a child that fell back to
the real data path would create and write the user's own Deckard directory.

Modes:
  establish       -- run the gate and report the verdict (and the NON_UNIQUE
                     flag); a primary holds the name for DECKARD_GATE_HOLD
                     seconds so contenders decide while it still owns it.
  activate        -- run the gate, then hand off the way a second launch does.
  primary-quit    -- become the primary and answer the "quit" action the way
                     the app does: end the process inside the handler, so the
                     reply never arrives and the name drops with the process.
  primary-deaf    -- become the primary and register no quit action at all:
                     the launch that asked it to close waits out its grace.
  old-name        -- own the PRE-RENAME application name, with a quit action,
                     for the transition guard.

Every mode prints single-line records on stdout, flushed, so the parent can
read them with a blocking readline and never wonder whether output is stuck in
a buffer.
"""
import os
import sys

# Before `import globals`, which resolves (and creates) the data directory
# from argv at import time. The parent's directory, not one of this child's
# own: a mode that ends in os._exit skips every cleanup hook it could register.
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

    Wall clock rather than monotonic on purpose: monotonic clocks are not
    comparable across processes, and the point of the barrier is that several
    children reach `register()` in the same millisecond.
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
        # Hold the name while the other contenders make their decision -- a
        # primary that exited immediately would let the next one win too, and
        # the leg would pass while proving nothing.
        #
        # DISPATCHING, not sleeping: joining an application as its remote takes
        # an answer from the owner, so a primary that holds the name without
        # iterating its main context blocks every contender for the full bus
        # timeout instead of losing to it cleanly. The app has the same
        # property between registering and reaching run().
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


def _become_primary_loop(app_id: str, answer_quit: bool,
                         use_gate: bool = True) -> None:
    app = Gio.Application(application_id=app_id)
    if use_gate:
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=False)
        if decision is not instance_gate.Decision.PRIMARY:
            say(f"VERDICT {decision.value}")
            raise SystemExit(f"expected to be the primary, got {decision.value}")
    else:
        # Registration only, deliberately not through the gate: this stands in
        # for a build that predates it, and running the gate under the
        # pre-rename name would have it probe the very name it is claiming.
        app.register(None)
        if app.get_is_remote():
            raise SystemExit(f"{app_id} was already owned")

    if answer_quit:
        delay = float(os.environ.get("DECKARD_GATE_QUIT_DELAY", "0"))

        def on_quit(*_args):
            say("QUIT-RECEIVED")
            if delay > 0:
                # Answers the call, then takes a moment to go -- an instance
                # with a teardown to run. The waiting launch has to poll for
                # the release rather than take the reply as one.
                GLib.timeout_add(int(delay * 1000), lambda: os._exit(0))
                return
            # The app's own shape: the process ends inside the handler, so the
            # caller never gets a reply and the name goes away with the
            # connection. What the waiting launch sees is the release, not an
            # answer.
            os._exit(0)

        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", on_quit)
        app.add_action(action)

    say("READY")
    # Owning the name without dispatching is what a real instance does for the
    # whole of its boot: it has registered, so it answers nothing that runs on
    # the main context -- including the quit action above.
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
