"""
run_command detaches a command line without forking the interpreter.

It Popens the command line directly, so shell semantics such as redirection and
&& survive, and it reaps the child on a throwaway daemon thread.
"""

# The call never blocks its caller, run_command(None) is a silent no-op, and an
# unspawnable command is logged rather than raised.
import multiprocessing
import os
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

import psutil

from src.backend.DeckManagement.HelperMethods import run_command


def _own_children() -> list:
    """Direct children of this process, including not-yet-reaped zombies."""
    children = []
    for child in psutil.Process().children(recursive=False):
        try:
            if child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                children.append(child)
        except psutil.Error:
            pass  # exited and got reaped between listing and inspection
    return children


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_run_command_detach")

    baseline_children = {c.pid for c in _own_children()}

    # Shell-only syntax, a redirection and an && chain. Neither survives an
    # argv-list rewrite, so the string and shell contract must hold.
    marker = os.path.join(gl.DATA_PATH, "run_command_marker")
    second = os.path.join(gl.DATA_PATH, "run_command_marker_2")
    assert not os.path.exists(marker)

    # The fork check samples while the command is still in flight. A fork
    # wrapper is short-lived and active_children() reaps as a side effect, so
    # a single check afterwards would miss it.
    forked = []

    def _command_finished() -> bool:
        forked.extend(multiprocessing.active_children())
        return os.path.exists(second)

    run_command(f"echo detached > '{marker}' && cp '{marker}' '{second}'")

    assert fixtures.wait_until(_command_finished, timeout=15.0, interval=0.005), (
        "the shell command never ran -- run_command must keep its "
        "string/shell contract (redirection, &&)"
    )
    with open(marker) as f:
        assert f.read().strip() == "detached", "redirection did not produce the expected content"
    print("PASS: shell semantics (redirection + &&) survive")

    # The interpreter is never forked. Forking the whole app, with GTK,
    # plugins, deck threads and open HID handles, to spawn a shell is what
    # this scenario forbids.
    assert forked == [], f"run_command forked the interpreter: {forked!r}"
    print("PASS: no multiprocessing child -- the interpreter is not forked")

    # The direct child is reaped, not left as a zombie.
    assert fixtures.wait_until(
        lambda: {c.pid for c in _own_children()} <= baseline_children, timeout=15.0
    ), (
        f"run_command's child was never reaped (zombies: "
        f"{[(c.pid, c.status()) for c in _own_children()]})"
    )
    print("PASS: the spawned child is reaped, no zombie left behind")

    # The caller does not wait for the command.
    start = time.monotonic()
    run_command("sleep 10")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"run_command blocked its caller for {elapsed:.2f}s -- it must detach"
    print(f"PASS: run_command returned in {elapsed*1000:.0f}ms with a 10s command running")

    # Do not leave the sleeper behind for the rest of the run.
    for child in _own_children():
        if child.pid not in baseline_children:
            try:
                child.kill()
            except psutil.Error:
                pass
    fixtures.wait_until(
        lambda: {c.pid for c in _own_children()} <= baseline_children, timeout=10.0
    )

    # None is a no-op, not a crash.
    run_command(None)
    print("PASS: run_command(None) is a silent no-op")

    # An unspawnable command is logged, not raised. cwd is the only spawn
    # input a caller cannot see, so point HOME at a directory that does not
    # exist and the exec fails in the parent.
    real_home = os.environ.get("HOME")
    os.environ["HOME"] = os.path.join(gl.DATA_PATH, "home that is not there")
    try:
        run_command("true")
    except OSError as e:
        raise AssertionError(f"run_command must not raise on a failed spawn: {e!r}")
    finally:
        if real_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = real_home
    print("PASS: an unspawnable command is logged, not raised")

    print("PASS: scenario_run_command_detach")


if __name__ == "__main__":
    main()
