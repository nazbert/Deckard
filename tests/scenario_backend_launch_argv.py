"""Plugin and action backends launch with an argv list and a real interpreter.

PluginManager.build_backend_launch_command keeps spaces and quotes inside one
argv item, picks the venv python or sys.executable, and validates the paths.
"""
import os
import sys
import threading
import time
import types

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

# launch_backend and register_backend push into these two registries. The
# harness never builds a real PluginManager, which would drag in the whole
# plugin ecosystem, so stand in with what those paths touch. This must precede
# the ActionCore import.
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


def _make_venv(name: str) -> str:
    """Build a venv skeleton with a directory and a bin/python that resolves.

    The helper checks both. A dangling bin/python must fail as a ValueError
    with a message, not as a bare FileNotFoundError out of Popen.
    """
    venv_path = os.path.join(gl.DATA_PATH, name)
    os.makedirs(os.path.join(venv_path, "bin"), exist_ok=True)
    interpreter = os.path.join(venv_path, "bin", "python")
    if not os.path.exists(interpreter):
        os.symlink(sys.executable, interpreter)
    return venv_path


def check_argv_shape(backend_path: str) -> None:
    venv_path = _make_venv("venv with spaces")

    # Without a venv the command uses this interpreter, not the python3 on PATH.
    command = build_backend_launch_command(backend_path, None, 4242)
    assert command == [sys.executable, backend_path, "--port=4242"], command
    print("PASS: venv-less launch uses sys.executable and a 3-item argv")

    # With a venv the command uses that venv's interpreter.
    command = build_backend_launch_command(backend_path, venv_path, 4242)
    assert command == [os.path.join(venv_path, "bin", "python"), backend_path, "--port=4242"], command
    print("PASS: venv launch uses {venv}/bin/python")

    # Spaces and quotes stay inside one argv item. A shell string would turn
    # each of these into several words.
    assert " " in backend_path and "'" in backend_path, "test path lost its metacharacters"
    for item in command:
        assert isinstance(item, str), f"argv items must be strings: {item!r}"
    assert command.count(backend_path) == 1, (
        f"backend path is not a single intact argv item: {command}"
    )
    print("PASS: spaced/quoted paths survive as single argv items")

    # In the terminal debug form the paths ride in as bash positional
    # parameters, so the script text stays constant with nothing to interpolate.
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

    # The emulator is configurable. DECKARD_TERMINAL is the whole command
    # prefix, because terminals disagree about how to accept a command.
    # gnome-terminal wants --, konsole, alacritty and xterm want -e, and kitty
    # wants a bare positional. A hardcoded separator fits one family only.
    for spec, expected_prefix in (
        ("kitty", ["kitty"]),
        ("konsole -e", ["konsole", "-e"]),
        ("alacritty -e", ["alacritty", "-e"]),
        ("'my terminal' --", ["my terminal", "--"]),
    ):
        os.environ["DECKARD_TERMINAL"] = spec
        command = build_backend_launch_command(backend_path, None, 4242, open_in_terminal=True)
        assert command[:len(expected_prefix)] == expected_prefix, (spec, command)
        assert command[len(expected_prefix)] == "bash", (spec, command)
        assert command[-3:] == [sys.executable, backend_path, "4242"], (spec, command)
    os.environ.pop("DECKARD_TERMINAL", None)
    print("PASS: DECKARD_TERMINAL carries the terminal's own exec flag")

    # A blank setting must not strip the terminal off the argv, which would run
    # bash headless and pass the whole thing to the wrong argv[0].
    os.environ["DECKARD_TERMINAL"] = "   "
    command = build_backend_launch_command(backend_path, None, 4242, open_in_terminal=True)
    assert command[:2] == ["gnome-terminal", "--"], command
    os.environ.pop("DECKARD_TERMINAL", None)
    print("PASS: a blank DECKARD_TERMINAL falls back to the default")


def check_path_validation(backend_path: str) -> None:
    """The path-validation contract, enforced for PluginBase and ActionCore.

    Both go through the shared helper.
    """
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

    # A venv dir that exists whose bin/python does not resolve, which is what a
    # python upgrade leaves behind on a native install. Popen would raise
    # FileNotFoundError from inside launch_backend. The contract says
    # ValueError, and the message must name the interpreter.
    broken_venv = os.path.join(gl.DATA_PATH, "broken venv")
    os.makedirs(os.path.join(broken_venv, "bin"), exist_ok=True)
    dangling = os.path.join(broken_venv, "bin", "python")
    if not os.path.lexists(dangling):
        os.symlink(os.path.join(gl.DATA_PATH, "gone", "python3.13"), dangling)
    try:
        build_backend_launch_command(backend_path, broken_venv, 4242)
    except ValueError as e:
        assert "interpreter" in str(e), f"unhelpful message for a broken venv: {e}"
    except OSError as e:
        raise AssertionError(f"a dangling venv interpreter escaped as OSError: {e}")
    else:
        raise AssertionError("a venv with no usable interpreter did not raise")

    print("PASS: ValueError for None/missing backend_path, missing venv and dangling interpreter")


def _make_action() -> ActionCore:
    """An ActionCore with only the backend-launch state wired up.

    __init__ is bypassed because it needs a deck controller, a page and a
    plugin base, and the launch contract touches none of them.
    """
    action = ActionCore.__new__(ActionCore)
    action.backend_connection = None
    action.backend = None
    action.server = None
    action.backend_process = None
    action._backend_ready = threading.Event()
    return action


def check_end_to_end_spaced_path(backend_path: str) -> None:
    """A backend under a spaced directory name launches and registers.

    A shell string splits that path into words and cannot succeed.
    """
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
        # Grab the handle first. _release_backend_resources nulls the attribute
        # synchronously and sends SIGTERM on a daemon thread, so a wait on the
        # attribute lets this process exit while the stub is alive. The stub
        # inherits this stdout, so run_all.py would block on the pipe.
        process = action.backend_process
        action.on_disconnect(None)
        # Guarded, because launch_backend may have raised and left no process.
        # An AttributeError here would replace the real failure.
        if process is not None:
            assert fixtures.wait_until(lambda: process.poll() is not None, timeout=15.0), (
                "the stub backend was never terminated"
            )


def check_wait_for_backend_event() -> None:
    """wait_for_backend waits on the Event that register_backend sets.

    A registration that lands mid-tick wakes it at once. tries keeps its
    meaning as a timeout budget of tries * 0.1 s.
    """
    action = _make_action()

    # An already registered backend returns at once, not on a tick boundary.
    action._backend_ready.set()
    start = time.monotonic()
    action.wait_for_backend()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05, f"wait_for_backend slept {elapsed:.3f}s despite a ready backend"
    print(f"PASS: wait_for_backend returns in {elapsed*1000:.1f}ms when the backend is ready")

    # A backend that never registers stays bounded by tries * 0.1 s.
    action._backend_ready.clear()
    start = time.monotonic()
    action.wait_for_backend()
    elapsed = time.monotonic() - start
    assert 0.2 < elapsed < 1.5, f"default wait was {elapsed:.3f}s, expected ~0.3s"
    print(f"PASS: wait_for_backend times out after {elapsed:.2f}s (tries=3 -> 0.3s)")

    # A registration that arrives mid-wait wakes the Event early.
    action._backend_ready.clear()
    threading.Timer(0.1, action._backend_ready.set).start()
    start = time.monotonic()
    action.wait_for_backend(tries=50)  # 5s budget
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"wait_for_backend did not wake on the Event: {elapsed:.3f}s"
    print(f"PASS: a mid-wait registration wakes wait_for_backend in {elapsed*1000:.0f}ms")


def main() -> None:
    # Below the per-scenario timeout of run_all.py, so a stall is reported here
    # with a message rather than as an opaque runner timeout.
    fixtures.start_watchdog(75, label="scenario_backend_launch_argv")

    backend_path = _write_stub_backend()

    check_argv_shape(backend_path)
    check_path_validation(backend_path)
    check_end_to_end_spaced_path(backend_path)
    check_wait_for_backend_event()

    print("PASS: scenario_backend_launch_argv")


if __name__ == "__main__":
    main()
