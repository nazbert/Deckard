"""Thread-safe user-notification facade.

gl.notify.info() and gl.notify.error() are the only entry points. Both are
safe from any thread and at any point in startup.

Before gl.app exists, the delivery queues on gl.app_loading_finished_tasks,
and App.on_activate drains that queue on the main thread once the window is
up. src/backend/startup_queue.py owns the queueing decision and the race
between the append and the drain.

Otherwise GLib.idle_add marshals the delivery, and the callback chooses
between an in-app toast and a desktop notification on the GTK main thread. A
choice at call time reads is_visible() off-thread and leaves a gap in which
the main window can disappear.

A main window that exists but is not visible (closed to the tray, or behind
onboarding) gets the desktop notification.

This module imports globals. globals must not import it back, and globals
declares the notify slot alone, which main.create_global_objects fills.
"""
from gi.repository import GLib

import appinfo
import globals as gl
from src.backend import startup_queue


class Notify:
    def info(self, text: str, title: str | None = None) -> None:
        """Non-urgent feedback. Toast while the window is up, desktop
        notification otherwise. Safe to call from any thread."""
        self._dispatch(False, text, title)

    def error(self, text: str, title: str | None = None) -> None:
        """Something the user asked for did not happen. Same routing as
        info(), with the error presentation. Safe to call from any thread."""
        self._dispatch(True, text, title)

    def _dispatch(self, is_error: bool, text: str, title: str | None) -> None:
        # False means the queue owns the delivery. App.on_activate drains the
        # queue on the main thread once the window is up, and the re-entry
        # into _dispatch then takes the deliver-now path.
        # src/backend/startup_queue.py holds the append-against-drain protocol.
        if not startup_queue.get().when_app_ready(
                lambda: self._dispatch(is_error, text, title)):
            return
        GLib.idle_add(self._deliver, is_error, text, title)

    def _deliver(self, is_error: bool, text: str, title: str | None) -> bool:
        # Main thread only. Bind gl.app once. _dispatch schedules this
        # callback only after gl.app is up, but the callback runs later and
        # the desktop-notification branch below reads it a second time.
        app = gl.app
        main_win = getattr(app, "main_win", None)
        if main_win is not None and main_win.is_visible():
            if is_error:
                main_win.show_error_toast(text)
            else:
                main_win.show_info_toast(text)
        elif app is not None:
            icon = "dialog-error-symbolic" if is_error else "dialog-information-symbolic"
            app.send_notification(icon, title or appinfo.APP_NAME, text)
        return GLib.SOURCE_REMOVE
