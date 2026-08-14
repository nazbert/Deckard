"""
Author: Core447
Year: 2023

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import signal
import threading
import time
from typing import TYPE_CHECKING

import gi

from src.windows.Store.ResponsibleNotesDialog import ResponsibleNotesDialog
from src.windows.Donate.DonateWindow import DonateWindow

import appinfo
import globals as gl

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Xdp", "1.0")

from gi.repository import Gtk, Adw, Gdk, Gio, GLib, Xdp

from loguru import logger as log
import os

from src.backend import timer_wheel
from src.backend import ui_port
from src.backend import startup_queue
from src.backend.PageManagement import page_flush
from src.backend.Store.store_result import Err, Ok
from src.windows.ui_adapter import GtkUIAdapter
from src.windows.mainWindow.mainWindow import MainWindow
from src.windows.AssetManager.AssetManager import AssetManager
from src.windows.Store.Store import Store
from src.windows.Shortcuts.Shortcuts import ShortcutsWindow
from src.windows.Onboarding.OnboardingWindow import OnboardingWindow
from src.windows.Permissions.FlatpakPermissionRequest import FlatpakPermissionRequestWindow

from src.Signals import Signals
from src.api import stop_dbus_service

if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckManager import DeckManager

import globals as gl


# How long a queued Ctrl+C can wait for a dispatch before the next one force
# quits from signal-handler context. See App._on_sigint. This is a third of the
# 6 s force_quit watchdog. The gate is elapsed time with the teardown not
# started, never the press count. A busy app answers late, and key repeat
# sends three presses in 66 ms.
SIGINT_ESCALATE_AFTER_S = 2.0


def unix_signal_add(priority, signum, callback) -> bool:
    """Install a GLib main-loop source for signum. True if one went in.

    GLib 2.80 moved the Unix API from the GLib-2.0 introspection namespace to
    GLibUnix-2.0, so the runtime GLib carries exactly one of the two spellings:
    GLib.unix_signal_add on older distributions, GLibUnix.signal_add on current
    ones. This tries both, and returns False when neither is introspectable.
    """
    add = getattr(GLib, "unix_signal_add", None)
    if add is None:
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix  # type: ignore[attr-defined]  # gi stub: PyGObject-stubs ships no GLibUnix-2.0; the host GLib decides whether it exists, which this try/except probes
            add = GLibUnix.signal_add
        except (ImportError, ValueError, AttributeError):
            return False
    try:
        add(priority, signum, callback)
    except Exception as e:
        # A resolved symbol can still fail. A signum that g_unix_signal_add
        # refuses, a GLib without UNIX signal support, and an argument mismatch
        # between the two spellings all raise here. An escaping exception
        # leaves App.__init__ and stops the startup.
        log.warning(f"Could not install a GLib unix-signal source for {signum}: {e}")
        return False
    return True


class App(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Re-entry latch for on_quit. Set at construction, so the first signal
        # finds it whenever the handlers go up.
        self._quit_started = False

        # Time of the first Ctrl+C, for the escalation in _on_sigint, and None
        # until then. It lives here for the same reason as the latch above.
        self._sigint_first_at: float | None = None

        # The live engine-to-UI adapter, so on_quit can detach it. It stays
        # None until on_activate builds the window, so a TERM before that
        # raises nothing here.
        self._ui_adapter = None

        # on_activate fills both. Other windows read them through gl.app, which
        # publishes before the loop starts. Declare them here, so an early
        # reader finds None instead of an AttributeError.
        self.deck_manager: "DeckManager" = None  # type: ignore[assignment]  # late-init: on_activate
        self.style_manager: Adw.StyleManager = None  # type: ignore[assignment]  # late-init: on_activate

        self.connect("activate", self.on_activate)

        css_provider = Gtk.CssProvider()
        css_provider.load_from_path(os.path.join(gl.top_level_dir, "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_search_path(os.path.join(gl.top_level_dir, "Assets", "icons"))

    def on_activate(self, app):
        log.trace("running: on_activate")
        if getattr(self, "_activate_completed", False):
            # GApplication forwards a second launch here as a remote
            # activation.
            # A rebuild orphans every gl.app.main_win reader and the cached UI
            # bindings of the controllers, and a boot argv with -b never
            # presents the replacement. on_reopen gives a forwarded activation
            # and the reopen action one code path.
            #
            # The guard reads a completion flag, not main_win. The first
            # statement of MainWindow.__init__ publishes self.main_win before
            # the build can fail, so a main_win guard latches on a failed build
            # and presents a half-built window. This flag stays False on such a
            # failure, and the next activation rebuilds.
            self.on_reopen()
            return

        # The code below needs the global objects. This application is
        # constructed before they exist, because its registration decides
        # whether the launch boots. The deck manager and the settings manager
        # both exist by the time the main loop activates it.
        self.deck_manager = gl.deck_manager

        app_settings = gl.settings_manager.app()

        allow_white_mode = app_settings.allow_white_mode

        # Count the launch. This sits below the re-activation return, so a
        # second launch that presents this window counts as a reopen.
        app_settings.app_launches = app_settings.app_launches + 1
        app_settings.save()

        self.style_manager = self.get_style_manager()
        if allow_white_mode:
            self.style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        else:
            self.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK) # Not everything looks good in light mode at the moment #TODO

        # Install the engine-to-UI adapter before the window construction.
        # Every boot-time add_page runs inside the MainWindow constructor, so a
        # later install misses each bind and leaves every preview dirty-marked.
        # attach_window() runs after, for the map and unmap handlers, and it
        # re-scans the stack to bind the children the constructor added.
        adapter = GtkUIAdapter()
        self._ui_adapter = adapter
        ui_port.install(adapter)
        try:
            self.main_win = MainWindow(application=app, deck_manager=self.deck_manager)
        except Exception:
            ui_port.install(None)
            self._ui_adapter = None
            raise
        adapter.attach_window(self.main_win)
        if not gl.argparser.parse_args().b:
            self.main_win.present()

        self.show_onboarding()
        # Call directly, because MainWindow.do_after_build_tasks() drains
        # on_finished inside the constructor above, so an entry added to that
        # list here never runs.
        self.show_donate()
        # self.show_permissions()

        self.shortcuts = ShortcutsWindow(app=app, application=app)
        # self.shortcuts.present()

        on_reopen_action = Gio.SimpleAction.new("reopen", None)
        on_reopen_action.connect("activate", self.on_reopen)
        self.add_action(on_reopen_action)

        on_quit_action = Gio.SimpleAction.new("quit", None)
        on_quit_action.connect("activate", self.on_quit)
        self.add_action(on_quit_action)

        self.add_signals()

        # Publish first, drain second. That order lets an appender that races
        # this drain reclaim its own task. See src/backend/startup_queue.py.
        gl.app = self
        startup_queue.get().drain_app_ready()

        # Warm the plugin backends on their own daemon thread, so a backend
        # subprocess launch cannot block this GTK main loop. Background mode
        # needs it most, because no config UI opens there to start a backend
        # before the first hardware press.
        gl.plugin_manager.warm_up_plugins()

        # Set this last. Everything above completed, so a re-activation can
        # take the present-only early return.
        self._activate_completed = True

        log.success("Finished loading app")

    def on_reopen(self, *args, **kwargs):
        self.main_win.present()
        log.info("awake")

        self.show_donate(ignore_background_launch=True)

    def let_user_select_asset(self, default_path, callback_func=None, *callback_args, **callback_kwargs):
        # Reuse the window instead of orphaning it with a new one. on_close()
        # nulls gl.asset_manager and self.asset_manager, so this constructs the
        # window on first use, and again after a close.
        if getattr(self, "asset_manager", None) is None:
            self.asset_manager = AssetManager(application=self, main_window=self.main_win)
            gl.asset_manager = self.asset_manager
        self.asset_manager.show_for_path(default_path, callback_func, *callback_args, **callback_kwargs)

    def show_donate(self, ignore_background_launch: bool = False):
        if not ignore_background_launch and gl.argparser.parse_args().b:
            return
        if gl.showed_donate_window:
            return
        gl.showed_donate_window = True

        app_settings = gl.settings_manager.app()

        if not app_settings.show_donate_window:
            return
        if app_settings.app_launches < 4:
            return
        if hasattr(self, "onboarding"):
            return
        if hasattr(self, "permissions"):
            return

        self.donate = DonateWindow()
        self.donate.present(self.main_win)

    def show_onboarding(self):
        if gl.argparser.parse_args().b:
            return
        if os.path.exists(os.path.join(gl.DATA_PATH, ".skip-onboarding")):
            return

        self.onboarding = OnboardingWindow(application=self, main_win=self.main_win)
        self.onboarding.present(self.main_win)

        # Disable onboarding for future sessions
        with open(os.path.join(gl.DATA_PATH, ".skip-onboarding"), "w") as f:
            f.write("")

    def show_permissions(self):
        portal = Xdp.Portal.new()
        if not portal.running_under_flatpak():
            return
        if os.path.exists(os.path.join(gl.DATA_PATH, ".skip-permissions")):
            return
        self.permissions = FlatpakPermissionRequestWindow(application=self, main_window=self.main_win)
        if hasattr(self, "onboarding"):
            if self.onboarding.is_visible():
                return
        self.permissions.present()

    def on_quit(self, *args):
        # Run at most once. Many routes reach here: the TERM and HUP source,
        # which stays armed for every further signal, the main-loop idle that
        # Ctrl+C queues, the Gio quit action, the tray, and the window close
        # handler. A second arrival during a teardown re-destroys the window,
        # triggers AppQuit again, re-runs close_all(), and arms a second
        # force_quit watchdog. The caller continues after this early return.
        if self._quit_started:
            return
        self._quit_started = True

        log.info("Quitting...")

        # Detach the UI first. The media and tick threads keep running, and a
        # push into a window under destruction crashes. The null port makes
        # those threads dirty-mark instead.
        ui_port.install(None)
        # Drop the references of the adapter too: the bound DeckStackChildren,
        # the window, and the per-controller throttle and coalescer state. The
        # uninstall only stops new calls, and it still points at the widget graph
        # while the tick threads wind down. Use getattr, not self._ui_adapter,
        # so a quit that lands before __init__ finished still reaches
        # terminate_all_backends().
        adapter = getattr(self, "_ui_adapter", None)
        if adapter is not None:
            adapter.detach_window()
            self._ui_adapter = None

        stop_dbus_service()

        # Guard the window teardown. A TERM that arrives before on_activate
        # built the window raises AttributeError here and stops the teardown
        # before terminate_all_backends() below, which orphans the backends.
        # main() installs the signal handlers only after it publishes every
        # global that this method reads, so this stays the only guard needed.
        self._destroy_main_window()

        # Force the quit when the normal quit cannot finish. Arm the watchdog
        # before the AppQuit fan-out below. That fan-out runs third-party quit
        # hooks inline, and nothing bounds a hook. A hook that blocks, such as
        # a plugin that waits on a dead socket, parks the quit while no
        # watchdog runs. With the watchdog the block costs 6 s and a
        # force_quit. Everything between here and the deck teardown runs on the
        # watchdog clock.
        timer_wheel.schedule(6, self.force_quit, name="force_quit_timer")

        # Call synchronously, because this process ends in os._exit a few
        # statements below, so an AppQuit handler on the main loop never runs.
        # trigger_signal_sync isolates the handlers, so a plugin that raises in
        # its quit hook only logs and the fan-out continues. An abort here
        # skips close_all(), which leaves a deck open and fails the next
        # startup with TransportError(-1), and skips terminate_all_backends(),
        # which orphans the plugin backends. The _quit_started latch above
        # sends every later quit route to the early return, so an abort here
        # cannot retry.
        gl.signal_manager.trigger_signal_sync(Signals.AppQuit)

        gl.threads_running = False

        # Stop a pending boot re-enumeration before close_all() below. The
        # stop event wakes a rescan that waits in backoff, and the bounded join
        # covers an enumeration in flight, so the rescan cannot register a new
        # controller while the quit path closes the existing ones.
        gl.deck_manager.stop_boot_rescan()

        # Write every page edit that still waits on its debounce timer. Those
        # timers run on daemon threads, and this process ends in os._exit, so a
        # quit within a second of the last edit loses it. This call is an
        # atomic_write_json, a pair of fsyncs with no timeout, so a wedged
        # filesystem costs the 6 s watchdog above instead of the whole quit.
        try:
            page_flush.get().flush_all()
        except Exception as e:
            log.warning(f"Could not write pending page edits during shutdown: {e}")

        # Drain the deferred index writes of the store cache. This process
        # ends in os._exit(0), which skips the StoreCache atexit hook, and the
        # flush timer is a daemon, so without this call every quit loses the
        # last-use clock renewals of the last browse. The guard covers a partly
        # built process, and a failure here must not stop the teardown. This
        # sits after the watchdog above, because the flush is an
        # atomic_write_json and a wedged filesystem blocks it without limit.
        try:
            if gl.store_backend is not None:
                gl.store_backend.store_cache.flush_index()
        except Exception as e:
            log.warning(f"Could not flush the store cache index during shutdown: {e}")

        # Detach the async log sinks before the slow teardown below. Each sink
        # owns a multiprocessing writer queue, and only a handler removal
        # releases its POSIX semaphores. A detach at the end of on_quit lets
        # the force_quit os._exit(1) skip the removal, and the multiprocessing
        # resource_tracker then reports leaked semaphores. The synchronous
        # logs.log and stderr sinks stay up for the remaining messages. A
        # plugin thread that logs after this point loses the record, which
        # affects third-party plugins only. The per-logger guard keeps one
        # failed detach from stopping the teardown.
        for logger_obj in gl.loggers.values():
            try:
                logger_obj.remove_sink()
            except Exception as e:
                log.warning(f"Failed to detach log sink during shutdown: {e}")

        # Run before the close loop below. close_all() submits the terminal
        # ClearAndClose control message and bounds a join on each media thread.
        # Without it, media_player.stop() races a writer that never cleared and
        # closed the device. It also runs before the slow joins. A deck that
        # is still open when force_quit fires fails the next startup with
        # TransportError(-1).
        gl.deck_manager.close_all()

        for ctrl in gl.deck_manager.deck_controller:
            # app_quit=True skips the action teardown, which can run plugin
            # hooks through run_on_main. on_quit already runs on the main
            # thread against the 6 s force_quit timer, and a plugin gains
            # nothing from a notification before os._exit(). close_all() above
            # already drove each writer through ClearAndCloseMsg, so the device
            # close here does nothing.
            ctrl.close(remove_media=True, app_quit=True)

        gl.deck_manager.stop_usb_monitoring()

        gl.plugin_manager.loop_daemon = False

        from src.backend.main_loop import shutdown_background_pool
        shutdown_background_pool()

        # Stop the plugin-event batches, so a late trigger_event() cannot start
        # a new lane thread during the teardown. This joins nothing; lane
        # runners are daemon threads, so a wedged observer cannot delay the quit.
        from src.backend.PluginManager import event_dispatch
        event_dispatch.shutdown()

        for thread in threading.enumerate():
            if thread is not threading.current_thread() and not thread.daemon:
                thread.join(timeout=5)
                if thread.is_alive():
                    log.error(f"Thread {thread.name} did not exit in time")

        # Terminate the plugin and action backend subprocesses. They are the
        # only child processes this app owns.
        gl.plugin_manager.terminate_all_backends()

        gl.tray_icon.stop()

        log.success("Stopped Deckard. Have a nice day!")
        log.stop()
        # Use os._exit, not sys.exit. Interpreter teardown aborts in libusb on
        # the hidapi read thread during exit.
        os._exit(0)

    def _destroy_main_window(self) -> None:
        """Tear down the main window, when a window exists that can go.

        close() is no substitute, because MainWindow.on_close shows the
        keep-running dialog when the setting is unset, and otherwise re-enters
        on_quit through GLib.idle_add.
        """
        main_win = getattr(self, "main_win", None)
        if main_win is None:
            # A TERM arrived before on_activate built the window.
            return
        if not main_win.get_realized():
            # GTK 4.22 segfaults when it disposes a window that never realized.
            # destroy(), remove_window() and set_application(None) all abort
            # there, and only close() and a skip are safe. Background mode
            # builds main_win and skips present(), so a destroy here kills the
            # process before terminate_all_backends() runs, which orphans every
            # plugin backend. An unrealized window holds no surface, so the
            # skip loses nothing.
            log.debug("Main window was never realized (background mode); "
                      "skipping destroy to avoid the GTK unrealized-dispose "
                      "abort")
            return
        try:
            main_win.destroy()
        except Exception as e:
            # The first statement of MainWindow.__init__ publishes main_win
            # before the build can fail, so this can be a half-built window.
            # This except cannot catch the unrealized-dispose abort, which is
            # native and not a Python exception.
            log.warning(f"Failed to destroy the main window during shutdown: {e}")

    def force_quit(self):
        log.info("Forcing quit...")
        # Last chance to reap the plugin backends. They start with
        # start_new_session=True, so nothing kills them after this os._exit.
        # The call does not block, it is one killpg per backend, it is safe
        # from the timer-wheel dispatch thread, and it can run beside a
        # concurrent on_quit.
        try:
            gl.plugin_manager.terminate_all_backends()
        except Exception as e:
            log.warning(f"Failed to terminate plugin backends during force quit: {e}")
        os._exit(1)

    def _on_unix_signal(self, *args):
        """SIGTERM and SIGHUP entry point. Runs on_quit and keeps the source.

        The Gio quit action and the GLib.idle_add(on_quit) routes do not use
        this method, because a true return on an idle source means run again,
        which spins the main loop.
        """
        # An exception from on_quit propagates. GLib then drops the source, and
        # a later TERM kills the process, which keeps a broken teardown from
        # making the app immune to TERM.
        self.on_quit()
        # GLib destroys a unix-signal source whose callback returns a false
        # value, and it restores SIG_DFL for that signum, so the next TERM
        # kills the process. on_quit returns through its _quit_started latch
        # once a teardown runs, so a plain return disarms the handler.
        return GLib.SOURCE_CONTINUE

    def _on_sigint(self, signum, frame):
        """SIGINT entry point. Queues the teardown, and escalates on a wedge.

        The _quit_started gate keeps a press during a running teardown a no-op.
        """
        now = time.monotonic()
        if self._sigint_first_at is None:
            self._sigint_first_at = now
        elif (not self._quit_started
                and now - self._sigint_first_at >= SIGINT_ESCALATE_AFTER_S):
            # The main loop dispatches the queued on_quit, so a press on a
            # wedged loop never arrives. TERM and HUP are loop sources too, so
            # only SIGKILL ends such a process, and SIGKILL orphans the plugin
            # backends and skips the force_quit watchdog that on_quit arms.
            log.warning(
                f"Interrupt requested {now - self._sigint_first_at:.1f}s ago and "
                f"the teardown never started (the main loop is not dispatching); "
                f"forcing quit"
            )
            # Back stop. This handler and force_quit both log, a log sink
            # takes a lock, and a wedge inside one swallows the escalation. A
            # further Ctrl+C then kills the process.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            # force_quit is the one call that is safe from handler context,
            # because it only terminates the backends and calls os._exit(1).
            self.force_quit()
            return
        # A Python handler runs between bytecodes on the main thread, so it can
        # interrupt a render, a GTK callback, or a section that holds a lock.
        # The main loop runs on_quit instead, which is where the TERM and HUP
        # sources run it, so every signal route uses one teardown context.
        # PRIORITY_DEFAULT matches those sources, because the default idle
        # priority sits below the GTK frame-clock redraws.
        GLib.idle_add(self.on_quit, priority=GLib.PRIORITY_DEFAULT)

    def register_signal_handlers(self):
        # SIGINT stays a Python-level handler. The PyGObject wakeup-fd bridge
        # fires it promptly under the GLib loop, and a custom handler keeps
        # register_sigint_fallback in Gio.Application.run inert. That fallback
        # reads signal.getsignal(SIGINT), cannot see a GLib unix-signal source,
        # and installs its own handler that routes Ctrl+C to app.quit() and
        # skips the on_quit teardown. The handler body only queues onto the main
        # loop. See _on_sigint.
        signal.signal(signal.SIGINT, self._on_sigint)
        # SIGTERM and SIGHUP use GLib-native sources on the main loop, so a
        # logout TERM runs the full teardown, and terminate_all_backends() in
        # particular. The backends run in their own session, so no killpg
        # reaches them. Register here, not at loop start, because GLib installs
        # its sigaction at once, so a signal before the loop stays pending. The
        # route uses _on_unix_signal for the return value it needs.
        for signum in (signal.SIGTERM, signal.SIGHUP):
            if unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_unix_signal):
                continue
            # This GLib has no introspectable unix-signal source. A Python
            # handler still runs the full teardown, and it fires between
            # bytecodes instead of as a loop source.
            log.warning(
                f"No GLib unix-signal source available for {signum}; falling "
                f"back to a Python-level handler"
            )
            signal.signal(signum, self._on_unix_signal)

    def add_signals(self):
        self.update_all_assets_action = Gio.SimpleAction.new("update-all-assets", None)
        self.update_all_assets_action.connect("activate", self.update_all_assets)
        self.add_action(self.update_all_assets_action)

        self.install_plugin_action = Gio.SimpleAction.new("install-plugin", GLib.VariantType("s"))
        self.install_plugin_action.connect("activate", self.install_plugin)
        self.add_action(self.install_plugin_action)

    def update_all_assets(self, *args, **kwargs):
        threading.Thread(target=self._update_all_assets, name="update_all_assets").start()

    @log.catch
    def _update_all_assets(self):
        self.set_working(True)

        result = gl.store_backend.update_everything()

        self.set_working(False)

        # update_everything returns Ok(count) or Err. A failure must not
        # report success.
        if isinstance(result, Ok):
            gl.app.send_notification("dialog-information-symbolic", "Assets updated",
                                     f"{result.value} assets have been updated")
        else:
            gl.app.send_notification("dialog-information-symbolic", "Asset update failed",
                                     "Could not reach the store to update assets")

    def install_plugin(self, action, plugin_id: GLib.Variant):
        plugin_id = plugin_id.unpack()
        threading.Thread(target=self._install_plugin, args=(plugin_id,), name="install_plugin").start()

    @log.catch
    def _install_plugin(self, plugin_id: str):
        store_backend = gl.store_backend
        if store_backend is None:
            log.error(f"Cannot install plugin {plugin_id}: no store backend")
            return
        plugin = store_backend.get_plugin_for_id(plugin_id=plugin_id)

        self.set_working(True)

        if plugin is None:
            self.send_notification("dialog-information-symbolic", "Failed to install plugin",
                                   f"The plugin {plugin_id} could not be installed")
            self.set_working(False)
            return

        result = store_backend.install_plugin(plugin)
        # install_plugin returns a StoreResult. Err is a failure, and the other
        # value is the single Ok. Narrow the type, do not test truth.
        if isinstance(result, Err):
            self.send_notification("dialog-information-symbolic", "Failed to install plugin",
                                   f"The plugin {plugin_id} could not be installed")
        else:
            self.send_notification("dialog-information-symbolic", "Plugin installed",
                                   f"The plugin {plugin_id} was successfully installed")

        self.set_working(False)            

    def set_working(self, working: bool) -> None:
        # Use self, not gl.app. This is an App method, so the application
        # object exists, and gl.app is this same instance.
        if working:
            GLib.idle_add(self.mark_busy)
            GLib.idle_add(self.main_win.set_cursor_from_name, "wait")
        else:
            GLib.idle_add(self.unmark_busy)
            GLib.idle_add(self.main_win.set_cursor_from_name, "default")

    def send_notification(self,  # type: ignore[override]  # shadows Gio.Application.send_notification with the (icon, title, body) form of this app; the parent_send binding below reaches the base signature
                          icon_name: str,
                          title: str,
                          body: str,
                          button: tuple[str, str, GLib.Variant] = None,
                          category: str = "im.error") -> None:
        """Safe from any thread, because the body runs on the GTK main thread.

        Most callers are background threads, such as store installs and plugin
        loads, so the settings read and the Gio.Notification construction must
        marshal too.
        """
        parent_send = super().send_notification

        def _send() -> bool:
            if not gl.settings_manager.app().show_notifications:
                return GLib.SOURCE_REMOVE

            notif = Gio.Notification()
            notif.set_icon(Gio.Icon.new_for_string(icon_name))
            notif.set_category(category)
            notif.set_title(title)
            notif.set_body(body)
            if button:
                notif.add_button_with_target(button[0], button[1], button[2])

            parent_send(appinfo.APP_ID, notif)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_send)

    def send_outdated_plugin_notification(self, plugin_id: str) -> None:
        self.send_notification(
            "software-update-available-symbolic",
            "Plugin out of date",
            f"The plugin {plugin_id} is out of date and needs to be updated"
        )

    def send_missing_plugin_notification(self, plugin_id: str) -> None:
        self.send_notification(
            "dialog-information-symbolic",
            "Plugin missing",
            f"The plugin {plugin_id} is missing. Please install it.",
            button=("Install", "app.install-plugin", GLib.Variant.new_string(plugin_id))
        )
    def open_store(self, callback_agreed: bool = None) -> None:
        agreed = gl.settings_manager.app().responsibility_notes_agreed

        if not agreed:
            if callback_agreed is None:
                resp_dialog = ResponsibleNotesDialog(self.get_active_window(), self.open_store)
                resp_dialog.present()
            return
        
        if gl.store is None:
            gl.store = Store(application=self, main_window=self.main_win)
        gl.store.present()
