"""
The GTK side of the engine->UI port: `GtkUIAdapter` implements
`src.backend.ui_port.UIPort` against the real widget tree.

Everything the engine used to do itself -- resolving a controller's
`DeckStackChild`, reading `main_win.get_mapped()` from the media thread,
scanning the deck stack's ListModel off-main, poking sidebar editors -- lives
here, on the GTK side of the seam.

Threading: every method is callable from any thread and must return without
blocking on the main loop. Widget mutations are marshalled with
`GLib.idle_add`; `run_on_main` is banned here (a wedged main loop must never
stall the media writer). The one documented exception to idle-everything is
`on_deck_layout_changed` -- see its docstring.

This module deliberately imports nothing from `src.windows.*` at module scope:
`KeyGrid` imports it (for `mark_dirty`), so the only widget import here is a
function-local one in the rotation path.
"""
import threading
import time

from gi.repository import GLib
from loguru import logger as log

import globals as gl
from src.backend import ui_port
from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.backend.DeckManagement.InputIdentifier import Input

# Seconds between on-screen touchscreen previews. The physical touchscreen
# still gets every frame; only the mirror is throttled.
TOUCHSCREEN_UI_INTERVAL_S = 0.1
TOUCHSCREEN_UI_FLUSH_MS = 100


def mark_dirty(controller, identifier) -> None:
    """Late-failure channel for an ACCEPTED-then-dropped frame.

    `push_input_image` answers True as soon as a frame is handed to the
    throttle or to a widget's idle -- but the window can unmap (or a rebuild
    orphan the widget) before that paint runs, and by then there is no engine
    call left to return False through. Every such drop routes here instead, so
    `load_from_changes` still has something to replay on remap.

    The markers dict stays engine-side on purpose: it is what a future
    detached UI client would ask the engine to recomposite.
    """
    try:
        controller.ui_image_changes_while_hidden[identifier] = True
    except Exception:
        # A controller torn down mid-flight has nothing left to replay to.
        log.opt(exception=True).debug("Could not record a dropped preview frame")


class _TouchscreenThrottle:
    __slots__ = ("last_push", "pending", "flush_scheduled")

    def __init__(self):
        self.last_push: float = 0.0
        self.pending = None
        self.flush_scheduled: bool = False


class GtkUIAdapter(ui_port.UIPort):
    def __init__(self):
        # controller -> DeckStackChild, maintained by DeckStack.add_page /
        # remove_page. Binding is by OBJECT IDENTITY at add time:
        # no serial/name matching anywhere, and no ListModel scan from the
        # media thread.
        self._children: dict = {}
        self._window = None
        # Plain bool written by the window's map/unmap handlers and read
        # lock-free from the media thread. This replaces the off-main
        # `main_win.get_mapped()` widget reads the engine used to do.
        self._window_mapped: bool = False
        self._ts_lock = threading.Lock()
        self._ts_state: dict = {}
        # controller -> bool; the page-sync coalescer.
        self._page_sync_queued: dict = {}

    # ---------------------------------------------------------------- setup

    def attach_window(self, window) -> None:
        """Bind to a built MainWindow: track its mapped state and (re-)scan
        the deck stack so any child added during construction is bound.

        Called AFTER the constructor because the map/unmap handlers need a
        real window -- but the adapter itself is installed BEFORE it, since
        every boot-time `add_page` runs inside `MainWindow.build()`.
        """
        self._window = window
        try:
            self._window_mapped = bool(window.get_mapped())
            window.connect("map", self._on_window_map)
            window.connect("unmap", self._on_window_unmap)
        except Exception:
            log.opt(exception=True).warning("Could not track the main window's mapped state")
        self.rescan_children()
        self.reconcile_children()

    def reconcile_children(self) -> None:
        """Heal decks the window constructor could not see.

        `on_deck_added`/`on_deck_removed` are no-ops while `_window` is None
        -- which is exactly the window in which `MainWindow.__init__` runs
        (the adapter is installed BEFORE the constructor so boot-time
        `add_page` calls still bind, but `attach_window` only lands after
        it). A deck the USB monitor plugged in during that window would
        therefore never get a stack child, and one unplugged during it would
        leave a stale one. `rescan_children` only re-binds children that
        already exist, so reconcile both directions against the deck
        manager's live list here.
        """
        window = self._window
        if not recursive_hasattr(window, "leftArea.deck_stack"):
            return
        deck_stack = window.leftArea.deck_stack
        registered = getattr(getattr(gl, "deck_manager", None), "deck_controller", None)
        if registered is None:
            # No deck manager to reconcile against. Bail rather than treat
            # that as "no decks exist" -- the removal pass below would then
            # tear down every bound child. (main.py builds gl.deck_manager
            # before App, so this is belt-and-braces, not an expected state.)
            return
        live = list(registered)
        for controller in live:
            if controller not in self._children:
                GLib.idle_add(deck_stack.add_page, controller)
        for controller in [c for c in self._children if c not in live]:
            GLib.idle_add(deck_stack.remove_page, controller)
            self.unbind(controller)

    def detach_window(self) -> None:
        self._window = None
        self._window_mapped = False
        self._children.clear()
        self._ts_state.clear()
        self._page_sync_queued.clear()

    def rescan_children(self) -> None:
        """(Re-)bind every controller whose DeckStackChild is already in the
        stack. Makes binding independent of adapter-install ordering and heals
        a rebuilt window."""
        window = self._window
        if not recursive_hasattr(window, "leftArea.deck_stack"):
            return
        for page in window.leftArea.deck_stack.get_pages():
            if page is None:
                # ListModel iteration snapshots len once; trailing removed
                # indices yield None. Only trailing entries can be None.
                break
            child = page.get_child()
            controller = getattr(child, "deck_controller", None)
            if controller is not None:
                self._children[controller] = child

    def bind(self, controller, child) -> None:
        self._children[controller] = child

    def unbind(self, controller) -> None:
        self._children.pop(controller, None)
        self._page_sync_queued.pop(controller, None)
        with self._ts_lock:
            for key in [k for k in self._ts_state if k[0] is controller]:
                del self._ts_state[key]

    def _on_window_map(self, *args) -> None:
        self._window_mapped = True

    def _on_window_unmap(self, *args) -> None:
        self._window_mapped = False

    # ------------------------------------------------------------ resolvers

    def _grid(self, child):
        if not recursive_hasattr(child, "page_settings.deck_config.grid"):
            return None
        return child.page_settings.deck_config.grid

    def _screenbar(self, child):
        if not recursive_hasattr(child, "page_settings.deck_config.screenbar.image"):
            return None
        return child.page_settings.deck_config.screenbar

    # --------------------------------------------------------- render mirror

    def push_input_image(self, controller, identifier, image) -> bool:
        try:
            if image is None or not self._window_mapped:
                return False
            child = self._children.get(controller)
            if child is None:
                return False

            if isinstance(identifier, Input.Key):
                grid = self._grid(child)
                if grid is None:
                    return False
                x, y = identifier.coords
                # set_image converts on THIS thread and idles only the paint.
                # The lookup races a grid rebuild and the button grid
                # can be smaller than these coords mid-rebuild -- contained by
                # the except below.
                grid.buttons[x][y].set_image(image)
                return True

            if isinstance(identifier, Input.Touchscreen):
                return self._push_touchscreen(controller, child, identifier, image)

            return False
        except Exception:
            # Open failure set: widget lookups race window teardown, and
            # set_image runs PIL convert/tobytes plus GdkPixbuf.new_from_bytes.
            # Contain all of it -- the mirror is best-effort and we run under
            # the media tick, whose catch-all backs off 0.25s per exception. A
            # failing preview must never throttle the deck writer loop.
            log.opt(exception=True).warning(f"Failed to mirror {identifier} into the UI")
            return False

    def _push_touchscreen(self, controller, child, identifier, image) -> bool:
        screenbar = self._screenbar(child)
        if screenbar is None:
            return False

        key = (controller, identifier)
        state = self._ts_state.get(key)
        if state is None:
            state = self._ts_state.setdefault(key, _TouchscreenThrottle())

        now = time.time()
        with self._ts_lock:
            if now - state.last_push < TOUCHSCREEN_UI_INTERVAL_S:
                # Within the throttle window: keep the latest frame and flush
                # it after the window, so the final frame (when a scroll stops)
                # isn't lost.
                state.pending = image
                if not state.flush_scheduled:
                    state.flush_scheduled = True
                    GLib.timeout_add(TOUCHSCREEN_UI_FLUSH_MS, self._flush_touchscreen,
                                     controller, identifier)
                return True
            state.last_push = now
            state.pending = None

        screenbar.image.set_image(image)
        return True

    def _flush_touchscreen(self, controller, identifier) -> bool:
        # Main loop: push the last throttled frame so the preview doesn't
        # freeze mid-scroll. Skipped if a fresh frame already superseded it.
        state = self._ts_state.get((controller, identifier))
        if state is None:
            return False
        with self._ts_lock:
            state.flush_scheduled = False
            image = state.pending
            state.pending = None
        if image is None:
            return False

        child = self._children.get(controller)
        screenbar = self._screenbar(child) if child is not None else None
        if not self._window_mapped or screenbar is None:
            # Unmapped mid-throttle: the frame we accepted never landed.
            mark_dirty(controller, identifier)
            return False
        try:
            # last_push is read+written by the media thread in
            # _push_touchscreen; every other access is under the lock, so
            # this one must be too.
            with self._ts_lock:
                state.last_push = time.time()
            screenbar.image.set_image(image)
        except Exception:
            log.opt(exception=True).warning("Touchscreen mirror flush failed")
            mark_dirty(controller, identifier)
        return False

    # ------------------------------------------------------------ deck sync

    def on_page_changed(self, controller) -> None:
        # Coalesce page-load completion signals into one pending idle: N rapid
        # page changes (unlock bursts, ChangePage chains) must not queue N
        # full sidebar rebuilds. Each callback renders the LIVE state, so
        # whichever completion lands last wins. The check-then-set race
        # between the two trigger threads is benign -- worst case two idles,
        # both rendering the same current state.
        if self._page_sync_queued.get(controller):
            return
        self._page_sync_queued[controller] = True
        GLib.idle_add(self._run_page_changed, controller)

    def _run_page_changed(self, controller) -> bool:
        # pop, not `= False`: an idle queued before unbind() still runs after
        # it, and re-inserting the key would resurrect a pinned reference to
        # an unplugged controller's whole graph -- one leak per replug.
        self._page_sync_queued.pop(controller, None)
        window = self._window
        if not recursive_hasattr(window, "sidebar"):
            return False
        child = self._children.get(controller)
        if child is None:
            return False
        # The sidebar mirrors the VISIBLE deck's selected input; a page change
        # on a background deck must not reload it.
        if window.leftArea.deck_stack.get_visible_child() is not child:
            return False
        sidebar = window.sidebar
        # Never yank the user out of a sub-view: Sidebar.load_for_* forces
        # main_stack back to the input editor, so refreshing while the
        # ActionChooser/ActionConfigurator (or the error page, whose deferred
        # on_map task handles its own reload) is up would snap a mid-edit user
        # away on every automatic page change.
        if sidebar.main_stack.get_visible_child() is not sidebar.configurator_stack:
            return False
        sidebar.update()
        return False

    _EDITOR_FOR_ASPECT = {
        "labels": "label_editor",
        "layout": "image_editor",
        "background": "background_editor",
    }

    def on_input_visuals_changed(self, controller, identifier, state, aspect) -> None:
        GLib.idle_add(self._run_input_visuals_changed, controller, identifier, state, aspect)

    def _run_input_visuals_changed(self, controller, identifier, state, aspect) -> bool:
        editor_name = self._EDITOR_FOR_ASPECT.get(aspect)
        if editor_name is None:
            log.warning(f"Unknown UI aspect {aspect!r}")
            return False
        sidebar = self._sidebar_for(controller, identifier)
        if sidebar is None:
            return False
        getattr(sidebar.key_editor, editor_name).load_for_identifier(identifier, state)
        return False

    def on_input_states_changed(self, controller, identifier, n_states) -> None:
        GLib.idle_add(self._run_input_states_changed, controller, identifier, n_states)

    def _run_input_states_changed(self, controller, identifier, n_states) -> bool:
        sidebar = self._sidebar_for(controller, identifier, require_active_deck=False)
        if sidebar is None:
            return False
        sidebar.key_editor.state_switcher.set_n_states(n_states)
        return False

    def on_input_state_selected(self, controller, identifier, state) -> None:
        GLib.idle_add(self._run_input_state_selected, controller, identifier, state)

    def _run_input_state_selected(self, controller, identifier, state) -> bool:
        sidebar = self._sidebar_for(controller, identifier)
        if sidebar is None:
            return False
        sidebar.active_state = state
        sidebar.update()
        return False

    def _sidebar_for(self, controller, identifier, require_active_deck: bool = True):
        """The sidebar, but only when it is currently showing `identifier` of
        `controller`. Runs on the main loop -- all the widget reads the engine
        used to do off-thread live here."""
        window = self._window
        if not recursive_hasattr(window, "sidebar.active_identifier"):
            return None
        sidebar = window.sidebar
        if sidebar.active_identifier != identifier:
            return None
        if require_active_deck and window.get_active_controller() is not controller:
            return None
        return sidebar

    def set_low_fps_warning(self, controller, shown) -> None:
        GLib.idle_add(self._run_set_low_fps_warning, controller, shown)

    def _run_set_low_fps_warning(self, controller, shown) -> bool:
        child = self._children.get(controller)
        if child is None or not hasattr(child, "low_fps_banner"):
            return False
        child.low_fps_banner.set_revealed(shown)
        return False

    def on_deck_layout_changed(self, controller) -> None:
        """Rebuild the deck's key grid for a new rotation.

        Runs INLINE when already on the main loop, and set_rotation's only
        caller is main-thread: it reloads the page immediately afterwards, and
        an idled rebuild would let those repaints hit the pre-rotation grid
        (transposed buttons[x][y] -> contained IndexErrors -> dropped frames
        with no marker).
        """
        if threading.current_thread() is threading.main_thread():
            self._run_deck_layout_changed(controller)
            return
        GLib.idle_add(self._run_deck_layout_changed, controller)

    def _run_deck_layout_changed(self, controller) -> bool:
        # Function-local: KeyGrid imports this module for mark_dirty.
        from src.windows.mainWindow.elements.KeyGrid import KeyGrid

        child = self._children.get(controller)
        if child is None:
            return False
        deck_config = child.page_settings.deck_config
        old_grid = deck_config.grid
        deck_config.remove(old_grid)
        deck_config.grid = KeyGrid(controller, old_grid.page_settings_page)
        deck_config.prepend(deck_config.grid)
        return False

    # --------------------------------------------------- deprecated queries

    def query_input_widget(self, controller, identifier):
        child = self._children.get(controller)
        if child is None:
            return None
        try:
            if isinstance(identifier, Input.Key):
                grid = self._grid(child)
                if grid is None:
                    return None
                x, y = identifier.coords
                return grid.buttons[x][y]
            if isinstance(identifier, Input.Touchscreen):
                screenbar = self._screenbar(child)
                return None if screenbar is None else screenbar.image
        except Exception:
            log.opt(exception=True).warning(f"Could not resolve the widget for {identifier}")
        return None

    def query_deck_widget(self, controller, part: str):
        child = self._children.get(controller)
        if child is None:
            return None
        if part == "deck_stack_child":
            return child
        if part == "key_grid":
            return self._grid(child)
        return None

    # ----------------------------------------------------------- app level

    def on_deck_added(self, controller) -> None:
        window = self._window
        if not recursive_hasattr(window, "leftArea.deck_stack"):
            return
        GLib.idle_add(window.leftArea.deck_stack.add_page, controller)

    def on_deck_removed(self, controller) -> None:
        # The detach idle is queued SYNCHRONOUSLY here (plan P1.3): the caller
        # spawns the slow close thread immediately after this returns, and a
        # fast unplug/replug must not race a late detach against a fresh
        # add_page idle and leave two stack children for one serial.
        window = self._window
        if recursive_hasattr(window, "leftArea.deck_stack"):
            GLib.idle_add(window.leftArea.deck_stack.remove_page, controller)
        self.unbind(controller)

    def refresh_deck_availability(self) -> None:
        window = self._window
        if window is None:
            return
        GLib.idle_add(window.check_for_errors)

    def on_page_list_changed(self) -> None:
        window = self._window
        if not recursive_hasattr(window, "sidebar.page_selector"):
            return
        GLib.idle_add(window.sidebar.page_selector.update)

    def notify_plugin_problem(self, plugin_id: str, kind: str) -> None:
        app = getattr(gl, "app", None)
        if app is None:
            return
        # App.send_notification marshals its whole body onto the main loop, so
        # this is callable straight from an action executor thread.
        if kind == "outdated":
            app.send_outdated_plugin_notification(plugin_id)
        elif kind == "missing":
            app.send_missing_plugin_notification(plugin_id)
        else:
            log.warning(f"Unknown plugin problem kind {kind!r}")
