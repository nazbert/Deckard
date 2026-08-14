"""
Regression test for the main-window toast methods.

MainWindow.show_error_toast exists and is defined exactly once, because a
second definition would shadow the first. The methods run unbound over a duck-
typed self.
"""

# Both toast methods, called from a worker thread as update_assets does, touch
# the overlay only through the GLib main context.
import ast
import os
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

    # 1. The method the store-update error path calls must exist.
    assert hasattr(MainWindow, "show_error_toast"), (
        "MainWindow.show_error_toast is missing -- main.py update_assets' "
        "error path raises AttributeError and the user never sees the failure"
    )

    # 1b. A duplicate definition is invisible at runtime, because the last
    # one wins, so the count is pinned in the source instead.
    source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "windows", "mainWindow", "mainWindow.py")
    with open(source_path) as f:
        tree = ast.parse(f.read(), filename=source_path)
    definitions: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    definitions[item.name] = definitions.get(item.name, 0) + 1
    for name in ("show_error_toast", "show_info_toast", "_add_toast"):
        assert definitions.get(name) == 1, (
            f"MainWindow.{name} is defined {definitions.get(name)} times -- a "
            f"shadowed duplicate makes the live behaviour depend on definition "
            f"order"
        )

    fake_win = FakeWindowSelf()
    # Bind the real toast internals onto the duck-typed window, so the
    # unbound public methods reach them through self.
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

    # The worker must not touch the overlay itself. The GTK work is deferred
    # to the main context.
    assert fake_win.toast_overlay.toasts == [], (
        f"toast overlay was touched directly from the worker thread: "
        f"{fake_win.toast_overlay.calling_threads}"
    )

    # 3. Drain the default main context on the main thread. The marshalled
    # toasts must land here, with the right content.
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
    # The error toast keeps its longer dwell time.
    assert by_title["Failed to update store assets"].get_timeout() == 7, (
        "error toasts must linger 7s -- they explain missing functionality"
    )
    assert by_title["3 assets updated"].get_timeout() == 3

    print("PASS: scenario_toast_threadsafe")


if __name__ == "__main__":
    main()
