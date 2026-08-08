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
# Import python modules
import signal
import threading
import gi

from src.windows.Store.ResponsibleNotesDialog import ResponsibleNotesDialog
from src.windows.Donate.DonateWindow import DonateWindow

# Import globals first to get IS_MAC
import appinfo
import globals as gl

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
if not gl.IS_MAC:
    gi.require_version("Xdp", "1.0")

from gi.repository import Gtk, Adw, Gdk, Gio, GLib
if not gl.IS_MAC:
    from gi.repository import Xdp

# Import Python modules
from loguru import logger as log
import os

# Import own modules
from src.backend import timer_wheel
from src.windows.mainWindow.mainWindow import MainWindow
from src.windows.AssetManager.AssetManager import AssetManager
from src.windows.Store.Store import Store
from src.windows.Shortcuts.Shortcuts import ShortcutsWindow
from src.windows.Onboarding.OnboardingWindow import OnboardingWindow
from src.windows.Permissions.FlatpakPermissionRequest import FlatpakPermissionRequestWindow
from src.backend.DeckManagement.InputIdentifier import Input

from src.Signals import Signals
from src.api import start_dbus_service, stop_dbus_service

# Import globals
import globals as gl


def unix_signal_add(priority, signum, callback) -> bool:
    """Install a GLib main-loop source for `signum`; returns whether one was
    installed.

    GLib 2.80 moved the Unix-specific API out of the GLib-2.0 introspection
    namespace into a separate GLibUnix-2.0 one, so exactly one of the two
    spellings exists depending on the runtime's GLib -- GLib.unix_signal_add
    on older distros, GLibUnix.signal_add on current ones (and in the
    org.gnome.Platform//50 flatpak runtime). Try both rather than pinning
    either. Returns False if neither is introspectable, leaving the caller to
    decide how to degrade.
    """
    add = getattr(GLib, "unix_signal_add", None)
    if add is None:
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            add = GLibUnix.signal_add
        except (ImportError, ValueError, AttributeError):
            return False
    try:
        add(priority, signum, callback)
    except Exception as e:
        # Resolving the symbol is not the same as it working: a signum
        # g_unix_signal_add refuses, a GLib built without UNIX signal support,
        # or an argument-marshalling mismatch between the two spellings all
        # raise here. Degrading is this helper's entire contract -- letting it
        # escape would propagate out of App.__init__ and abort startup, which
        # is a far worse outcome than a Python-level handler.
        log.warning(f"Could not install a GLib unix-signal source for {signum}: {e}")
        return False
    return True


class App(Adw.Application):
    def __init__(self, deck_manager, **kwargs):
        super().__init__(**kwargs)
        self.deck_manager = deck_manager

        # Re-entry latch for on_quit (issue #169). Set before the handlers are
        # registered below so the very first signal already sees it.
        self._quit_started = False

        self.register_signal_handlers()

        self.connect("activate", self.on_activate)

        css_provider = Gtk.CssProvider()
        css_provider.load_from_path(os.path.join(gl.top_level_dir, "style.css"))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon_theme.add_search_path(os.path.join(gl.top_level_dir, "Assets", "icons"))

        app_settings = gl.settings_manager.app()

        allow_white_mode = app_settings.allow_white_mode

        # increment app launches
        app_settings.app_launches = app_settings.app_launches + 1
        app_settings.save()

        self.style_manager = self.get_style_manager()
        if allow_white_mode:
            self.style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        else:
            self.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK) # Not everything looks good in light mode at the moment #TODO

    def on_activate(self, app):
        log.trace("running: on_activate")
        if getattr(self, "_activate_completed", False):
            # Remote activation: a second launch was forwarded here by
            # GApplication. Rebuilding the window would orphan every
            # gl.app.main_win consumer and the controllers' cached UI
            # bindings (issue #158, field 2026-07-16: the replacement was
            # also never presented because the boot argv had -b, so preview
            # pushes dirty-marked forever). Delegate to on_reopen so this
            # route behaves exactly like the single-instance probe's
            # Activate("reopen") -- one code path for re-activation.
            #
            # Guarded by an explicit completion flag, NOT by main_win:
            # MainWindow.__init__'s first statement publishes itself as
            # gl.app.main_win (= self.main_win) before construction can
            # still fail, so a main_win guard would latch on a failed first
            # build and permanently present a half-built window with the
            # reopen action never registered. On such a failure this guard
            # stays False and the next activation retries the full build.
            self.on_reopen()
            return
        self.main_win = MainWindow(application=app, deck_manager=self.deck_manager)
        if not gl.argparser.parse_args().b:
            self.main_win.present()

        self.show_onboarding()
        # Called directly: MainWindow.do_after_build_tasks() drains
        # on_finished synchronously during the constructor above, so anything
        # added to that list here would never run. The old form appended
        # show_donate()'s None result -- it only worked because the call
        # happened while building the argument.
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

        # Do tasks. Drain by atomic pop, not iterate-then-clear: background
        # threads (gl.notify) race their appends against this drain, and a
        # task appended mid-iteration would be cleared unrun. pop(0) makes
        # every task owned by exactly one side -- this loop or the appender's
        # post-append reclaim (see Notify._dispatch) -- and a task that
        # appends further tasks while running gets those drained too.
        gl.app = self
        while gl.app_loading_finished_tasks:
            task = gl.app_loading_finished_tasks.pop(0)
            if callable(task):
                task()
        change_page_action = Gio.SimpleAction.new("change_page", GLib.VariantType("as")) # as = array of strings
        change_page_action.connect("activate", self.on_change_page)
        self.add_action(change_page_action)

        change_state_action = Gio.SimpleAction.new("change_state", GLib.VariantType("as")) # as = array of strings
        change_state_action.connect("activate", self.on_change_state)
        self.add_action(change_state_action)

        # Start DBus API service
        if not gl.IS_MAC:
            start_dbus_service()

        # Eagerly warm plugin backends (issue #117): async on its own daemon
        # thread, so backend subprocess launches can never block this GTK
        # main loop. Matters most in background mode (-b), where no config
        # UI ever opens to trigger lazy backend init before the first
        # hardware press.
        gl.plugin_manager.warm_up_plugins()

        # Last: everything above ran to completion, so re-activations may
        # take the present-only early return from now on.
        self._activate_completed = True

        log.success("Finished loading app")

    def on_reopen(self, *args, **kwargs):
        self.main_win.present()
        log.info("awake")

        self.show_donate(ignore_background_launch=True)

    def let_user_select_asset(self, default_path, callback_func=None, *callback_args, **callback_kwargs):
        # Reuse the existing window instead of orphaning it with a new one (P4.2):
        # on_close() nulls out both gl.asset_manager and self.asset_manager, so this
        # only (re)constructs the window on first use or after it has been closed.
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
        if gl.IS_MAC:
            return
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
        # Run at most once (issue #169). SIGINT is a Python-level handler, so
        # it fires between bytecodes on the main thread: a Ctrl+C landing
        # during a teardown already in flight would otherwise re-enter here
        # and re-destroy the window, re-trigger AppQuit, re-run close_all()
        # and arm a second force_quit watchdog. The interrupted outer frame
        # resumes normally after this early return.
        if self._quit_started:
            return
        self._quit_started = True

        log.info("Quitting...")

        # Stop DBus API service
        if not gl.IS_MAC:
            stop_dbus_service()

        # Guarded: a TERM arriving before on_activate built the window
        # (autostart followed by an immediate logout, or a startup crash-loop
        # kill) would raise AttributeError here and abort teardown *before*
        # terminate_all_backends() below -- exactly the orphan this issue is
        # about. Everything else on this path is created in main.py before
        # App exists, or is internally guarded.
        self._destroy_main_window()

        # Guarded for the same reason as the window teardown above, and it
        # matters more here: SignalManager.trigger_signal invokes AppQuit
        # handlers synchronously and *unwrapped*, so one third-party plugin
        # raising in its quit hook aborts on_quit right here -- before
        # close_all() (a deck left open fails the next startup with
        # TransportError(-1)), before terminate_all_backends() (the orphaned
        # backends this issue is about) and before the force_quit watchdog
        # below is even armed. With the _quit_started latch above, that abort
        # is now permanent: every later quit route (tray, Gio "quit" action,
        # Ctrl+C, TERM) takes the early return instead of retrying.
        try:
            gl.signal_manager.trigger_signal(Signals.AppQuit)
        except Exception as e:
            log.warning(f"An AppQuit handler failed during shutdown: {e}")

        gl.threads_running = False

        # Stop a pending boot re-enumeration (issue #106) before close_all()
        # below: the stop event wakes a rescan parked in backoff immediately,
        # and the bounded join covers an in-flight enumeration -- so the
        # rescan can't register a fresh controller while the quit path tears
        # the existing ones down (same residual window as a hotplug event
        # arriving mid-quit, which the USB monitor has always had).
        gl.deck_manager.stop_boot_rescan()

        # Force quit if normal quit is not possible
        timer_wheel.schedule(6, self.force_quit, name="force_quit_timer")

        # Detach the async (enqueue=True) log sinks now, before the slow
        # teardown below. Each owns a multiprocessing writer queue whose POSIX
        # semaphores are only released when the handler is removed; deferring
        # that to the end of on_quit lets the force_quit os._exit(1) fallback
        # (6s timer above) -- or any exit on a shutdown that overruns it --
        # skip it, and the multiprocessing resource_tracker then reports the
        # queue's semaphores as "leaked ... at shutdown". The synchronous
        # logs.log/stderr sinks stay up for the remaining shutdown messages.
        # Trade-off: PLUGIN-level records emitted by plugin threads after this
        # point are dropped (not written to plugins.log); no in-repo teardown
        # uses the plugin logger, so this only affects third-party plugins
        # logging during their own shutdown. Guarded per-logger so one failing
        # detach cannot abort the rest of teardown.
        for logger_obj in gl.loggers.values():
            try:
                logger_obj.remove_sink()
            except Exception as e:
                log.warning(f"Failed to detach log sink during shutdown: {e}")

        # Must run BEFORE the delete() loop (plan §2.4): close_all() submits
        # the terminal ClearAndClose control message and bounds a join on
        # each media thread. delete()'s media_player.stop() would otherwise
        # race a writer that never got the chance to clear+close the device.
        # It also must run before the slow joins below: a deck still open
        # when force_quit fires fails the next startup with TransportError(-1).
        gl.deck_manager.close_all()

        for ctrl in gl.deck_manager.deck_controller:
            # app_quit=True (plan P1.3): skips action teardown (step 6),
            # which can run plugin hooks via run_on_main -- on_quit is
            # already running on main against the 6s force_quit timer above,
            # and plugin notification doesn't matter when the process is
            # about to os._exit() anyway. close_all() just above already
            # drove each controller's writer through ClearAndCloseMsg, so
            # close()'s own step 5 is a fast no-op here.
            ctrl.close(remove_media=True, app_quit=True)

        gl.deck_manager.stop_usb_monitoring()

        gl.plugin_manager.loop_daemon = False

        from src.backend.main_loop import shutdown_background_pool
        shutdown_background_pool()

        # Stop accepting plugin-event batches, so a late trigger_event()
        # can't spawn a fresh lane thread mid-teardown. Nothing is joined:
        # lane runners are daemon threads, so a wedged observer cannot delay
        # quit (issue #178).
        from src.backend.PluginManager import event_dispatch
        event_dispatch.shutdown()

        for thread in threading.enumerate():
            if thread is not threading.current_thread() and not thread.daemon:
                thread.join(timeout=5)
                if thread.is_alive():
                    log.error(f"Thread {thread.name} did not exit in time")

        # Terminate plugin/action backend subprocesses -- the only child
        # processes we own. (This used to be preceded by a
        # multiprocessing.active_children() terminate loop; the fork-per-spawn
        # wrapper inside run_command was its only source and is gone.)
        gl.plugin_manager.terminate_all_backends()

        gl.tray_icon.stop()

        log.success("Stopped Deckard. Have a nice day!")
        log.stop()
        # os._exit, not sys.exit: interpreter teardown aborts in libusb on the
        # hidapi read thread during exit.
        os._exit(0)

    def _destroy_main_window(self) -> None:
        """Tear down the main window, if there is one that can be torn down.

        GTK 4.22 segfaults disposing a window that was never realized (issue
        #193, reproduced down to a bare Gtk/Adw.ApplicationWindow: destroy(),
        remove_window() and set_application(None) all abort on the unrealized
        dispose path; only close() and leaving it alone are safe). In
        background mode (-b -- the autostart path) on_activate builds
        main_win but skips present(), so the window is never realized and
        this statement killed the process mid-teardown, before
        terminate_all_backends() below: every plugin backend was orphaned on
        every quit, which is the symptom #169 set out to fix.

        close() is not a substitute: MainWindow.on_close pops the
        keep-running dialog when the setting is unset and otherwise
        re-enters on_quit through GLib.idle_add.

        Skipping is correct rather than merely safe -- an unrealized window
        owns no surface to release, and the process os._exit()s a few lines
        below regardless.
        """
        main_win = getattr(self, "main_win", None)
        if main_win is None:
            # A TERM arriving before on_activate built the window (autostart
            # followed by an immediate logout, or a startup crash-loop kill).
            return
        if not main_win.get_realized():
            log.debug("Main window was never realized (background mode); "
                      "skipping destroy to avoid the GTK unrealized-dispose "
                      "abort (#193)")
            return
        try:
            main_win.destroy()
        except Exception as e:
            # main_win is published by MainWindow.__init__'s first statement,
            # before the build can still fail (see on_activate), so this can
            # be a half-built window. Note this cannot catch #193 itself --
            # that is a native abort, not a Python exception.
            log.warning(f"Failed to destroy the main window during shutdown: {e}")

    def force_quit(self):
        log.info("Forcing quit...")
        # Last chance to reap the plugin backends (issue #169): they are
        # spawned with start_new_session=True, so once this os._exit lands
        # nothing else kills them -- a wedged teardown that never reached
        # on_quit's terminate_all_backends() would orphan them. Non-blocking
        # (escalate=False is just a killpg per backend), safe from the timer
        # wheel's dispatch thread, and idempotent against a concurrent
        # on_quit (snapshot copy + ProcessLookupError swallowed).
        try:
            gl.plugin_manager.terminate_all_backends()
        except Exception as e:
            log.warning(f"Failed to terminate plugin backends during force quit: {e}")
        os._exit(1)

    def _on_unix_signal(self, *args):
        """SIGTERM/SIGHUP entry point. Runs on_quit, then keeps the source.

        Not just `on_quit` itself, because of the return value. A unix-signal
        source whose callback returns falsy is destroyed, and GLib restores
        SIG_DFL for that signum along with it (verified: the *next* TERM then
        kills the process outright). on_quit normally never returns -- but its
        _quit_started latch does, so once a teardown is in flight every
        further TERM/HUP would fall through the latch, return None, disarm the
        handler, and hand the next signal back to the default disposition,
        killing the process mid-teardown with the backends still running --
        precisely the failure issue #169 is about. SOURCE_CONTINUE keeps the
        source armed for as long as the process lives.

        Deliberately not reused for the Gio "quit" action or the
        GLib.idle_add(on_quit) routes (mainWindow.on_close, the hamburger
        menu): on an idle source a truthy return means "run me again", which
        would spin the main loop. on_quit's plain None is right for those.

        An exception from on_quit is *not* swallowed: it propagates, GLib
        drops the source, and a follow-up TERM hard-kills. That escalation is
        wanted -- a deterministically broken teardown must not leave the app
        immune to TERM.
        """
        self.on_quit()
        return GLib.SOURCE_CONTINUE

    def register_signal_handlers(self):
        # SIGINT stays a Python-level handler: PyGObject's wakeup-fd bridge
        # makes it fire promptly under the GLib loop, and having a custom
        # handler installed keeps Gio.Application.run's register_sigint_fallback
        # inert -- that fallback checks signal.getsignal(SIGINT), cannot see a
        # GLib unix-signal source, and would install its own handler routing
        # Ctrl+C to app.quit(), bypassing on_quit's whole teardown.
        signal.signal(signal.SIGINT, self.on_quit)
        # SIGTERM/SIGHUP (issue #169): GLib-native sources dispatched on the
        # main loop, so a logout/systemd TERM runs the full teardown -- notably
        # terminate_all_backends(), without which the backends (own session,
        # so no killpg reaches them) are orphaned. Registered here rather than
        # at loop start: GLib installs its sigaction immediately, so a signal
        # arriving before the loop runs is held pending, not lost. Routed via
        # _on_unix_signal rather than on_quit directly -- see its docstring
        # for why the source's return value is not moot.
        for signum in (signal.SIGTERM, signal.SIGHUP):
            if unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_unix_signal):
                continue
            # No introspectable unix-signal source on this GLib. A Python-level
            # handler still runs the full teardown (that is what matters here);
            # it just fires between bytecodes instead of as a loop source.
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

        # update_everything returns the number of successfully updated
        # assets, or NoConnectionError -- don't toast success on failure.
        if isinstance(result, int):
            gl.app.send_notification("dialog-information-symbolic", "Assets updated",
                                     f"{result} assets have been updated")
        else:
            gl.app.send_notification("dialog-information-symbolic", "Asset update failed",
                                     "Could not reach the store to update assets")

    def install_plugin(self, action, plugin_id: GLib.Variant):
        plugin_id = plugin_id.unpack()
        threading.Thread(target=self._install_plugin, args=(plugin_id,), name="install_plugin").start()

    @log.catch
    def _install_plugin(self, plugin_id: str):
        plugin = gl.store_backend.get_plugin_for_id(plugin_id=plugin_id)

        self.set_working(True)

        if plugin is None:
            gl.app.send_notification("dialog-information-symbolic", "Failed to install plugin",
                                     f"The plugin {plugin_id} could not be installed")
            self.set_working(False)
            return
        
        success = gl.store_backend.install_plugin(plugin)
        # Success is exactly True -- failure returns include truthy ints
        # (404/400), which "if not success" misread as installed.
        if success is not True:
            gl.app.send_notification("dialog-information-symbolic", "Failed to install plugin",
                                     f"The plugin {plugin_id} could not be installed")
        else:
            gl.app.send_notification("dialog-information-symbolic", "Plugin installed",
                                     f"The plugin {plugin_id} was successfully installed")

        self.set_working(False)            

    def set_working(self, working: bool) -> None:
        if working:
            GLib.idle_add(gl.app.mark_busy)
            GLib.idle_add(gl.app.main_win.set_cursor_from_name, "wait")
        else:
            GLib.idle_add(gl.app.unmark_busy)
            GLib.idle_add(gl.app.main_win.set_cursor_from_name, "default")

    def send_notification(self,
                          icon_name: str,
                          title: str,
                          body: str,
                          button: tuple[str, str, GLib.Variant] = None,
                          category: str = "im.error") -> None:
        """Safe to call from any thread: the ENTIRE body runs on the GTK main
        thread. Marshalling only the final super() call left the settings read
        and the Gio.Notification construction on the caller's thread, and most
        callers are background threads (store installs, plugin loads)."""
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

    def on_change_page(self, action, data: GLib.Variant, *args):
        """
        page_name can be either the name or the path of the page
        """
        serial_number, page_name = data.unpack()

        for controller in self.deck_manager.deck_controller:
            if controller.serial_number() == serial_number:
                page_path = gl.page_manager.find_matching_page_path(page_name)
                if page_path is None:
                    # Page not found - provide helpful suggestions
                    available_pages = [os.path.splitext(os.path.basename(p))[0] for p in gl.page_manager.get_pages()]
                    log.error(f"Page '{page_name}' not found. Available pages: {', '.join(available_pages)}")
                    continue

                if controller.active_page is not None:
                    if os.path.abspath(page_path) == os.path.abspath(controller.active_page.json_path):
                        continue

                page = gl.page_manager.get_page(page_path, controller)
                controller.load_page(page)

    def on_change_state(self, action, data: GLib.Variant, *args):
        """
        Change the state of a specific StreamDeck item
        """
        serial_number, page_name, coords, state_number = data.unpack()
        
        # Find the controller with matching serial number
        target_controller = None
        for controller in self.deck_manager.deck_controller:
            if controller.serial_number() == serial_number:
                target_controller = controller
                break
        
        if target_controller is None:
            # Serial number not found - provide helpful suggestions
            available_serials = [c.serial_number() for c in self.deck_manager.deck_controller]
            if available_serials:
                log.error(f"StreamDeck with serial '{serial_number}' not found. Available devices: {', '.join(available_serials)}")
            else:
                log.error("No StreamDeck devices connected")
            return

        # Find the requested page
        page_path = gl.page_manager.find_matching_page_path(page_name)
        if page_path is None:
            # Page not found - provide helpful suggestions
            available_pages = [os.path.splitext(os.path.basename(p))[0] for p in gl.page_manager.get_pages()]
            log.error(f"Page '{page_name}' not found. Available pages: {', '.join(available_pages)}")
            return

        # Load the page if not already active
        if target_controller.active_page is None or os.path.abspath(page_path) != os.path.abspath(target_controller.active_page.json_path):
            page = gl.page_manager.get_page(page_path, target_controller)
            target_controller.load_page(page)

        # Parse and validate coordinates
        try:
            x, y = map(int, coords.split(','))
        except (ValueError, AttributeError):
            log.error(f"Invalid coordinate format '{coords}'. Expected format: 'x,y' (e.g., '0,0')")
            return

        # Validate coordinates are within deck bounds
        rows, cols = target_controller.deck.key_layout()
        if x < 0 or x >= cols or y < 0 or y >= rows:
            log.error(f"Coordinates ({x},{y}) are out of bounds for this device. Valid range: x=0-{cols-1}, y=0-{rows-1}")
            return

        # Create the input identifier for the key
        identifier = Input.Key(f"{x}x{y}")
        c_input = target_controller.get_input(identifier)
        
        if c_input is None:
            log.error(f"Could not find input at coordinates ({x},{y})")
            return

        # Validate state number
        try:
            state_num = int(state_number)
        except ValueError:
            log.error(f"Invalid state number '{state_number}'. Must be an integer")
            return

        # Check if the requested state exists
        if state_num < 0 or state_num >= len(c_input.states):
            max_state = len(c_input.states) - 1
            if max_state == 0:
                log.error(f"Position ({x},{y}) only has 1 state (state 0). Requested state {state_num} does not exist")
            else:
                log.error(f"Position ({x},{y}) has {len(c_input.states)} states (0-{max_state}). Requested state {state_num} does not exist")
            return

        # Successfully change to the specified state
        c_input.set_state(state_num)
        log.info(f"Successfully changed state of ({x},{y}) to state {state_num} on device {serial_number}")

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
