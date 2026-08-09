"""
Scenario: run_command must detach a command line without
forking the interpreter -- and must not leave the child as a zombie.

Pre-fix, run_command wrapped subprocess.Popen in a multiprocessing.Process:
the whole app (GTK, plugins, deck threads, open HID handles) was forked just
to spawn a shell, and the wrapper's only real function was to orphan the
grandchild so nobody had to reap it. The fix Popens directly and reaps with
a throwaway daemon thread.

The contract asserted here:

  (a) Shell semantics survive. run_command takes a command LINE, not an argv
      list -- callers use redirection/pipes/`&&` and the flatpak prefix is
      spliced in as a string. A command that only works through a shell must
      still work.
  (b) No interpreter fork: multiprocessing.active_children() stays empty.
  (c) The direct child is reaped -- it must not linger as a zombie.
  (d) The call is non-blocking: a command that sleeps must not hold up the
      caller (the old wrapper's fork was async for the same reason).
  (e) run_command(None) is still a silent no-op, and a command that cannot
      be spawned at all is logged, not raised: the fork wrapper ran the
      Popen in the child, so an OSError never reached the caller, and the
      callers are plugin action callbacks with nothing to do about one.
"""
import multiprocessing
import os
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

import psutil

from src.backend.DeckManagement.HelperMethods import run_command


def _own_children() -> list:
    """Direct children of this process, including not-yet-reaped zombies --
    which is exactly what (c) is about."""
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

    # (a) -- shell-only syntax: a redirection AND a `&&` chain. Neither
    # survives an argv-list rewrite, which is the point: the string/shell
    # contract is deliberate, not an oversight.
    marker = os.path.join(gl.DATA_PATH, "run_command_marker")
    second = os.path.join(gl.DATA_PATH, "run_command_marker_2")
    assert not os.path.exists(marker)

    # (b) is sampled while (a) is still in flight: the fork-wrapper was
    # short-lived, and active_children() reaps as a side effect, so a single
    # check after the fact would miss it. Poll from the instant of the call.
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

    # (b) -- the interpreter is never forked any more. Forking the whole app
    # (GTK, plugins, deck threads, open HID handles) to spawn a shell is
    # exactly what this scenario forbids.
    assert forked == [], f"run_command forked the interpreter: {forked!r}"
    print("PASS: no multiprocessing child -- the interpreter is not forked")

    # (c) -- the direct child is reaped, not left as a zombie.
    assert fixtures.wait_until(
        lambda: {c.pid for c in _own_children()} <= baseline_children, timeout=15.0
    ), (
        f"run_command's child was never reaped (zombies: "
        f"{[(c.pid, c.status()) for c in _own_children()]})"
    )
    print("PASS: the spawned child is reaped, no zombie left behind")

    # (d) -- the caller doesn't wait for the command.
    start = time.monotonic()
    run_command("sleep 10")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"run_command blocked its caller for {elapsed:.2f}s -- it must detach"
    print(f"PASS: run_command returned in {elapsed*1000:.0f}ms with a 10s command running")

    # Don't leave the sleeper behind for the rest of the run.
    for child in _own_children():
        if child.pid not in baseline_children:
            try:
                child.kill()
            except psutil.Error:
                pass
    fixtures.wait_until(
        lambda: {c.pid for c in _own_children()} <= baseline_children, timeout=10.0
    )

    # (e) -- None is a no-op, not a crash.
    run_command(None)
    print("PASS: run_command(None) is a silent no-op")

    # (e cont.) -- an unspawnable command is logged, not raised. cwd is the
    # only spawn input a caller can't see: point HOME at a directory that
    # doesn't exist and the exec fails in the parent, where the old fork
    # wrapper would have swallowed it in the child.
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
