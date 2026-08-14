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

    environment_components holds the XDG_CURRENT_DESKTOP components, and this
    matches them one by one. The variable is a colon-separated list
    ("ubuntu:GNOME", "sway:wlroots:swayfx"), so a comparison against the whole
    string leaves a stock distro session with no integration, and automatic
    page switching then does nothing.

    The order of the checks matters. The X11 session check sits above the KDE
    component, so a KDE session on Xorg reads windows through xprop and not
    through kdotool. It sits below the Wayland-only compositors, which have no
    X11 fallback.
    """
    if "hyprland" in environment_components:
        return Hyprland
    if "gnome" in environment_components:
        return Gnome
    # A Sway fork names itself in the same component ("swayfx") and speaks
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

    A watch on the active window costs real work. The X11 and KDE
    integrations poll their helper binary five subprocesses at a time every
    200 ms, and the Sway one runs swaymsg as often. The watch helps only while
    some page carries an enabled window auto-change rule, so a rule gates it.
    The watcher starts with the first rule and stops with the last one, and a
    session that never uses the feature polls no windows at all.

    The integration builds on first use, not in the constructor, because it
    costs a helper-binary probe or a D-Bus proxy. A one-shot window query must
    also work while the watcher is off, because the user reaches the page
    editor's matching-window list before the first rule exists.

    A gate pass therefore blocks on subprocess probes, on a synchronous D-Bus
    proxy build, and on a watcher join, and the GTK main thread is one of its
    callers. Every gate pass runs on the background pool, one at a time.
    refresh_watch_state only records that a re-check is due, and one worker
    drains those requests. The state settles rather than updates at once. A
    request that arrives mid-pass gets a further pass, which re-reads the
    rules, so the settled state always matches the last write.
    """

    def __init__(self) -> None:
        self.environment_components: list[str] = desktop_components()
        self.server: str | None = session_type()

        self._integration_class = select_integration_class(self.environment_components, self.server)
        if self._integration_class is None:
            log.error(f"Unsupported environment: {self.environment_components} with server: {self.server} for window grabber.")
        else:
            log.info(f"Window grabber environment: {self.environment_components} under server: {self.server}")

        # Two locks. _transition_lock serializes a whole gate transition,
        # which reads the rules and then starts or stops, so two transitions
        # cannot interleave and leave the watcher out of step with the rules
        # on disk. _lock guards the fields only, and no holder keeps it across
        # the blocking part of a transition, so a reader (is_watching, a
        # one-shot window query) never waits behind a watcher join. Every
        # holder takes _transition_lock before _lock, and never the reverse.
        self._transition_lock = threading.RLock()
        self._lock = threading.RLock()
        self.integration: Integration | None = None
        self._watching = False

        # Set while no gate pass is queued or running. A caller waits on it to
        # see a settled decision. The app never waits, because the gate
        # settles on its own. A test that asserts that nothing started has no
        # other way to know the pass ended.
        self._gate_idle = threading.Event()
        self._gate_idle.set()
        self._gate_pending = False
        self._gate_running = False
        self._reset_requested = False

        self.refresh_watch_state()

    def _ensure_integration(self) -> Integration | None:
        """The integration for this session, built on first use.

        This contains a construction failure. An integration that fails to
        build leaves window grabbing inert, and does not abort the caller,
        which is a gate pass or a window query.
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

        This blocks. It builds the integration when needed and starts its
        watcher. Call it off the GTK main thread; refresh_watch_state() is the
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

        This blocks. The reap joins the watcher within
        WATCHER_STOP_TIMEOUT_S. It holds _transition_lock across the join and
        never _lock, so the join stalls no reader.
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

            # Restore after the reap, so a watcher still mid-switch cannot
            # load a page over the restore. That holds while the join
            # succeeds. A watcher abandoned at the timeout inside a page load
            # can still land after the restore and strand that deck again.
            # This happens once and stays rare, because nothing feeds the
            # abandoned watcher further window changes.
            self._restore_auto_loaded_decks()

    def _restore_auto_loaded_decks(self) -> None:
        """Undoes the last automatic switch on every deck still showing one.

        Runs when the gate goes off. An automatically switched deck normally
        returns to its manually chosen page on the next window change that
        matches no rule. Once the last rule goes, no window change follows,
        and without this pass the deck stays on the auto-switched page until
        the user acts.
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
                # The same isolation as the per-deck routing catch. One deck
                # torn down mid-restore must not strand the others.
                log.opt(exception=True).warning(
                    "Could not restore the manually loaded page for one deck; continuing with the others"
                )

    def reset_integration(self) -> None:
        """Discards the integration so the next use builds a fresh one
        against the session as it stands now.

        The live case is an install of the GNOME shell extension mid-session.
        The existing integration built its D-Bus proxy while nothing owned
        that interface, and that proxy cannot start to report on its own. The
        gate pass afterwards leaves the rules to decide what watches.

        Returns at once, like refresh_watch_state and for the same reason. A
        discard stops and joins the watcher, and the caller here is a button
        handler on the GTK main thread.
        """
        with self._lock:
            self._reset_requested = True

        self.refresh_watch_state()

    def refresh_watch_state(self) -> None:
        """Queues a re-check of the watcher against the rules on disk.

        Returns at once. The pass itself blocks (see start_watching), and the
        callers are page writes, which run on the GTK main thread and on
        plugin threads. Requests coalesce. A re-check queued while one runs
        causes exactly one more pass, which re-reads the rules, so the settled
        state always matches the last write.
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
            # Nothing drains the request now, so take back the "a pass is
            # running" claim above. A flag left set makes every later re-gate
            # do nothing for the rest of the session. A dropped request costs
            # one stale decision until the next page write asks again. This
            # branch runs once the background pool shuts down, which is part
            # of quit, and it heals itself rather than trust that.
            with self._lock:
                self._gate_running = False
                self._gate_pending = False
                self._gate_idle.set()
            log.opt(exception=True).warning("Could not schedule a window watcher gate pass")

    def wait_for_gate(self, timeout: float = 10.0) -> bool:
        """Blocks until no gate pass is queued or running. False on timeout.

        This exists for a test that asserts on a settled decision, above all
        that nothing started, which a poll cannot establish. Production code
        never needs it.
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
                # The drain loop must survive anything a pass throws. The
                # running flag otherwise stays set and kills the gate for the
                # rest of the session.
                log.opt(exception=True).error("A window watcher gate pass failed")

    def _apply_watch_state(self) -> None:
        """One gate pass. Read the rules, then match the watcher to them.

        Both steps run under _transition_lock, so no other transition slips
        between the question and the answer. A rule written after the read
        survives too. Its own refresh_watch_state() sets the pending flag, and
        the drain loop runs another pass.
        """
        with self._transition_lock:
            with self._lock:
                reset_requested = self._reset_requested
                self._reset_requested = False
            if reset_requested:
                # Rebuild against the session as it is now. Stop first, so the
                # reap takes the discarded integration's watcher and leaves no
                # thread that nothing references.
                self.stop_watching()
                with self._lock:
                    self.integration = None

            page_manager = gl.page_manager
            if page_manager is None:
                return

            try:
                wanted = page_manager.any_auto_change_rule_enabled()
            except Exception:
                # Leave the current state alone. A guess either way restarts
                # the polling that this gate removes, or kills a working
                # auto-change.
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

        This blocks. It builds the integration on first use and then queries
        the desktop, which runs subprocesses on most of them, so a caller on
        the GTK main thread must marshal it off.
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

        # Tell the D-Bus API about the foreground window change.
        notify_foreground_window_changed(window.title, window.wm_class)

        if gl.deck_manager is None:
            return

        for deck_controller in gl.deck_manager.deck_controller:
            # Skip a closed or disabled deck. A return here would abort auto
            # page switching for every remaining deck as soon as one disabled
            # deck's page regex matched.
            if deck_controller is None or not deck_controller.deck.is_open():
                continue

            try:
                self._apply_auto_change(deck_controller, window)
            except Exception:
                # One deck can fail mid-switch, when a concurrent close()
                # flips is_open() after the check above passed. That must not
                # abort auto-switching for the remaining decks. The watcher
                # threads wrap their loops in @log.catch, so an exception that
                # escapes here kills auto-switching until the next app start.
                log.opt(exception=True).warning(
                    "Auto page switch failed for one deck; continuing with the others"
                )

    def _apply_auto_change(self, deck_controller, window: Window) -> None:
        """Applies the auto-change page rules to a single deck for the given
        foreground window. It can raise when the deck is torn down mid-call,
        and the caller isolates that per deck."""
        page_manager = gl.page_manager
        if page_manager is None:
            return

        if deck_controller.active_page is None:
            # A deck still starting up, or mid-hotplug, has no page yet.
            # Nothing exists to compare or restore, so skip the deck instead
            # of reading active_page.json_path.
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

        Two paths reach this. No rule matches the current window, or the last
        rule goes away and turns the gate off, which leaves no further window
        change to carry the restore. Both paths apply the same conditions, so
        the two cannot drift apart.
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
            # The user deleted the manually chosen page. Nothing remains to go
            # back to, and a load of None takes the deck's page away.
            return
        deck_controller.load_page(page, allow_reload=False)
