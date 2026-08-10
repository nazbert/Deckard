"""
Author: Core447
Year: 2024

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import re
import threading
from loguru import logger as log

import globals as gl

from src.backend.main_loop import run_in_background
from src.backend.session_info import desktop_components, session_type
from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.Integration import Integration
from src.backend.WindowGrabber.Integrations.Hyprland import Hyprland
from src.backend.WindowGrabber.Integrations.Gnome import Gnome
from src.backend.WindowGrabber.Integrations.Sway import Sway
from src.backend.WindowGrabber.Integrations.X11 import X11
from src.backend.WindowGrabber.Integrations.KDE import KDE
from src.api import notify_foreground_window_changed


def select_integration_class(environment_components: list[str], server: str | None) -> type[Integration] | None:
    """The integration that can grab windows in this session, or None when
    none can.

    `environment_components` are the XDG_CURRENT_DESKTOP components, matched
    one by one: the variable is a colon-separated list ("ubuntu:GNOME",
    "sway:wlroots:swayfx"), so comparing it as a single string leaves stock
    distro sessions with no integration and automatic page switching dead.

    The order is load-bearing. The X11 session check sits above the KDE
    component, so a KDE session on Xorg reads windows through xprop rather
    than kdotool; it sits below the Wayland-only compositors, which have no
    X11 fallback to be demoted to.
    """
    if "hyprland" in environment_components:
        return Hyprland
    if "gnome" in environment_components:
        return Gnome
    # Sway forks name themselves in the same component ("swayfx") and speak
    # the same IPC.
    if any("sway" in component for component in environment_components):
        return Sway
    if server == "x11":
        return X11
    if "kde" in environment_components:
        return KDE
    return None


class WindowGrabber:
    """Routes active-window changes onto the pages that ask for them.

    Watching the active window is never free -- the X11 and KDE integrations
    poll their helper binary five subprocesses at a time every 200 ms, the
    Sway one shells out to swaymsg just as often -- and it is useful only
    while some page carries an enabled window auto-change rule. So the
    watcher is gated on exactly that: it starts when the first rule appears
    and stops when the last one goes, so a session that never uses the
    feature runs no window polling at all.

    The integration itself is built lazily rather than at construction,
    because it is not free either (a helper-binary probe, or a D-Bus proxy),
    and because a one-shot window query -- the page editor's matching-window
    list, which the user reaches *before* the first rule exists -- must still
    work while the watcher is off.

    Applying the gate is therefore blocking work (subprocess probes, a
    synchronous D-Bus proxy build, joining a watcher), and its callers
    include the GTK main thread. So every gate pass runs on the background
    pool, serialized: `refresh_watch_state` only records that a re-check is
    due, and one worker drains those requests one at a time. State is
    eventually consistent by design -- a request that arrives mid-pass is
    honoured by a further pass that re-reads the rules, so the settled state
    always reflects the last write.
    """

    def __init__(self) -> None:
        self.environment_components: list[str] = desktop_components()
        self.server: str | None = session_type()

        self._integration_class = select_integration_class(self.environment_components, self.server)
        if self._integration_class is None:
            log.error(f"Unsupported environment: {self.environment_components} with server: {self.server} for window grabber.")
        else:
            log.info(f"Window grabber environment: {self.environment_components} under server: {self.server}")

        # Two locks, deliberately. `_transition_lock` serializes whole gate
        # transitions -- read the rules, then start or stop -- so no two
        # transitions can interleave and leave the watcher disagreeing with
        # the rules on disk. `_lock` guards only the fields, and is never
        # held across the blocking part of a transition, so a reader
        # (is_watching, a one-shot window query) never waits behind a
        # watcher join. Order is always _transition_lock -> _lock; nothing
        # takes them the other way round.
        self._transition_lock = threading.RLock()
        self._lock = threading.RLock()
        self.integration: Integration | None = None
        self._watching = False

        # Set whenever no gate pass is queued or running: what a caller
        # waits on to observe a settled decision. Nothing in the app needs
        # to -- the gate converges on its own -- but a test asserting that
        # *nothing* started has no other way to know the pass is done.
        self._gate_idle = threading.Event()
        self._gate_idle.set()
        self._gate_pending = False
        self._gate_running = False
        self._reset_requested = False

        self.refresh_watch_state()

    def _ensure_integration(self) -> Integration | None:
        """The integration for this session, built on first use.

        Construction failures are contained: an integration that cannot be
        built leaves window grabbing inert rather than aborting whatever
        asked for it (a gate pass, or a window query).
        """
        with self._lock:
            if self.integration is None and self._integration_class is not None:
                try:
                    self.integration = self._integration_class(self)
                except Exception:
                    log.opt(exception=True).error("Could not initialize the window grabber integration")
                    return None
            return self.integration

    @property
    def is_watching(self) -> bool:
        with self._lock:
            return self._watching

    def start_watching(self) -> None:
        """Begins watching the active window. Idempotent.

        Blocking: builds the integration if needed and starts its watcher.
        Call it off the GTK main thread -- refresh_watch_state() is the
        route that guarantees that.
        """
        with self._transition_lock:
            with self._lock:
                if self._watching:
                    return
            integration = self._ensure_integration()
            if integration is None:
                return
            integration.start_watching()
            with self._lock:
                self._watching = True
            log.info("Watching the active window: a page has a window auto-change rule")

    def stop_watching(self) -> None:
        """Stops watching and reaps the watcher. Idempotent.

        Blocking: the reap joins the watcher (bounded by
        WATCHER_STOP_TIMEOUT_S). Only `_transition_lock` is held across it,
        never `_lock`, so readers are not stalled by the join.
        """
        with self._transition_lock:
            with self._lock:
                if not self._watching:
                    return
                self._watching = False
                integration = self.integration

            if integration is not None:
                integration.stop_watching()
            log.info("Stopped watching the active window: no page has a window auto-change rule")

            # After the reap rather than before, so a watcher still mid-
            # switch does not load a page on top of the restore below. That
            # holds as long as the join succeeded; a watcher abandoned at
            # the timeout while inside a page load can still land after the
            # restore and re-strand that deck. One-shot and rare -- nothing
            # feeds it further window changes once it unwinds.
            self._restore_auto_loaded_decks()

    def _restore_auto_loaded_decks(self) -> None:
        """Undoes the last automatic switch on every deck still showing one.

        Runs when the gate goes off. A deck that was switched automatically
        normally returns to its manually chosen page on the next window
        change that matches no rule -- but once the last rule is gone there
        are no more window changes to come, so without this pass the deck
        stays stranded on the auto-switched page until the user intervenes.
        """
        deck_manager = gl.deck_manager
        if deck_manager is None:
            return

        for deck_controller in deck_manager.deck_controller:
            if deck_controller is None or not deck_controller.deck.is_open():
                continue
            try:
                self._restore_manual_page(deck_controller)
            except Exception:
                # Same isolation as the per-deck routing catch: one deck
                # torn down mid-restore must not strand the others.
                log.opt(exception=True).warning(
                    "Could not restore the manually loaded page for one deck; continuing with the others"
                )

    def reset_integration(self) -> None:
        """Discards the integration so the next use builds a fresh one
        against the session as it stands now.

        The live case is the GNOME shell extension being installed
        mid-session: the existing integration's D-Bus proxy was built while
        nothing owned that interface, and it cannot start reporting on its
        own. Re-gating afterwards keeps the rules the single source of truth
        for whether anything is watching.

        Returns at once, like refresh_watch_state and for the same reason:
        discarding an integration means stopping and joining its watcher,
        and the caller here is a button handler on the GTK main thread.
        """
        with self._lock:
            self._reset_requested = True

        self.refresh_watch_state()

    def refresh_watch_state(self) -> None:
        """Queues a re-check of the watcher against the rules on disk.

        Returns at once: the pass itself blocks (see start_watching), and
        the callers are page writes, which happen on the GTK main thread and
        on plugin threads alike. Requests coalesce -- a re-check queued
        while one runs causes exactly one more pass, which re-reads the
        rules, so the settled state always reflects the last write.
        """
        with self._lock:
            self._gate_pending = True
            self._gate_idle.clear()
            if self._gate_running:
                return
            self._gate_running = True

        try:
            run_in_background(self._drain_gate_requests)
        except Exception:
            # Nothing will drain the request, so the "a pass is running"
            # claim above must be taken back: leaving it set would make
            # every later re-gate a silent no-op for the rest of the
            # session, with no way back. Dropping this request instead
            # costs one stale decision until the next page write asks
            # again. Reachable once the background pool is shut down,
            # which is part of quit -- but self-healing beats depending on
            # that staying true.
            with self._lock:
                self._gate_running = False
                self._gate_pending = False
                self._gate_idle.set()
            log.opt(exception=True).warning("Could not schedule a window watcher gate pass")

    def wait_for_gate(self, timeout: float = 10.0) -> bool:
        """Blocks until no gate pass is queued or running; False on timeout.

        Observability for tests that assert on a settled decision -- notably
        that nothing was started, which polling cannot establish. Production
        code never needs it.
        """
        return self._gate_idle.wait(timeout)

    def _drain_gate_requests(self) -> None:
        while True:
            with self._lock:
                if not self._gate_pending:
                    self._gate_running = False
                    self._gate_idle.set()
                    return
                self._gate_pending = False

            try:
                self._apply_watch_state()
            except Exception:
                # The drain loop must survive anything a pass throws, or the
                # running flag would stay set and gating would be dead for
                # the rest of the session.
                log.opt(exception=True).error("A window watcher gate pass failed")

    def _apply_watch_state(self) -> None:
        """One gate pass: read the rules, then match the watcher to them.

        Both steps under `_transition_lock`, so no other transition can slip
        between the question and the answer. A rule written *after* the read
        is not lost either: its own refresh_watch_state() set the pending
        flag, and the drain loop runs another pass.
        """
        with self._transition_lock:
            with self._lock:
                reset_requested = self._reset_requested
                self._reset_requested = False
            if reset_requested:
                # Rebuilds against the session as it is now: stop first, so
                # the discarded integration's watcher is reaped rather than
                # left running with nothing referencing it.
                self.stop_watching()
                with self._lock:
                    self.integration = None

            page_manager = gl.page_manager
            if page_manager is None:
                return

            try:
                wanted = page_manager.any_auto_change_rule_enabled()
            except Exception:
                # Leave the current state alone: guessing either way would
                # resurrect the polling this gating exists to avoid, or
                # silently kill a working auto-change.
                log.opt(exception=True).warning("Could not determine whether any window auto-change rule is enabled")
                return

            if wanted:
                self.start_watching()
            else:
                self.stop_watching()

    @log.catch
    def get_all_windows(self) -> list[Window]:
        """
        returns a list of [wm_class, title] lists

        Blocking: builds the integration on first use and then queries the
        desktop (subprocesses on most of them), so callers on the GTK main
        thread must marshal it off.
        """
        integration = self._ensure_integration()
        if integration is None:
            return []

        return integration.get_all_windows()

    def get_all_matching_windows(self, class_regex: str, title_regex: str) -> list[Window]:
        all_windows = self.get_all_windows()

        matching_windows: list[Window] = []
        for window in all_windows:
            if self.get_is_window_matching(window, class_regex, title_regex):
                matching_windows.append(window)

        return matching_windows

    def get_is_window_matching(self, window: Window, class_regex: str | None, title_regex: str | None) -> bool:
        wm_class = window.wm_class
        title = window.title
        if wm_class is None or title is None or class_regex is None or title_regex is None:
            return False
        try:
            class_match = re.search(class_regex, wm_class, re.IGNORECASE)
            title_match = re.search(title_regex, title, re.IGNORECASE)
        except re.error:
            return False
        return bool(class_match and title_match)

    def on_active_window_changed(self, window: Window) -> None:
        # log.info(f"Active window changed to: {window}")

        # Notify DBus API of the foreground window change
        notify_foreground_window_changed(window.title, window.wm_class)

        if gl.deck_manager is None:
            return

        for deck_controller in gl.deck_manager.deck_controller:
            # A closed/disabled deck must only be skipped -- this used to
            # `return`, aborting auto page switching for every remaining
            # deck as soon as one disabled deck's page regex matched.
            if deck_controller is None or not deck_controller.deck.is_open():
                continue

            try:
                self._apply_auto_change(deck_controller, window)
            except Exception:
                # One deck failing mid-switch (e.g. torn down concurrently:
                # close() flips is_open() after the check above already
                # passed) must not abort auto-switching for the remaining
                # decks -- and since the watcher threads wrap their loops in
                # @log.catch, an exception escaping here would kill
                # auto-switching entirely until restart.
                log.opt(exception=True).warning(
                    "Auto page switch failed for one deck; continuing with the others"
                )

    def _apply_auto_change(self, deck_controller, window: Window) -> None:
        """Applies the auto-change page rules to a single deck for the given
        foreground window. May raise if the deck is torn down mid-call; the
        caller isolates that per deck."""
        page_manager = gl.page_manager
        if page_manager is None:
            return

        if deck_controller.active_page is None:
            # A deck that is still starting up or being hotplugged has no
            # page yet: there is nothing to compare against or restore,
            # so skip it instead of dereferencing active_page.json_path.
            return

        found_page = False
        for page_path in page_manager.get_pages():
            info = page_manager.get_auto_change_settings(page_path)
            wm_regex = info.get("wm-class")
            title_regex = info.get("title")
            enabled = info.get("enable", False)
            decks = info.get("decks", [])
            if not enabled:
                continue

            if self.get_is_window_matching(window, wm_regex, title_regex):
                if deck_controller.serial_number() not in decks:
                    continue

                if deck_controller.active_page.json_path != page_path:
                    log.debug(f"Auto changing page: {page_path} on deck {deck_controller.deck.get_serial_number()}")
                    page = page_manager.get_page(page_path, deck_controller)
                    if not deck_controller.page_auto_loaded:
                        deck_controller.last_manual_loaded_page_path = deck_controller.active_page.json_path
                    deck_controller.load_page(page)
                deck_controller.page_auto_loaded = True
                found_page = True
                break

        if not found_page:
            self._restore_manual_page(deck_controller)

    def _restore_manual_page(self, deck_controller) -> None:
        """Returns one deck to its last manually loaded page, if the page it
        shows got there by an automatic switch and does not ask to stay.

        Shared by the two ways a deck stops being covered by a rule: no rule
        matches the current window, and the last rule going away (the gate
        turning off), which produces no further window changes to carry the
        restore. Same conditions either way, so the two cannot drift apart.
        """
        page_manager = gl.page_manager
        if page_manager is None:
            return

        # A deck mid-startup or mid-hotplug has no page to restore from, and
        # a deck that was never auto-switched has nothing to undo.
        if deck_controller.active_page is None:
            return
        if not getattr(deck_controller, "page_auto_loaded", False):
            return

        active_page_change_info = page_manager.get_auto_change_settings(deck_controller.active_page.json_path)
        if active_page_change_info.get("stay-on-page", True):
            return
        deck_controller.page_auto_loaded = False
        if deck_controller.last_manual_loaded_page_path is None:
            return
        page = page_manager.get_page(deck_controller.last_manual_loaded_page_path, deck_controller)
        if page is None:
            # The manually chosen page has been deleted since. Nothing to go
            # back to, and loading None would take the deck's page away.
            return
        deck_controller.load_page(page, allow_reload=False)
