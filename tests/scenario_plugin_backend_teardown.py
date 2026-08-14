"""
Integration scenario for PluginBase backend teardown.

on_disconnect must return fast, null all four backend references and drop the
gl.plugin_manager registry entries synchronously, then finish the closes and
the process kill off-thread.
"""

# A second call is a no-op, and start_server afterwards builds a fresh server
# instead of skipping against a dead one.
import subprocess
import sys
import threading
import time
import types

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl

# PluginBase dereferences gl.plugin_manager registries during teardown. The
# harness installs no real PluginManager, because that imports the whole
# plugin ecosystem, so only the two lists teardown touches exist here.
gl.plugin_manager = types.SimpleNamespace(backends=[], backend_processes=[])

from src.backend.PluginManager.PluginBase import PluginBase  # noqa: E402


class _SlowClosable:
    """Stands in for the rpyc ThreadedServer and Connection. close() blocks,
    like an rpyc close waiting out an in-flight call, and records who ran
    it."""

    def __init__(self, name: str, delay: float = 0.75):
        self.name = name
        self.delay = delay
        self.close_calls = 0
        self.close_threads: list[threading.Thread] = []

    def close(self):
        self.close_calls += 1
        self.close_threads.append(threading.current_thread())
        time.sleep(self.delay)


def _make_plugin(server, connection, process) -> PluginBase:
    """A PluginBase with only the backend-teardown state wired up. __init__
    is bypassed, because it needs a real plugin directory that the teardown
    contract never uses."""
    plugin = PluginBase.__new__(PluginBase)
    plugin.server = server
    plugin.backend_connection = connection
    plugin.backend = object() if connection is not None else None
    plugin.backend_process = process
    return plugin


def main() -> None:  # noqa: C901 -- linear scenario script
    fixtures.start_watchdog(30, label="scenario_plugin_backend_teardown")

    server = _SlowClosable("server")
    connection = _SlowClosable("connection")
    # A real child in its own session, so terminate_backend_process runs its
    # os.killpg path and the reap is observable.
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               start_new_session=True)
    gl.plugin_manager.backends.append(connection)
    gl.plugin_manager.backend_processes.append(process)

    plugin = _make_plugin(server, connection, process)

    # The caller must not pay for the closes or the kill escalation.
    caller = threading.current_thread()
    start = time.monotonic()
    plugin.on_disconnect(None)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, (
        f"on_disconnect blocked its caller for {elapsed:.2f}s -- teardown is "
        f"still inline; with a real backend that is up to ~5s of "
        f"frozen UI on the uninstall path"
    )
    print(f"PASS: on_disconnect returned in {elapsed*1000:.0f}ms with slow closes pending")

    # References nulled and registries dropped synchronously.
    assert plugin.server is None, "self.server not nulled -- relaunch would skip against a dead server"
    assert plugin.backend_connection is None, "self.backend_connection not nulled"
    assert plugin.backend is None, "self.backend not nulled -- later calls would hit a dead proxy"
    assert plugin.backend_process is None, "self.backend_process not nulled"
    assert connection not in gl.plugin_manager.backends, "connection left in gl.plugin_manager.backends"
    assert process not in gl.plugin_manager.backend_processes, "process left in gl.plugin_manager.backend_processes"
    print("PASS: all backend references nulled and registries dropped synchronously")

    # The teardown itself still happens, off-thread.
    assert fixtures.wait_until(
        lambda: server.close_calls == 1 and connection.close_calls == 1, timeout=10.0
    ), "server/connection close() never ran"
    assert fixtures.wait_until(lambda: process.poll() is not None, timeout=10.0), (
        "backend child process was never terminated/reaped"
    )
    for closable in (server, connection):
        for t in closable.close_threads:
            assert t is not caller, f"{closable.name}.close() ran on the calling thread"
    print("PASS: closes and process termination ran to completion off-thread")

    # A second teardown is a no-op.
    plugin.on_disconnect(None)
    time.sleep(0.2)
    assert server.close_calls == 1 and connection.close_calls == 1, (
        f"double teardown re-closed resources (server={server.close_calls}, "
        f"connection={connection.close_calls})"
    )
    print("PASS: double teardown is idempotent")

    # With self.server nulled, start_server() must start a fresh server
    # instead of skipping against a dead one.
    plugin.start_server()
    assert plugin.server is not None, "start_server() did not build a fresh server after teardown"
    assert plugin.server is not server, "start_server() reused the dead server"
    print("PASS: start_server() relaunches after teardown instead of skipping")

    # Close the real rpyc server this spawned, and give its thread a moment.
    plugin.on_disconnect(None)
    fixtures.wait_until(lambda: plugin.server is None, timeout=5.0)
    time.sleep(0.3)

    print("PASS: scenario_plugin_backend_teardown")


if __name__ == "__main__":
    main()
