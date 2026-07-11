"""
Regression test for gl#41: main.py's update_assets failure path called a
non-existent MainWindow.show_error_toast (every store-update failure died
as AttributeError inside @log.catch, invisible to the user), and its
success path called show_info_toast directly from the update_assets worker
thread -- constructing an Adw.Toast and calling add_toast off the GTK main
thread.

Guards:
  1. show_error_toast exists.
  2. Both toast methods, called from a worker thread (as update_assets
     does), touch the toast overlay ONLY via the GLib main context -- no
     off-main add_toast, and the marshalled work lands with the right
     title/priority once the main thread drains the context.

The methods are driven unbound with a duck-typed `self` (a real MainWindow
needs a display); the marshalling path itself -- GLib.idle_add into the
default main context -- is real.
"""
import threading
import types

import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, GLib


class FakeToastOverlay:
    def __init__(self):
        self.toasts = []
        self.calling_threads = []

    def add_toast(self, toast) -> None:
        self.toasts.append(toast)
        self.calling_threads.append(threading.current_thread())


class FakeWindowSelf:
    def __init__(self):
        self.toast_overlay = FakeToastOverlay()


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_toast_threadsafe")

    from src.windows.mainWindow.mainWindow import MainWindow

    # 1. The method main.py's store-update error path calls must exist.
    assert hasattr(MainWindow, "show_error_toast"), (
        "MainWindow.show_error_toast is missing -- main.py update_assets' "
        "error path raises AttributeError and the user never sees the failure"
    )

    fake_win = FakeWindowSelf()
    # Bind the real toast internals onto the duck-typed window so the
    # unbound public methods can reach them through `self`.
    fake_win._add_toast = types.MethodType(MainWindow._add_toast, fake_win)

    # 2. Call both from a worker thread, exactly like update_assets does.
    worker_errors = []

    def worker():
        try:
            MainWindow.show_error_toast(fake_win, "Failed to update store assets")
            MainWindow.show_info_toast(fake_win, "3 assets updated")
        except Exception as e:  # noqa: BLE001 -- reraised via assert below
            worker_errors.append(e)

    t = threading.Thread(target=worker, name="update_assets")
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "toast call hung in the worker thread"
    assert not worker_errors, f"toast call raised in the worker thread: {worker_errors}"

    # The worker must not have touched the overlay itself -- the whole point
    # is that GTK work is deferred to the main context.
    assert fake_win.toast_overlay.toasts == [], (
        f"toast overlay was touched directly from the worker thread: "
        f"{fake_win.toast_overlay.calling_threads}"
    )

    # 3. Drain the default main context on this ("main") thread; the
    # marshalled toasts must land here, with the right content.
    ctx = GLib.MainContext.default()
    for _ in range(100):
        if len(fake_win.toast_overlay.toasts) >= 2:
            break
        if not ctx.iteration(False):
            break

    toasts = fake_win.toast_overlay.toasts
    assert len(toasts) == 2, f"expected 2 marshalled toasts, got {len(toasts)}"
    assert all(th is threading.main_thread() for th in fake_win.toast_overlay.calling_threads), (
        "add_toast ran off the main thread"
    )

    by_title = {toast.get_title(): toast for toast in toasts}
    assert "Failed to update store assets" in by_title, f"error toast missing: {list(by_title)}"
    assert "3 assets updated" in by_title, f"info toast missing: {list(by_title)}"
    assert by_title["Failed to update store assets"].get_priority() == Adw.ToastPriority.HIGH
    assert by_title["3 assets updated"].get_priority() == Adw.ToastPriority.NORMAL

    print("PASS: scenario_toast_threadsafe")


if __name__ == "__main__":
    main()
