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
import time
from typing import TYPE_CHECKING

import gi

from src.windows.Store.ResponsibleNotesDialog import ResponsibleNotesDialog
from src.windows.Donate.DonateWindow import DonateWindow

# Import globals
import appinfo
import globals as gl

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Xdp", "1.0")

from gi.repository import Gtk, Adw, Gdk, Gio, GLib, Xdp

# Import Python modules
from loguru import logger as log
import os

# Import own modules
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

# Import globals
import globals as gl


# How long a queued Ctrl+C may go undispatched before the next one force-quits
# from signal-handler context (see App._on_sigint). A third of the 6s
# force_quit watchdog: far longer than any dispatch latency a responsive main
# loop can produce, and short enough that someone holding Ctrl+C against a real
# wedge reaches it without thinking about it.
SIGINT_ESCALATE_AFTER_S = 2.0


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
            from gi.repository import GLibUnix  # type: ignore[attr-defined]  # gi stub: PyGObject-stubs ships no GLibUnix-2.0; presence is a runtime property of the host GLib, which is exactly what this try/except probes
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
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Re-entry latch for on_quit. Set at construction so the very first
        # signal already sees it, whenever the handlers go up.
        self._quit_started = False

        # When the first Ctrl+C was seen, for _on_sigint's escalation; None
        # until then. Same reason for living here as the latch above: the
        # very first signal must already find it.
        self._sigint_first_at: float | None = None

        # The live engine->UI adapter, so on_quit can detach it. None
        # until on_activate builds the window -- a TERM before that must not
        # raise here.
        self._ui_adapter = None

        # Both are filled by on_activate, and both are reached through gl.app
        # by other windows -- which is published before the loop starts, so an
        # attribute that did not exist yet would be an AttributeError rather
        # than the None those readers can see coming.
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
            # Remote activation: a second launch was forwarded here by
            # GApplication. Rebuilding the window would orphan every
            # gl.app.main_win consumer and the controllers' cached UI
            # bindings (field incident 2026-07-16: the replacement was
            # also never presented because the boot argv had -b, so preview
            # pushes dirty-marked forever). Delegate to on_reopen so a
            # forwarded activation and the kept reopen action share one
            # re-activation code path.
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

        # Everything from here down needs the global objects, and this
        # application is constructed before any of them exist -- it has to be,
        # because registering it is what decides whether this launch boots at
        # all. By the time the main loop starts and activates it, the deck
        # manager and the settings manager are both up.
        self.deck_manager = gl.deck_manager

        app_settings = gl.settings_manager.app()

        allow_white_mode = app_settings.allow_white_mode

        # increment app launches. Below the re-activation return above, so a
        # second launch presenting this window through the framework counts as
        # the reopen it is rather than as a launch of its own.
        app_settings.app_launches = app_settings.app_launches + 1
        app_settings.save()

        self.style_manager = self.get_style_manager()
        if allow_white_mode:
            self.style_manager.set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        else:
            self.style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK) # Not everything looks good in light mode at the moment #TODO

        # Install the engine->UI adapter BEFORE constructing the window
        # first: every boot-time add_page runs INSIDE MainWindow's
        # constructor (build() -> leftArea -> deck_stack.add_pages()), so an
        # adapter installed afterwards would miss every bind and leave all
        # previews permanently dirty-marked. attach_window() runs after, for
        # the map/unmap handlers, and re-scans the stack so any child added
        # during construction is bound regardless.
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

        # Publish first, drain second: the ordering is what lets an appender
        # racing this drain reclaim its own task instead of stranding it
        # (ownership rules in src/backend/startup_queue.py).
        gl.app = self
        startup_queue.get().drain_app_ready()

        # Eagerly warm plugin backends: async on its own daemon
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
        # Run at most once. Several routes lead here -- the TERM/HUP source
        # (which stays armed and dispatches again on every further signal), the
        # main-loop idle a Ctrl+C queues, the Gio "quit" action, the tray, the
        # window's close handler -- and a second arrival during a teardown
        # already in flight would otherwise re-destroy the window, re-trigger
        # AppQuit, re-run close_all() and arm a second force_quit watchdog.
        # The caller resumes normally after this early return.
        if self._quit_started:
            return
        self._quit_started = True

        log.info("Quitting...")

        # Detach the UI first: the media/tick threads keep running for a
        # while yet, and a push into a window that is being destroyed is a
        # crash shape. The null port makes them dirty-mark instead.
        ui_port.install(None)
        # Drop the adapter's own references too (bound DeckStackChildren, the
        # window, per-controller throttle/coalescer state): uninstalling only
        # stops new calls, it does not release the widget graph the adapter
        # still points at while the tick threads wind down.
        # getattr, not self._ui_adapter: guarded for the same reason as the
        # window teardown below -- a quit landing before __init__ finished
        # must still reach terminate_all_backends().
        adapter = getattr(self, "_ui_adapter", None)
        if adapter is not None:
            adapter.detach_window()
            self._ui_adapter = None

        # Stop DBus API service
        stop_dbus_service()

        # Guarded: a TERM arriving before on_activate built the window
        # (autostart followed by an immediate logout, or a startup crash-loop
        # kill) would raise AttributeError here and abort teardown *before*
        # terminate_all_backends() below -- exactly the orphan this issue is
        # about. Everything else on this path is either internally guarded or
        # in place before the signal handlers exist to route a quit here:
        # main() installs them only once every global this method dereferences
        # has been published, which is what keeps this the only guard needed.
        self._destroy_main_window()

        # Force quit if normal quit is not possible.
        # Deliberately armed BEFORE the AppQuit fan-out below rather than after
        # the deck teardown: that fan-out runs third-party quit hooks inline
        # and nothing bounds how long one of them takes, so a hook that BLOCKS
        # (a plugin waiting on a dead socket, say -- no exception for the
        # fan-out to contain) parks the quit for good if the watchdog is not up
        # yet. Behind it that costs 6s and a force_quit; ahead of it the quit
        # hangs with nothing armed to end the process. Everything between here
        # and the deck teardown is therefore on the watchdog's clock, which is
        # the intent.
        timer_wheel.schedule(6, self.force_quit, name="force_quit_timer")

        # Synchronous on purpose: this process ends in os._exit a few
        # statements below, so AppQuit handlers queued on the main loop would
        # never run. trigger_signal_sync isolates them from each other -- one
        # third-party plugin raising in its quit hook is logged and the fan-out
        # moves on, rather than aborting on_quit here -- before close_all() (a
        # deck left open fails the next startup with TransportError(-1)) and
        # before terminate_all_backends() (orphaned plugin backends). An abort
        # here is also unrecoverable: the _quit_started latch above sends every
        # later quit route (tray, Gio "quit" action, Ctrl+C, TERM) straight to
        # the early return instead of retrying the teardown.
        gl.signal_manager.trigger_signal_sync(Signals.AppQuit)

        gl.threads_running = False

        # Stop a pending boot re-enumeration before close_all()
        # below: the stop event wakes a rescan parked in backoff immediately,
        # and the bounded join covers an in-flight enumeration -- so the
        # rescan can't register a fresh controller while the quit path tears
        # the existing ones down (same residual window as a hotplug event
        # arriving mid-quit, which the USB monitor has always had).
        gl.deck_manager.stop_boot_rescan()

        # Write out every page edit still sitting on its debounce timer.
        # Those timers fire on daemon threads, and this process ends in
        # os._exit -- so without this call quitting within a second of the
        # last edit loses it. Same placement rationale as the store cache
        # flush below, and the same trade: both are atomic_write_json pairs
        # of fsyncs with no timeout of their own, so a wedged filesystem
        # costs the 6s watchdog above rather than hanging the quit.
        try:
            page_flush.get().flush_all()
        except Exception as e:
            log.warning(f"Could not write pending page edits during shutdown: {e}")

        # Drain the store cache's deferred index writes. This
        # process ends in os._exit(0), which skips StoreCache's atexit hook,
        # and the flush timer is a daemon -- without this call the last
        # browse's last-use clock renewals are lost on every quit. Guarded:
        # main.create_global_objects() constructs gl.store_backend on every
        # boot, but quit also has to survive a partially-built process (a
        # startup that aborted before that point), and a failure here must
        # never abort teardown.
        # Deliberately placed AFTER the watchdog above rather than earlier in
        # the teardown: the flush is an atomic_write_json, i.e. two fsyncs
        # with no timeout of their own, so on a wedged filesystem it can block
        # indefinitely. Behind the watchdog that costs 6s and a force_quit;
        # ahead of it the quit would hang with nothing armed to end it.
        try:
            if gl.store_backend is not None:
                gl.store_backend.store_cache.flush_index()
        except Exception as e:
            log.warning(f"Could not flush the store cache index during shutdown: {e}")

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
        # quit.
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

        GTK 4.22 segfaults disposing a window that was never realized
        (reproduced down to a bare Gtk/Adw.ApplicationWindow: destroy(),
        remove_window() and set_application(None) all abort on the unrealized
        dispose path; only close() and leaving it alone are safe). In
        background mode (-b -- the autostart path) on_activate builds
        main_win but skips present(), so the window is never realized and
        this statement killed the process mid-teardown, before
        terminate_all_backends() below: every plugin backend was orphaned on
        every quit, which is the symptom this teardown exists to prevent.

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
                      "abort")
            return
        try:
            main_win.destroy()
        except Exception as e:
            # main_win is published by MainWindow.__init__'s first statement,
            # before the build can still fail (see on_activate), so this can
            # be a half-built window. Note this cannot catch the unrealized-
            # dispose abort itself -- that is native, not a Python exception.
            log.warning(f"Failed to destroy the main window during shutdown: {e}")

    def force_quit(self):
        log.info("Forcing quit...")
        # Last chance to reap the plugin backends: they are
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
        precisely the failure this handler exists to prevent. SOURCE_CONTINUE
        keeps the source armed for as long as the process lives.

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

    def _on_sigint(self, signum, frame):
        """SIGINT (Ctrl+C) entry point. Queues the teardown; escalates if a
        queued one is still sitting there SIGINT_ESCALATE_AFTER_S later.

        A Python-level handler runs between bytecodes on the main thread, so
        it can interrupt any statement in the app -- mid-render, inside a
        GTK callback, with a lock held. Running the teardown from there means
        destroying the window and driving plugin quit hooks on top of a stack
        frame that was doing something else entirely. Handing on_quit to the
        main loop instead defers it to a normal dispatch, which is where the
        TERM/HUP unix-signal sources already run it -- one teardown context for
        every signal route.

        PRIORITY_DEFAULT to match those sources: the default idle priority
        sits below GTK's frame-clock redraws, so a busy UI could postpone the
        quit the user just asked for. on_quit's plain None return removes the
        idle source after the single run (SOURCE_REMOVE), which is what an idle
        wants -- unlike the unix-signal sources, whose sigaction dies with them
        (see _on_unix_signal).

        Everything dispatched is dispatched by the main loop, so a queued
        Ctrl+C only arrives once that loop iterates: a press landing before
        Gio.Application.run starts is held, not lost, but a press landing on a
        WEDGED loop would never arrive at all. Since TERM and HUP are loop
        sources too, that would leave no signal able to end the process short
        of SIGKILL -- which orphans the plugin backends (own session, so no
        killpg reaches them) and skips the force_quit watchdog, armed only
        inside on_quit. Hence the escalation, and hence its shape: what proves
        a wedge is ELAPSED TIME with the teardown still not started, never the
        press count. A healthy app busy in Python for a moment (on_activate
        loads pages and resizes images on the main thread) answers a press
        late, not never, while a double-tap is two presses ~150ms apart and
        key repeat is three in ~66ms -- escalating on those would os._exit(1)
        a perfectly live app with no AppQuit fan-out and no close_all(),
        leaving a deck open for the next startup to fail on. Past the
        threshold, force_quit is the one thing safe to run from handler
        context: terminate_all_backends() and os._exit(1), no GTK, no teardown
        ordering. Gated on _quit_started too, so presses during a teardown
        that IS running stay no-ops rather than cutting it short.

        SIGINT goes back to SIG_DFL just before that call, as a backstop: both
        this handler and force_quit log, log sinks take locks, and a wedge
        inside one would swallow the escalation itself. From that point a
        further Ctrl+C kills the process outright instead of re-entering the
        same deadlock.
        """
        now = time.monotonic()
        if self._sigint_first_at is None:
            self._sigint_first_at = now
        elif (not self._quit_started
                and now - self._sigint_first_at >= SIGINT_ESCALATE_AFTER_S):
            log.warning(
                f"Interrupt requested {now - self._sigint_first_at:.1f}s ago and "
                f"the teardown never started (the main loop is not dispatching); "
                f"forcing quit"
            )
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            self.force_quit()
            return
        GLib.idle_add(self.on_quit, priority=GLib.PRIORITY_DEFAULT)

    def register_signal_handlers(self):
        # SIGINT stays a Python-level handler: PyGObject's wakeup-fd bridge
        # makes it fire promptly under the GLib loop, and having a custom
        # handler installed keeps Gio.Application.run's register_sigint_fallback
        # inert -- that fallback checks signal.getsignal(SIGINT), cannot see a
        # GLib unix-signal source, and would install its own handler routing
        # Ctrl+C to app.quit(), bypassing on_quit's whole teardown. The handler
        # body only queues onto the main loop, so the teardown itself runs in a
        # normal dispatch rather than between two arbitrary bytecodes -- see
        # _on_sigint.
        signal.signal(signal.SIGINT, self._on_sigint)
        # SIGTERM/SIGHUP: GLib-native sources dispatched on the
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

        # update_everything returns Ok(count) or an Err -- don't toast success
        # on failure.
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
        # install_plugin answers a StoreResult -- an Err is a failure, anything
        # else is the single Ok success. Narrowing, not truthiness.
        if isinstance(result, Err):
            self.send_notification("dialog-information-symbolic", "Failed to install plugin",
                                   f"The plugin {plugin_id} could not be installed")
        else:
            self.send_notification("dialog-information-symbolic", "Plugin installed",
                                   f"The plugin {plugin_id} was successfully installed")

        self.set_working(False)            

    def set_working(self, working: bool) -> None:
        # self, not gl.app: this is an App method, so the application object is
        # structurally present -- gl.app is this same instance.
        if working:
            GLib.idle_add(self.mark_busy)
            GLib.idle_add(self.main_win.set_cursor_from_name, "wait")
        else:
            GLib.idle_add(self.unmark_busy)
            GLib.idle_add(self.main_win.set_cursor_from_name, "default")

    def send_notification(self,  # type: ignore[override]  # deliberate: shadows Gio.Application.send_notification with the app's own (icon, title, body) form; the base signature is still reached via the `parent_send` binding below
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
