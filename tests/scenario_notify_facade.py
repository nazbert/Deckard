"""
Pins the gl.notify facade and the send_notification threading fix
that sits under it.

Before the facade, every caller that wanted to tell the user something
carried its own guard: some checked `gl.app`, some checked `main_win`, some
marshalled onto the main thread and some did not, and none of them checked
whether the window was actually VISIBLE -- a toast raised while the app sat
hidden in the tray went onto an overlay nobody was looking at, replacing a
desktop notification the user would have seen.

Guards:
  1. From a worker thread with a visible main window, info()/error() reach
     show_info_toast/show_error_toast -- and touch NOTHING until the main
     context is drained (no GTK work on the caller's thread).
  2. A main window that exists but is not visible falls back to the desktop
     notification, with the error/info icon and the caller's title (or the
     app name when the caller gave none).
  3. An app with no main_win at all falls back the same way.
  4. Before gl.app exists, delivery is queued on gl.app_loading_finished_tasks
     -- the list App.on_activate drains on the main thread -- and lands once
     that drain happens.
  5. The routing decision is made on the main thread: the facade must not
     read is_visible() at call time. Proven by flipping visibility after the
     call but before the drain and observing the LATE value win.
  6. App.send_notification runs its whole body on the main thread: neither
     the settings read nor the Gio.Notification construction may happen on
     the caller's thread (only the final super() call used to be marshalled).

No GTK widgets and no display: the window and app are duck-typed, the
GLib.idle_add trampoline and the real facade/App code under test are not.
"""
import threading
import types

import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)
import globals as gl  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

import appinfo  # noqa: E402

WATCHDOG_SECONDS = 30


def pump() -> None:
    ctx = GLib.MainContext.default()
    for _ in range(200):
        if not ctx.iteration(False):
            break


class Recorder:
    """Duck-typed app/window that records what was delivered and where."""

    def __init__(self, visible: bool = True, with_main_win: bool = True):
        self.toasts: list[tuple[str, str, threading.Thread]] = []
        self.notifications: list[tuple[str, str, str, threading.Thread]] = []
        self.visible = visible
        if with_main_win:
            # Not set at all when absent: App only grows .main_win in
            # on_activate, so the attribute genuinely does not exist before it.
            self.main_win = types.SimpleNamespace(
                is_visible=lambda: self.visible,
                show_info_toast=lambda text: self.toasts.append(
                    ("info", text, threading.current_thread())),
                show_error_toast=lambda text: self.toasts.append(
                    ("error", text, threading.current_thread())),
            )

    def send_notification(self, icon, title, body, **kwargs):
        self.notifications.append((icon, title, body, threading.current_thread()))


def call_from_worker(fn, *args, **kwargs) -> None:
    errors: list[BaseException] = []

    def worker():
        try:
            fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 -- surfaced below
            errors.append(e)

    t = threading.Thread(target=worker, name="notify_caller")
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "gl.notify call hung on the worker thread"
    assert not errors, f"gl.notify call raised on the worker thread: {errors[0]!r}"


def check_visible_window_gets_toasts(notify) -> None:
    app = Recorder(visible=True)
    gl.app = app

    call_from_worker(notify.error, "plugins failed to load")
    call_from_worker(notify.info, "3 assets updated")

    # 1. Nothing may have happened yet -- the whole point is that the GTK
    # work waits for the main context.
    assert app.toasts == [], (
        f"the facade touched the window from the calling thread: {app.toasts}"
    )
    assert app.notifications == []

    pump()

    assert [(kind, text) for kind, text, _ in app.toasts] == [
        ("error", "plugins failed to load"),
        ("info", "3 assets updated"),
    ], f"wrong toast routing/order: {app.toasts}"
    assert all(th is threading.main_thread() for _, _, th in app.toasts), (
        "toasts were raised off the GTK main thread"
    )
    assert app.notifications == [], (
        f"a visible window must get toasts, not desktop notifications: "
        f"{app.notifications}"
    )

    print("PASS: visible window gets toasts, marshalled onto the main thread")


def check_hidden_window_falls_back(notify) -> None:
    # Window exists but is hidden (closed to the tray / behind onboarding).
    app = Recorder(visible=False)
    gl.app = app

    call_from_worker(notify.error, "plugins failed to load", "Plugins")
    call_from_worker(notify.info, "3 assets updated")
    pump()

    assert app.toasts == [], (
        f"a hidden window must not be toasted at -- nobody would see it: "
        f"{app.toasts}"
    )
    assert len(app.notifications) == 2, f"expected 2 notifications: {app.notifications}"
    icon, title, body, thread = app.notifications[0]
    assert icon == "dialog-error-symbolic", f"error must use the error icon: {icon}"
    assert title == "Plugins", f"the caller's title must be used: {title}"
    assert body == "plugins failed to load"
    assert thread is threading.main_thread()
    icon, title, body, _ = app.notifications[1]
    assert icon == "dialog-information-symbolic", f"info must use the info icon: {icon}"
    assert title == appinfo.APP_NAME, (
        f"a title-less call must fall back to the app name, got {title!r}"
    )

    print("PASS: hidden window falls back to desktop notifications")


def check_no_main_win_falls_back(notify) -> None:
    app = Recorder(with_main_win=False)
    gl.app = app
    assert not hasattr(app, "main_win")

    call_from_worker(notify.error, "boom")
    pump()

    assert len(app.notifications) == 1, (
        f"an app without a main window must still reach the user: "
        f"{app.notifications}"
    )
    print("PASS: app without a main window falls back to desktop notifications")


def check_pre_app_deferral(notify) -> None:
    gl.app = None
    gl.app_loading_finished_tasks.clear()

    call_from_worker(notify.error, "1 plugin failed to load -- check the logs")
    pump()

    assert len(gl.app_loading_finished_tasks) == 1, (
        f"a call made before the app exists must be queued for on_activate's "
        f"drain, got {gl.app_loading_finished_tasks}"
    )

    # Now do exactly what App.on_activate does: publish the app, then drain
    # by atomic pop (this mirrors app.py -- keep the two in sync).
    app = Recorder(visible=True)
    gl.app = app
    while gl.app_loading_finished_tasks:
        gl.app_loading_finished_tasks.pop(0)()
    pump()

    assert [(kind, text) for kind, text, _ in app.toasts] == [
        ("error", "1 plugin failed to load -- check the logs")
    ], f"the deferred report never landed: {app.toasts}"

    # A task that appends another task while the drain runs must get the
    # nested task drained too -- the old iterate-then-clear drain silently
    # discarded it (cleared unrun).
    ran: list[str] = []
    gl.app_loading_finished_tasks.append(
        lambda: (ran.append("outer"),
                 gl.app_loading_finished_tasks.append(lambda: ran.append("nested")))
    )
    while gl.app_loading_finished_tasks:
        gl.app_loading_finished_tasks.pop(0)()
    assert ran == ["outer", "nested"], (
        f"a task appended during the drain was dropped: {ran}"
    )

    print("PASS: pre-app calls defer onto app_loading_finished_tasks")


class _FlipOnAppend(list):
    """Simulates the racing interleaving deterministically: the moment the
    facade appends its deferred task, on_activate has already published the
    app (and, in the drain_owns variant, drains before the facade can take
    the task back)."""

    def __init__(self, app, drain_before_remove: bool = False):
        super().__init__()
        self._app = app
        self._drain_before_remove = drain_before_remove

    def append(self, task):
        gl.app = self._app  # on_activate's publish, racing our append
        super().append(task)

    def remove(self, task):
        if self._drain_before_remove:
            # The drain wins the race to the task: pop-and-run everything
            # before the facade's reclaim attempt goes through.
            while self:
                self.pop(0)()
        super().remove(task)


def check_drain_race_exactly_once(notify) -> None:
    """The append-vs-drain TOCTOU: gl.app flips between the
    facade's None-check and its append. Whichever side ends up owning the
    task, the report must be delivered exactly once -- the pre-fix facade
    stranded it in the list forever (silent loss of boot-time reports)."""
    original_tasks = gl.app_loading_finished_tasks

    # Interleaving A: the drain has already finished when the append lands.
    # The facade must notice and reclaim the task itself.
    app = Recorder(visible=True)
    gl.app = None
    gl.app_loading_finished_tasks = _FlipOnAppend(app)
    try:
        call_from_worker(notify.error, "boot-window report")
        assert list(gl.app_loading_finished_tasks) == [], (
            "the task was stranded on the queue after the drain finished"
        )
        pump()
        assert [(kind, text) for kind, text, _ in app.toasts] == [
            ("error", "boot-window report")
        ], f"reclaimed delivery wrong or missing: {app.toasts}"

        # Interleaving B: the drain pops the task before the facade's
        # reclaim. The reclaim must back off -- exactly one delivery, not two.
        app2 = Recorder(visible=True)
        gl.app = None
        gl.app_loading_finished_tasks = _FlipOnAppend(app2, drain_before_remove=True)
        call_from_worker(notify.error, "boot-window report")
        pump()
        assert [(kind, text) for kind, text, _ in app2.toasts] == [
            ("error", "boot-window report")
        ], f"drain-owned delivery must happen exactly once: {app2.toasts}"
    finally:
        gl.app_loading_finished_tasks = original_tasks

    print("PASS: append-vs-drain race delivers exactly once (both interleavings)")


def check_decision_is_made_on_the_main_thread(notify) -> None:
    # The window is visible when the call is made and hidden by the time the
    # idle callback runs. If the facade decided at call time it would toast
    # (and the toast would be invisible); deciding inside the callback picks
    # the notification. This is the TOCTOU the per-caller guards all had.
    app = Recorder(visible=True)
    gl.app = app

    call_from_worker(notify.error, "late-hidden")
    app.visible = False
    pump()

    assert app.toasts == [], (
        "the facade decided at call time -- a window hidden between the call "
        "and the delivery still got the toast"
    )
    assert len(app.notifications) == 1, (
        f"the delivery must re-check visibility on the main thread: "
        f"{app.notifications}"
    )

    # And the mirror case: hidden at call time, visible at delivery.
    app = Recorder(visible=False)
    gl.app = app
    call_from_worker(notify.info, "late-shown")
    app.visible = True
    pump()
    assert len(app.toasts) == 1 and app.notifications == [], (
        f"a window that came up before delivery must get the toast: "
        f"toasts={app.toasts} notifications={app.notifications}"
    )

    print("PASS: toast/notification routing is decided on the main thread")


def check_send_notification_is_fully_idle() -> None:
    """The facade's fallback leg calls App.send_notification from wherever the
    idle callback runs, but plenty of code still calls it directly from worker
    threads (asset updates, store installs). Its ENTIRE body must be
    marshalled -- it used to read the settings and build the Gio.Notification
    on the caller's thread and marshal only the final super() call."""
    from src.app import App

    settings_reads: list[threading.Thread] = []
    sends: list[threading.Thread] = []

    def get_app_settings():
        settings_reads.append(threading.current_thread())
        return {"ui": {"show-notifications": True}}

    # The stub mirrors SettingsManager's real shape: app() is an AppSettings
    # view over get_app_settings(), so the read-tracking still observes every
    # settings access whichever door the code under test uses.
    from src.backend.SettingsManager import AppSettings
    gl.settings_manager = types.SimpleNamespace(
        get_app_settings=get_app_settings,
        app=lambda: AppSettings(get_app_settings()))
    # super().send_notification resolves through the MRO to Gio.Application;
    # patching it there keeps the real method body under test while giving us
    # somewhere to observe the delivery (a real send needs a registered app).
    Gio.Application.send_notification = lambda self, app_id, notif: sends.append(
        threading.current_thread())

    # A bare instance: App.__init__ needs a display, and none of it is needed
    # to exercise the method's threading contract.
    app = App.__new__(App)

    call_from_worker(App.send_notification, app, "dialog-error-symbolic", "T", "B")

    assert settings_reads == [], (
        f"send_notification read the app settings on the calling thread: "
        f"{settings_reads}"
    )
    assert sends == []

    pump()

    assert [th is threading.main_thread() for th in settings_reads] == [True], (
        f"the settings read must happen exactly once, on the main thread: "
        f"{settings_reads}"
    )
    assert [th is threading.main_thread() for th in sends] == [True], (
        f"the notification must be sent exactly once, on the main thread: {sends}"
    )

    # The opt-out still works, and is also evaluated on the main thread.
    settings_reads.clear()
    sends.clear()
    gl.settings_manager = types.SimpleNamespace(
        get_app_settings=lambda: {"ui": {"show-notifications": False}},
        app=lambda: AppSettings({"ui": {"show-notifications": False}}))
    call_from_worker(App.send_notification, app, "dialog-error-symbolic", "T", "B")
    pump()
    assert sends == [], "show-notifications=False must suppress the notification"

    print("PASS: App.send_notification runs entirely on the main thread")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_notify_facade")

    from src.backend.notify import Notify

    notify = Notify()
    try:
        check_visible_window_gets_toasts(notify)
        check_hidden_window_falls_back(notify)
        check_no_main_win_falls_back(notify)
        check_pre_app_deferral(notify)
        check_drain_race_exactly_once(notify)
        check_decision_is_made_on_the_main_thread(notify)
        check_send_notification_is_fully_idle()
    finally:
        gl.app = None
        gl.app_loading_finished_tasks.clear()

    print("PASS: scenario_notify_facade")


if __name__ == "__main__":
    main()
