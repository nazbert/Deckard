"""
Regression test for a first launch behind a dead or rate-limited store.

StoreBackend.get_all_plugins answers an Err when every store is unreachable.
PluginRecommendations.load shows the error state instead of iterating that
Err, and an onboarding install failure reaches an error toast on the
surviving main window. Both legs run unbound over duck-typed selves.
"""
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
    from src.backend.Store.store_result import Err, ErrReason, Ok
    from src.windows.Onboarding.PluginRecommendations import PluginRecommendations

    # Leg 1a. An Err from the read boundary shows the error state. Iterating
    # the Err instead raises TypeError and kills the loader thread.
    gl.store_backend = types.SimpleNamespace(get_all_plugins=lambda: Err(ErrReason.NO_CONNECTION))
    fake, calls = make_recommendations_self()
    PluginRecommendations.load(fake)
    assert calls["error"] == 1, (
        "an Err from get_all_plugins must show the error state (pre-fix: "
        "iterating the sentinel killed the loader thread and the spinner span "
        "forever -- fresh-install mode)"
    )
    assert not fake.group.rows, "no rows may be built on a failed fetch"

    # Leg 1b. A raising fetch gets the same treatment.
    def boom():
        raise RuntimeError("store exploded")

    gl.store_backend = types.SimpleNamespace(get_all_plugins=boom)
    fake, calls = make_recommendations_self()
    PluginRecommendations.load(fake)
    assert calls["error"] == 1, "a raising fetch must also show the error state"

    # Leg 1c. An Ok still completes the load. The list holds only falsy
    # entries, so no widgets get built, and build_rows stops the spinner.
    gl.store_backend = types.SimpleNamespace(get_all_plugins=lambda: Ok([None, None]))
    fake, calls = make_recommendations_self()
    PluginRecommendations.load(fake)
    pump_main_context()
    assert calls["loading"] == [True, False], (
        f"a successful fetch must complete the load (set_loading calls: "
        f"{calls['loading']})"
    )
    assert calls["error"] == 0

    print("PASS: recommendations page survives an unreachable store")


def check_get_plugin_for_id_offline() -> None:
    """A headless backend check over an unreachable store. get_all_plugins
    answers an Err, and get_plugin_for_id narrows it to None. Iterating that
    Err raises TypeError, which each caller's @log.catch swallows."""
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.store_result import Err, ErrReason

    backend = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    backend.get_all_plugins = lambda include_images=True: Err(ErrReason.NO_CONNECTION, "offline")

    assert backend.get_plugin_for_id("com_any_Plugin") is None, (
        "an unreachable store must resolve to None, not raise TypeError out of "
        "an iterated sentinel"
    )
    print("PASS: get_plugin_for_id returns None when the store is unreachable")


class _LabelRecorder:
    def __init__(self, text):
        self.text = text

    def set_text(self, text):
        self.text = text


class _SpinnerRecorder:
    def __init__(self):
        self.visible = True
        self.spinning = True

    def set_visible(self, visible):
        self.visible = visible

    def start(self):
        self.spinning = True

    def stop(self):
        self.spinning = False


def check_missing_row_spinner_recovers() -> None:
    """The real MissingRow.install over a backend whose store is unreachable.

    The failure must reach the failed label and the error styling. A raise
    inside install lands in its @log.catch, which leaves the label on the
    installing text and the spinner turning."""
    import types as _types

    from src.backend import timer_wheel
    from src.backend.Store.StoreBackend import StoreBackend
    from src.backend.Store.store_result import Err, ErrReason
    from src.windows.mainWindow.elements.Sidebar.elements.ActionMissing.MissingRow import MissingRow

    backend = StoreBackend.__new__(StoreBackend)
    backend.get_all_plugins = lambda include_images=True: Err(ErrReason.NO_CONNECTION, "offline")
    gl.store_backend = backend

    # The 3s auto-hide would leave a live timer past this check, and it hides
    # the recovery state the assertions read.
    real_schedule = timer_wheel.schedule
    timer_wheel.schedule = lambda *a, **k: None

    css: list = []
    label = _LabelRecorder("Installing...")
    spinner = _SpinnerRecorder()
    fake = _types.SimpleNamespace(
        action_id="com_test_Missing::action0",
        spinner=spinner,
        label=label,
        install_label="Install",
        installing_label="Installing...",
        install_failed_label="Install failed",
        add_css_class=lambda name: css.append(name),
        set_sensitive=lambda sensitive: None,
        main_button=_types.SimpleNamespace(set_sensitive=lambda sensitive: None),
    )
    # show_install_error and hide_install_error are the real methods, bound
    # to the stub self. Their GLib marshalling is real and pumped below.
    fake.show_install_error = _types.MethodType(MissingRow.show_install_error, fake)
    fake.hide_install_error = _types.MethodType(MissingRow.hide_install_error, fake)

    try:
        MissingRow.install(fake)  # the @log.catch wrapper must not swallow into a stuck spinner
        pump_main_context()
    finally:
        timer_wheel.schedule = real_schedule

    assert label.text == fake.install_failed_label, (
        f"an unreachable store must move the row to the failed label, not leave "
        f"it stuck on {fake.installing_label!r}; got {label.text!r}"
    )
    assert spinner.visible is False and spinner.spinning is False, (
        "the spinner must stop on install failure, not spin forever"
    )
    assert "error" in css, "the error styling must be applied on install failure"
    print("PASS: MissingRow install surfaces failure when the store is unreachable")


def check_install_failures_toast() -> None:
    from src.windows.Onboarding.OnboardingWindow import OnboardingScreen5
    from src.backend.notify import Notify

    toasts = []
    gl.app = types.SimpleNamespace(
        main_win=types.SimpleNamespace(
            show=lambda: None,
            is_visible=lambda: True,
            show_error_toast=lambda body: toasts.append(body),
        )
    )
    # The real facade runs here. The onboarding path reports through
    # gl.notify, and its main-thread routing is under test.
    gl.notify = Notify()

    def get_plugin_for_id(plugin_id):
        return None  # unresolvable, so the install fails

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

    # Called on the main thread, where run_on_main runs inline. The idle_adds
    # queue on the default context and are pumped below.
    OnboardingScreen5._on_start_button_click(fake_self)
    pump_main_context()

    assert len(toasts) == 1, (
        f"install failures must surface as ONE error toast on the surviving "
        f"main window (got {toasts}) -- pre-fix they only flashed on the "
        f"progress bar of the closing onboarding window"
    )
    assert "TestX" in toasts[0] and "Store" in toasts[0], (
        f"the toast must name the failed plugin and point at the store: {toasts[0]}"
    )

    print("PASS: onboarding install failures surface as a main-window toast")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_onboarding_store_offline")
    check_recommendations_offline()
    check_get_plugin_for_id_offline()
    check_missing_row_spinner_recovers()
    check_install_failures_toast()
    print("PASS: scenario_onboarding_store_offline")


if __name__ == "__main__":
    main()
