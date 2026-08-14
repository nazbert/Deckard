"""
PluginManager.warm_up_plugins calls on_app_ready once per plugin.

The call runs off the caller's GTK main thread and returns at once, even when a
hook is slow, and one raising hook must not stop the others. A second warm-up
re-fires nothing, and a plugin loaded after activation still gets its hook.
"""

# PluginBase carries the hook as an inherited no-op.
import threading
import time

import fixtures  # must be first, to isolate DATA_PATH before globals loads
import globals as gl  # noqa: F401  (imported for side-effect ordering)

from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.PluginManager import PluginManager


class _BarePlugin(PluginBase):
    """Skips PluginBase.__init__. This scenario drives the warm-up dispatch
    contract, not plugin construction."""

    def __init__(self):  # does not call super().__init__()
        pass


class DefaultHookPlugin(_BarePlugin):
    """Uses the inherited no-op on_app_ready, so a plugin that never heard of
    the hook keeps working untouched."""


class RecordingPlugin(_BarePlugin):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.called_on_thread = None
        self.called_event = threading.Event()

    def on_app_ready(self):
        self.calls += 1
        self.called_on_thread = threading.current_thread()
        self.called_event.set()


class SlowPlugin(RecordingPlugin):
    def on_app_ready(self):
        time.sleep(1.0)
        super().on_app_ready()


class RaisingPlugin(RecordingPlugin):
    def on_app_ready(self):
        super().on_app_ready()
        raise RuntimeError("deliberate on_app_ready failure")


def main() -> None:
    fixtures.start_watchdog(30, "scenario_plugin_warm_up")

    assert hasattr(PluginBase, "on_app_ready"), "PluginBase.on_app_ready hook missing"
    assert hasattr(PluginBase, "on_backend_ready"), "PluginBase.on_backend_ready hook missing"

    manager = PluginManager()

    slow = SlowPlugin()
    raising = RaisingPlugin()
    recording = RecordingPlugin()
    default = DefaultHookPlugin()

    # Register directly in the class-level plugins dict, as register() does.
    # Warm-up reads only the "object" field.
    PluginBase.plugins.clear()
    PluginBase.plugins.update({
        "test_slow": {"object": slow},
        "test_raising": {"object": raising},
        "test_recording": {"object": recording},
        "test_default": {"object": default},
        "test_broken_entry": {},  # no "object", so warm-up must skip it
    })

    try:
        # A pre-activation load_plugins must warm nothing. At startup
        # load_plugins runs inside create_global_objects, long before
        # on_activate's warm-up establishes app-readiness.
        manager.load_plugins()
        time.sleep(0.3)
        assert recording.calls == 0, "load_plugins warmed plugins before app-ready"

        # The startup warm-up returns at once, fires every hook off the main
        # thread, and isolates exceptions.
        start = time.monotonic()
        manager.warm_up_plugins()
        elapsed = time.monotonic() - start
        # The caller runs on the GTK main thread and must not wait for the
        # slow plugin's one-second hook.
        assert elapsed < 0.2, f"warm_up_plugins blocked the caller for {elapsed:.2f}s"

        for name, plugin in (("slow", slow), ("raising", raising), ("recording", recording)):
            assert plugin.called_event.wait(timeout=10), f"{name} plugin's on_app_ready never ran"

        # A raising hook must not stop later plugins from warming. Dict order
        # puts raising before recording.
        assert recording.calls == 1
        assert raising.calls == 1
        assert slow.calls == 1

        for name, plugin in (("slow", slow), ("raising", raising), ("recording", recording)):
            assert plugin.called_on_thread is not threading.main_thread(), \
                f"{name} plugin's on_app_ready ran on the main thread"

        # A second warm-up must re-fire nobody.
        manager.warm_up_plugins()
        time.sleep(0.5)
        assert recording.calls == 1, "second warm_up_plugins re-fired on_app_ready"
        assert slow.calls == 1 and raising.calls == 1

        # A plugin hot-installed after activation gets its hook when
        # load_plugins re-runs on the store-install path, and an
        # already-warmed plugin does not re-fire.
        late = RecordingPlugin()
        PluginBase.plugins["test_late"] = {"object": late}
        manager.load_plugins()
        assert late.called_event.wait(timeout=10), \
            "hot-installed plugin's on_app_ready never ran after load_plugins"
        assert late.calls == 1
        assert late.called_on_thread is not threading.main_thread()
        time.sleep(0.3)
        assert recording.calls == 1, "late-load warm-up re-fired an already-warmed plugin"
    finally:
        PluginBase.plugins.clear()

    print("PASS: scenario_plugin_warm_up")


if __name__ == "__main__":
    main()
