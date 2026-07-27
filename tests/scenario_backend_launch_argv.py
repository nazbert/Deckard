"""
Scenario for issue #172: plugin/action backends must be launched with an argv
list and a real interpreter, not with a shell string built by f-string.

Pre-fix, both launch_backend implementations built

    f". {venv_path}/bin/activate && python3 {backend_path} --port={port}"

and handed it to `Popen(..., shell=True)`. A space anywhere in either path
(a plugin under "My Plugins/", a home dir with a space) made the shell split
it into separate words, and shell metacharacters in it were executed rather
than passed along. `python3` also resolved through PATH, which on a native
install is the system python -- no rpyc there, so a venv-less backend died at
import.

Both classes now go through PluginManager.build_backend_launch_command:

  (a) Argv shape. Spaces and quotes survive as single argv items; a venv
      yields {venv}/bin/python and no venv yields sys.executable; the
      open_in_terminal debug form passes user paths as bash positional
      parameters (nothing interpolated) and honors $DECKARD_TERMINAL.
  (b) The #56 ValueError contract, now shared -- PluginBase.launch_backend
      validates too, which it never used to.
  (c) End-to-end: a real stub backend under a directory whose name contains
      a space is launched by ActionCore.launch_backend and registers back
      over rpyc. Pre-fix this cannot work: the shell splits the path.
  (d) wait_for_backend waits on the Event that register_backend sets instead
      of polling backend_connection in 0.1 s steps -- so it wakes exactly on
      registration, while `tries` keeps its meaning as a tries * 0.1 s
      timeout budget for the plugins that pass one.
"""
import os
import sys
import threading
import time
import types

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

# launch_backend/register_backend push into these two registries; the harness
# never builds a real PluginManager (it would drag in the whole plugin
# ecosystem), so stand in with exactly what those paths touch. Must precede
# the ActionCore import for the same reason scenario_plugin_backend_teardown
# does it.
gl.plugin_manager = types.SimpleNamespace(backends=[], backend_processes=[])

from src.backend.PluginManager.ActionCore import ActionCore  # noqa: E402
from src.backend.PluginManager.PluginManager import build_backend_launch_command  # noqa: E402


# A path that a shell would mangle in three different ways at once.
NASTY_DIR = "backend dir with spaces & 'quotes'"

STUB_BACKEND = '''\
"""Minimal mirror of streamcontroller_plugin_tools.BackendBase: parse --port,
connect back to the frontend, serve on our own port, register."""
import argparse
import threading

import rpyc
from rpyc.utils.server import ThreadedServer


class StubBackend(rpyc.Service):
    def get_marker(self) -> str:
        return "stub-backend-alive"


def main() -> None:
    parser = argparse.ArgumentParser(prog="stub backend")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    frontend_connection = rpyc.connect("localhost", args.port,
                                       config={"allow_public_attrs": True})
    frontend = frontend_connection.root

    # ThreadedServer binds in __init__, so .port is valid before start().
    server = ThreadedServer(StubBackend(), port=0,
                            protocol_config={"allow_public_attrs": True})
    threading.Thread(target=server.start, name="stub_backend_server",
                     daemon=False).start()

    frontend.register_backend(port=server.port)


if __name__ == "__main__":
    main()
'''


def _write_stub_backend() -> str:
    directory = os.path.join(gl.DATA_PATH, NASTY_DIR)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "backend stub.py")
    with open(path, "w") as f:
        f.write(STUB_BACKEND)
    return path


def check_argv_shape(backend_path: str) -> None:
    venv_path = os.path.join(gl.DATA_PATH, "venv with spaces")
    os.makedirs(venv_path, exist_ok=True)

    # No venv -> our own interpreter, not whatever PATH calls python3.
    command = build_backend_launch_command(backend_path, None, 4242)
    assert command == [sys.executable, backend_path, "--port=4242"], command
    print("PASS: venv-less launch uses sys.executable and a 3-item argv")

    # A venv -> that venv's interpreter.
    command = build_backend_launch_command(backend_path, venv_path, 4242)
    assert command == [os.path.join(venv_path, "bin", "python"), backend_path, "--port=4242"], command
    print("PASS: venv launch uses {venv}/bin/python")

    # The whole point: spaces and quotes stay INSIDE one argv item. A shell
    # string would have turned each of these into several words.
    assert " " in backend_path and "'" in backend_path, "test path lost its metacharacters"
    for item in command:
        assert isinstance(item, str), f"argv items must be strings: {item!r}"
    assert command.count(backend_path) == 1, (
        f"backend path is not a single intact argv item: {command}"
    )
    print("PASS: spaced/quoted paths survive as single argv items")

    # Terminal debug form: paths ride in as bash positional parameters, so
    # the script text itself is constant -- nothing to interpolate into.
    os.environ.pop("DECKARD_TERMINAL", None)
    command = build_backend_launch_command(backend_path, venv_path, 4242, open_in_terminal=True)
    assert command == [
        "gnome-terminal", "--", "bash", "-c", '"$1" "$2" --port="$3"; exec $SHELL',
        "deckard-backend", os.path.join(venv_path, "bin", "python"), backend_path, "4242",
    ], command
    script = command[4]
    assert backend_path not in script and venv_path not in script, (
        f"user paths were interpolated into the bash script: {script!r}"
    )
    print("PASS: terminal form passes paths as bash positional parameters")

    # ... and the emulator is configurable.
    os.environ["DECKARD_TERMINAL"] = "kitty"
    command = build_backend_launch_command(backend_path, None, 4242, open_in_terminal=True)
    assert command[0] == "kitty", command
    os.environ.pop("DECKARD_TERMINAL", None)
    print("PASS: DECKARD_TERMINAL selects the terminal emulator")


def check_path_validation(backend_path: str) -> None:
    """The #56 contract, now enforced for PluginBase as well as ActionCore
    because both go through the shared helper."""
    missing = os.path.join(gl.DATA_PATH, "definitely", "not", "here.py")

    for bad in (None, missing):
        try:
            build_backend_launch_command(bad, None, 4242)
        except ValueError:
            pass
        except TypeError as e:
            raise AssertionError(f"backend_path={bad!r} reached os.path.exists: {e}")
        else:
            raise AssertionError(f"backend_path={bad!r} did not raise -- would Popen garbage")

    try:
        build_backend_launch_command(backend_path, missing, 4242)
    except ValueError:
        pass
    else:
        raise AssertionError("a missing venv_path did not raise")

    print("PASS: ValueError for None/missing backend_path and missing venv_path")


def _make_action() -> ActionCore:
    """An ActionCore with only the backend-launch state wired up. __init__ is
    bypassed deliberately (it needs a deck controller, a page and a plugin
    base, none of which the launch contract touches) -- same approach as
    scenario_plugin_backend_teardown."""
    action = ActionCore.__new__(ActionCore)
    action.backend_connection = None
    action.backend = None
    action.server = None
    action.backend_process = None
    action._backend_ready = threading.Event()
    return action


def check_end_to_end_spaced_path(backend_path: str) -> None:
    """The lock-in property: a backend under a directory with a space in its
    name actually launches and registers. Pre-fix the shell split the path
    into words and the launch could not possibly succeed."""
    action = _make_action()
    try:
        action.launch_backend(backend_path)

        assert action.backend_process is not None, "no backend process was spawned"
        assert fixtures.wait_until(
            lambda: action.backend_connection is not None, timeout=30.0
        ), (
            f"the backend under {NASTY_DIR!r} never registered (process "
            f"returncode={action.backend_process.poll()!r}) -- a shell-built "
            f"command line splits the spaced path into separate words"
        )
        assert action.backend.get_marker() == "stub-backend-alive", (
            "registered, but the backend proxy does not answer"
        )
        print("PASS: a backend under a spaced/quoted path launches and registers")
    finally:
        # Grab the handle first: _release_backend_resources nulls the
        # attribute synchronously and does the actual SIGTERM on a daemon
        # thread, so waiting on the attribute would let this process exit
        # while the stub is still alive -- and the stub inherits our stdout,
        # so run_all.py would then block on the pipe until its timeout.
        process = action.backend_process
        action.on_disconnect(None)
        assert fixtures.wait_until(lambda: process.poll() is not None, timeout=15.0), (
            "the stub backend was never terminated"
        )


def check_wait_for_backend_event() -> None:
    """wait_for_backend used to poll self.backend_connection in 0.1 s steps;
    it waits on the Event that register_backend sets, so a registration that
    lands mid-tick wakes it immediately instead of on the next tick. `tries`
    keeps its meaning as a timeout budget of tries * 0.1 s."""
    action = _make_action()

    # Already registered -> returns at once, not on the next tick boundary.
    action._backend_ready.set()
    start = time.monotonic()
    action.wait_for_backend()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05, f"wait_for_backend slept {elapsed:.3f}s despite a ready backend"
    print(f"PASS: wait_for_backend returns in {elapsed*1000:.1f}ms when the backend is ready")

    # Never registers -> still bounded by tries * 0.1s.
    action._backend_ready.clear()
    start = time.monotonic()
    action.wait_for_backend()
    elapsed = time.monotonic() - start
    assert 0.2 < elapsed < 1.5, f"default wait was {elapsed:.3f}s, expected ~0.3s"
    print(f"PASS: wait_for_backend times out after {elapsed:.2f}s (tries=3 -> 0.3s)")

    # A registration arriving mid-wait wakes it early -- the point of the Event.
    action._backend_ready.clear()
    threading.Timer(0.1, action._backend_ready.set).start()
    start = time.monotonic()
    action.wait_for_backend(tries=50)  # 5s budget
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"wait_for_backend did not wake on the Event: {elapsed:.3f}s"
    print(f"PASS: a mid-wait registration wakes wait_for_backend in {elapsed*1000:.0f}ms")


def main() -> None:
    # Below run_all.py's per-scenario timeout, so a stall is reported here
    # (with a message) rather than as an opaque runner timeout.
    fixtures.start_watchdog(75, label="scenario_backend_launch_argv")

    backend_path = _write_stub_backend()

    check_argv_shape(backend_path)
    check_path_validation(backend_path)
    check_end_to_end_spaced_path(backend_path)
    check_wait_for_backend_event()

    print("PASS: scenario_backend_launch_argv")


if __name__ == "__main__":
    main()
