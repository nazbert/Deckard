"""
Regression test for issue #118's fresh-install mode: a first launch behind a
dead/rate-limited store must not silently strand the user with zero plugins.

Two legs, both driven unbound with duck-typed selves (real onboarding
widgets need a display; the control flow and the GLib marshalling are real):

1. PluginRecommendations.load(): StoreBackend.get_all_plugins() returns a
   NoConnectionError SENTINEL when every store is unreachable (offline,
   GitHub rate limit). load() iterated it -> TypeError killed the loader
   thread -> the onboarding Plugins page span forever; the user paged past,
   installed nothing, and landed in the main window with an empty Add-Action
   list (the upstream #610 report). Now: the error state is shown (spinner
   stopped), and a raising fetch gets the same treatment. A fetch returning
   a normal list still completes the load.

2. OnboardingScreen5._on_start_button_click(): install failures only flashed
   on the progress bar of a window that closes moments later. Now failures
   aggregate into an error toast on the surviving main window.
"""
import threading
import types

import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import gi

gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

import globals as gl  # noqa: E402

WATCHDOG_SECONDS = 30


def pump_main_context(rounds: int = 50) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


class RecorderGroup:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def make_recommendations_self():
    calls = {"loading": [], "error": 0}
    fake = types.SimpleNamespace(
        defaults=[],
        group=RecorderGroup(),
        set_loading=lambda loading: calls["loading"].append(loading),
        show_connection_error=lambda: calls.__setitem__("error", calls["error"] + 1),
    )
    return fake, calls


def check_recommendations_offline() -> None:
    from src.backend.Store.StoreBackend import NoConnectionError
    from src.windows.Onboarding.PluginRecommendations import PluginRecommendations

    # Leg 1a: sentinel return -- must show the error state, not die.
    gl.store_backend = types.SimpleNamespace(get_all_plugins=lambda: NoConnectionError())
    fake, calls = make_recommendations_self()
    PluginRecommendations.load(fake)
    assert calls["error"] == 1, (
        "a NoConnectionError sentinel from get_all_plugins must show the "
        "error state (pre-fix: TypeError killed the loader thread and the "
        "spinner span forever -- issue #118 fresh-install mode)"
    )
    assert not fake.group.rows, "no rows may be built on a failed fetch"

    # Leg 1b: raising fetch -- same treatment.
    def boom():
        raise RuntimeError("store exploded")

    gl.store_backend = types.SimpleNamespace(get_all_plugins=boom)
    fake, calls = make_recommendations_self()
    PluginRecommendations.load(fake)
    assert calls["error"] == 1, "a raising fetch must also show the error state"

    # Leg 1c: a normal (here: all-falsy, so no widgets get built) list still
    # completes the load -- the idle-marshalled build_rows must run and stop
    # the spinner.
    gl.store_backend = types.SimpleNamespace(get_all_plugins=lambda: [None, None])
    fake, calls = make_recommendations_self()
    PluginRecommendations.load(fake)
    pump_main_context()
    assert calls["loading"] == [True, False], (
        f"a successful fetch must complete the load (set_loading calls: "
        f"{calls['loading']})"
    )
    assert calls["error"] == 0

    print("PASS: recommendations page survives an unreachable store")


def check_install_failures_toast() -> None:
    from src.windows.Onboarding.OnboardingWindow import OnboardingScreen5

    toasts = []
    gl.app = types.SimpleNamespace(
        main_win=types.SimpleNamespace(
            show=lambda: None,
            show_error_toast=lambda body: toasts.append(body),
        )
    )

    async def get_plugin_for_id(plugin_id):
        return None  # unresolvable -> install failure

    gl.store_backend = types.SimpleNamespace(get_plugin_for_id=get_plugin_for_id)

    progress_bar = types.SimpleNamespace(
        set_text=lambda *_: None,
        set_fraction=lambda *_: None,
        set_visible=lambda *_: None,
    )
    loading_box = types.SimpleNamespace(
        loading_label=types.SimpleNamespace(set_label=lambda *_: None),
        set_spinning=lambda *_: None,
        progress_bar=progress_bar,
    )
    plugin_data = types.SimpleNamespace(plugin_id="com_test_x", plugin_name="TestX")
    onboarding_window = types.SimpleNamespace(
        stack=types.SimpleNamespace(set_visible_child_name=lambda *_: None),
        loading_box=loading_box,
        recommendations=types.SimpleNamespace(get_selected_plugins=lambda: [plugin_data]),
        close=lambda: None,
    )
    fake_self = types.SimpleNamespace(onboarding_window=onboarding_window)

    # Called on the main thread: run_on_main runs inline, the idle_adds queue
    # onto the default context and are pumped below.
    OnboardingScreen5._on_start_button_click(fake_self)
    pump_main_context()

    assert len(toasts) == 1, (
        f"install failures must surface as ONE error toast on the surviving "
        f"main window (got {toasts}) -- pre-fix they only flashed on the "
        f"progress bar of the closing onboarding window (issue #118)"
    )
    assert "TestX" in toasts[0] and "Store" in toasts[0], (
        f"the toast must name the failed plugin and point at the store: {toasts[0]}"
    )

    print("PASS: onboarding install failures surface as a main-window toast")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_onboarding_store_offline")
    check_recommendations_offline()
    check_install_failures_toast()
    print("PASS: scenario_onboarding_store_offline")


if __name__ == "__main__":
    main()
