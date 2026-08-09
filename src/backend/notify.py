"""
Thread-safe user-notification facade (issue #183).

Every caller that wanted to tell the user something used to carry its own
copy of the same dance: is `gl.app` up yet, does it have a `main_win`, do I
need a `GLib.idle_add`, and do I fall back to a desktop notification. Six
call sites, six slightly different guards, several of them subtly wrong
(checking the window on a background thread, or toasting onto a window that
is hidden in the tray).

`gl.notify.info(...)` / `gl.notify.error(...)` is the whole interface. It is
safe from any thread and at any point in startup:

* Before `gl.app` exists the delivery is queued on
  `gl.app_loading_finished_tasks`, which `App.on_activate` drains on the
  main thread once the window is up.
* Otherwise it is marshalled with `GLib.idle_add`, and the choice between
  an in-app toast and a desktop notification is made *inside* that callback
  -- i.e. on the GTK main thread. Deciding at call time would read
  `is_visible()` off-thread and leave a window between the check and the
  delivery in which the main window can disappear.
* A main window that exists but is not visible (closed to the tray, or
  still behind onboarding) gets the desktop notification, not a toast on an
  overlay nobody is looking at.

Imports `globals` directly; `globals` must NOT import this module back
(it only declares the `notify` slot, filled by main.create_global_objects).
"""
from gi.repository import GLib

import appinfo
import globals as gl


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
        if gl.app is None:
            # Drained on the main thread by App.on_activate once the window
            # is up; re-entering _dispatch then takes the branch below.
            #
            # The append races the drain: on_activate may set gl.app and
            # finish popping the queue between our None-check and our
            # append, stranding the task forever. After appending, re-check
            # and try to take the task back -- list.remove/pop are atomic
            # under the GIL, so exactly one side ends up owning it: either
            # the drain popped it (remove raises ValueError; the drain
            # delivers) or we reclaim it here and deliver ourselves.
            task = lambda: self._dispatch(is_error, text, title)
            gl.app_loading_finished_tasks.append(task)
            if gl.app is None:
                return
            try:
                gl.app_loading_finished_tasks.remove(task)
            except ValueError:
                return
        GLib.idle_add(self._deliver, is_error, text, title)

    def _deliver(self, is_error: bool, text: str, title: str | None) -> bool:
        # Main thread only. Bound once: _dispatch only schedules us once gl.app
        # is up, but the idle callback runs later and the desktop-notification
        # branch below would dereference it a second time.
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
