"""
Unit-tier scenario (docs/memory-footprint-impl-plan.md P1.4): CallbackRegistry
(src/Signals/weak_callbacks.py) is the registry backing SignalManager,
EventHolder and the plugin-settings Observer. Its correctness properties --
weak storage for bound methods, dedupe-on-add, thread safety, and the
SC_STRONG_CALLBACKS escape hatch -- are independent of which subsystem is
using it, so this exercises the registry directly rather than through any
of those higher-level classes.

Covers:
  (a) a bound method dies with its owner after gc -> snapshot() drops it
  (b) a lambda (no owner to weak-ref) survives
  (c) dedupe: the same bound method added twice -> one entry
  (d) concurrent add/remove/snapshot from 4 threads for 2s -> no exception,
      no live entry lost
  (e) SC_STRONG_CALLBACKS=1 keeps a bound method alive past its owner's
      death (checked in a subprocess, since the flag is read once at import)
"""
import gc
import os
import subprocess
import sys
import threading
import time
import weakref

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from src.Signals.weak_callbacks import CallbackRegistry

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class _Owner:
    """A throwaway object whose only purpose is to die and take its bound
    method's WeakMethod entry down with it (unless SC_STRONG_CALLBACKS is
    set)."""

    def __init__(self):
        self.calls = 0

    def method(self):
        self.calls += 1


def check_bound_method_dies_with_owner():
    registry = CallbackRegistry()
    owner = _Owner()
    assert registry.add(owner.method) is True
    assert len(registry.snapshot()) == 1

    died = []
    weakref.finalize(owner, died.append, True)

    del owner
    gc.collect()

    assert died, "fixture sanity: owner should have been collected"
    assert registry.snapshot() == [], "dead bound method must not survive in snapshot()"
    assert len(registry) == 0


def check_lambda_stays():
    registry = CallbackRegistry()
    calls = []
    cb = lambda: calls.append(1)  # noqa: E731
    assert registry.add(cb) is True
    gc.collect()
    snap = registry.snapshot()
    assert snap == [cb], snap
    snap[0]()
    assert calls == [1]


def check_dedupe_same_bound_method():
    registry = CallbackRegistry()
    owner = _Owner()
    assert registry.add(owner.method) is True
    # `owner.method` creates a brand new bound-method wrapper object every
    # time it's accessed -- dedupe must be on (obj, func) equality, not the
    # identity of that wrapper.
    assert registry.add(owner.method) is False
    assert len(registry) == 1
    snap = registry.snapshot()
    assert len(snap) == 1
    snap[0]()
    assert owner.calls == 1


def check_concurrent_add_remove_snapshot():
    registry = CallbackRegistry()

    # Canaries: added once, up-front, never touched again by the hammering
    # threads below. If concurrent add/remove/snapshot corrupts the
    # registry's internal list, a canary going missing from the final
    # snapshot is the tell.
    canary_owners = [_Owner() for _ in range(5)]
    for owner in canary_owners:
        assert registry.add(owner.method) is True
    assert len(registry) == 5

    stop = threading.Event()
    errors = []

    def hammer_add():
        local_owners = []
        while not stop.is_set():
            o = _Owner()
            local_owners.append(o)
            try:
                registry.add(o.method)
                if len(local_owners) > 20:
                    victim = local_owners.pop(0)
                    registry.remove(victim.method)
            except Exception as e:  # pragma: no cover
                errors.append(e)

    def hammer_remove():
        # Repeatedly remove a callable that was never added -- pure lock
        # contention, must never raise or corrupt state.
        ghost = _Owner()
        while not stop.is_set():
            try:
                registry.remove(ghost.method)
            except Exception as e:  # pragma: no cover
                errors.append(e)

    def hammer_snapshot():
        while not stop.is_set():
            try:
                for cb in registry.snapshot():
                    cb()
            except Exception as e:  # pragma: no cover
                errors.append(e)

    threads = [
        threading.Thread(target=hammer_add, name="hammer_add_1"),
        threading.Thread(target=hammer_add, name="hammer_add_2"),
        threading.Thread(target=hammer_remove, name="hammer_remove"),
        threading.Thread(target=hammer_snapshot, name="hammer_snapshot"),
    ]
    for t in threads:
        t.start()
    time.sleep(2.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), f"{t.name} did not stop"

    assert not errors, f"concurrent add/remove/snapshot raised: {errors!r}"

    final = registry.snapshot()
    for owner in canary_owners:
        assert owner.method in final, (
            "a live canary callback was lost under concurrent add/remove/snapshot"
        )


def check_strong_callbacks_env_escape_hatch():
    # SC_STRONG_CALLBACKS is read once at import time, so this needs a fresh
    # interpreter with the env var already set.
    script = (
        "import sys, gc\n"
        f"sys.path.insert(0, {_REPO_ROOT!r})\n"
        "from src.Signals.weak_callbacks import CallbackRegistry\n"
        "class Owner:\n"
        "    def method(self):\n"
        "        pass\n"
        "registry = CallbackRegistry()\n"
        "owner = Owner()\n"
        "registry.add(owner.method)\n"
        "del owner\n"
        "gc.collect()\n"
        "snap = registry.snapshot()\n"
        "assert len(snap) == 1, f'expected the bound method to survive, got {len(snap)}'\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["SC_STRONG_CALLBACKS"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"SC_STRONG_CALLBACKS=1 subprocess failed "
        f"(rc={result.returncode}):\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout, result.stdout


def main() -> None:
    check_bound_method_dies_with_owner()
    check_lambda_stays()
    check_dedupe_same_bound_method()
    check_concurrent_add_remove_snapshot()
    check_strong_callbacks_env_escape_hatch()
    print("PASS: scenario_weak_registry")


if __name__ == "__main__":
    main()
