"""
Plugin warm-up scenario (issue #117): PluginManager.warm_up_plugins() must
invoke every registered plugin's on_app_ready() hook exactly once, off the
calling (GTK main) thread, returning immediately even when a plugin's hook
is slow -- and one raising plugin must not prevent the others from being
warmed. Also checks the hook exists as an inherited no-op on PluginBase, so
unmodified plugins are unaffected.
"""
import threading
import time

import fixtures  # must be first: isolates DATA_PATH before `import globals`
import globals as gl  # noqa: F401  (imported for side-effect ordering)

from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.PluginManager import PluginManager


class _BarePlugin(PluginBase):
    """Skips PluginBase.__init__ on purpose: the scenario exercises the
    warm-up dispatch contract, not plugin construction (locales, asset
    manager)."""

    def __init__(self):  # noqa: super-init-not-called
        pass


class DefaultHookPlugin(_BarePlugin):
    """Uses the inherited no-op on_app_ready -- proves existing plugins that
    never heard of the hook keep working untouched."""


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

    # Register directly in the class-level plugins dict, the way register()
    # would (subset of its fields; warm-up only reads "object").
    PluginBase.plugins.clear()
    PluginBase.plugins.update({
        "test_slow": {"object": slow},
        "test_raising": {"object": raising},
        "test_recording": {"object": recording},
        "test_default": {"object": default},
        "test_broken_entry": {},  # no "object" -- must be skipped, not crash
    })

    try:
        start = time.monotonic()
        manager.warm_up_plugins()
        elapsed = time.monotonic() - start
        # The caller (App.on_activate, on the GTK main thread) must not be
        # blocked by the slow plugin's 1s hook.
        assert elapsed < 0.2, f"warm_up_plugins blocked the caller for {elapsed:.2f}s"

        for name, plugin in (("slow", slow), ("raising", raising), ("recording", recording)):
            assert plugin.called_event.wait(timeout=10), f"{name} plugin's on_app_ready never ran"

        # A raising hook must not have prevented later plugins from warming
        # (dict order puts raising before recording).
        assert recording.calls == 1
        assert raising.calls == 1
        assert slow.calls == 1

        for name, plugin in (("slow", slow), ("raising", raising), ("recording", recording)):
            assert plugin.called_on_thread is not threading.main_thread(), \
                f"{name} plugin's on_app_ready ran on the main thread"

        # Warm-up is a startup one-shot; a second invocation calls hooks
        # again by design (nothing re-invokes it in the app), so just check
        # it stays non-blocking and isolated.
        recording.called_event.clear()
        manager.warm_up_plugins()
        assert recording.called_event.wait(timeout=10)
        assert recording.calls == 2
    finally:
        PluginBase.plugins.clear()

    print("PASS: scenario_plugin_warm_up")


if __name__ == "__main__":
    main()
