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
# still gets every frame; only the mirror is rate-limited, because one strip
# frame repaints a preview as wide as the whole deck.
TOUCHSCREEN_UI_INTERVAL_S = 0.1
# Key previews paint as fast as the main loop drains them; the slot below is
# what keeps that bounded.
KEY_UI_INTERVAL_S = 0.0


def mark_dirty(controller, identifier) -> None:
    """Late-failure channel for an ACCEPTED-then-dropped frame.

    `push_input_image` answers True as soon as a frame is in the input's
    mirror slot -- but the window can unmap (or a rebuild orphan the widget)
    before that paint runs, and by then there is no engine call left to
    return False through. Every such drop routes here instead, so
    `load_from_changes` still has something to replay on remap.

    The markers dict stays engine-side on purpose: it is what a future
    detached UI client would ask the engine to recomposite.
    """
    try:
        controller.ui_image_changes_while_hidden[identifier] = True
    except Exception:
        # A controller torn down mid-flight has nothing left to replay to.
        log.opt(exception=True).debug("Could not record a dropped preview frame")


class _MirrorSlot:
    """Latest-wins hand-off of ONE input's preview frames to the main loop.

    A producer leaves a paint-ready payload here and arms at most ONE
    main-loop callback; that callback paints whatever is in the slot when it
    runs. A backlogged loop therefore costs one queued callback and one
    retained payload per input instead of one of each per frame -- and the
    frame that lands is always the newest one, so nothing is gained by
    painting the ones it superseded.

    `interval` is a floor on how often the slot drains, for previews whose
    repaint is worth rate-limiting on its own. 0 means "as fast as the loop
    drains". A frame held by the interval is still flushed by its own
    delayed callback, so the last frame of a burst (a scroll that stops)
    lands instead of being held forever.

    Thread-safe: `offer` runs on producers (the media thread), `take` on the
    main loop.
    """

    __slots__ = ("_interval", "_lock", "_pending", "_armed", "_last_drain")

    def __init__(self, interval: float = 0.0) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._pending: object | None = None
        self._armed: bool = False
        # Deliberately infinitely far in the past: the first frame paints
        # immediately, however long the process has been up.
        self._last_drain: float = float("-inf")

    def offer(self, payload: object) -> float | None:
        """Producer side, any thread. Makes `payload` the frame to paint,
        replacing one that has not been painted yet.

        Returns the seconds to wait before draining, or None when a drain is
        already armed -- that callback picks this payload up instead of the
        one it was armed for, which is what keeps the callback count at one.
        """
        with self._lock:
            self._pending = payload
            if self._armed:
                return None
            self._armed = True
            return max(0.0, self._interval - (time.monotonic() - self._last_drain))

    def take(self) -> object | None:
        """Main loop. The frame to paint, disarming the slot. None when the
        slot is empty -- a producer that armed a second callback in the gap
        between this call and the paint finds nothing left to do."""
        with self._lock:
            payload, self._pending = self._pending, None
            self._armed = False
            if payload is not None:
                self._last_drain = time.monotonic()
            return payload

    def disarm(self) -> None:
        """Undo an `offer` whose callback never got scheduled. The payload
        stays: without this the slot looks armed forever and the input's
        preview freezes with a frame pinned behind it."""
        with self._lock:
            self._armed = False


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
        # (controller, identifier) -> _MirrorSlot. One slot per input, so a
        # stalled main loop holds one frame per input, not a queue per input.
        self._mirror_slots: dict = {}
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
        self._mirror_slots.clear()
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
        # Snapshot, then delete TOLERANTLY: this runs off the main loop (deck
        # removal arrives on the USB monitor, boot rescan and flatpak poll
        # threads) and is not the only writer. The media thread creates a slot
        # the first time it mirrors an input, so scanning the live dict can
        # see it change size; and every armed drain for this controller --
        # precisely what is pending at unplug -- pops its own slot from the
        # GTK loop as soon as the child is gone, so an entry in the snapshot
        # can already be deleted by the time we get to it. Either one raising
        # here would come out of on_deck_removed, skipping the controller's
        # close() and stranding its media thread and USB handle.
        for key in [k for k in list(self._mirror_slots) if k[0] is controller]:
            self._mirror_slots.pop(key, None)

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

    def _mirror_widget(self, child, identifier):
        """The widget that mirrors `identifier`, or None when this input has
        no preview widget (yet).

        Raises through a grid rebuild -- buttons[x][y] can be short of these
        coords mid-rebuild -- so every caller contains that.
        """
        if isinstance(identifier, Input.Key):
            grid = self._grid(child)
            if grid is None:
                return None
            x, y = identifier.coords
            return grid.buttons[x][y]
        if isinstance(identifier, Input.Touchscreen):
            screenbar = self._screenbar(child)
            return None if screenbar is None else screenbar.image
        return None

    # --------------------------------------------------------- render mirror

    def push_input_image(self, controller, identifier, image) -> bool:
        try:
            if image is None or not self._window_mapped:
                return False
            child = self._children.get(controller)
            if child is None:
                return False
            widget = self._mirror_widget(child, identifier)
            if widget is None:
                return False

            # Convert on THIS thread -- the media thread for live frames.
            # prepare_mirror_frame is pure PIL + GdkPixbuf, so the main loop
            # only ever gets handed the finished payload.
            payload = widget.prepare_mirror_frame(image)

            key = (controller, identifier)
            slot = self._mirror_slots.get(key)
            if slot is None:
                interval = (TOUCHSCREEN_UI_INTERVAL_S
                            if isinstance(identifier, Input.Touchscreen)
                            else KEY_UI_INTERVAL_S)
                slot = self._mirror_slots.setdefault(key, _MirrorSlot(interval))

            delay_s = slot.offer(payload)
            if delay_s is None:
                # A drain is already armed and now carries this frame.
                return True
            try:
                # Idle priority on BOTH arms: pixbuf updates that outrank
                # GTK's layout/draw (priority 120) starve the very redraw
                # they are feeding. timeout_add defaults to priority 0, so
                # the delayed arm has to say so explicitly.
                if delay_s <= 0:
                    GLib.idle_add(self._drain_mirror, controller, identifier)
                else:
                    GLib.timeout_add(int(delay_s * 1000) + 1, self._drain_mirror,
                                     controller, identifier,
                                     priority=GLib.PRIORITY_DEFAULT_IDLE)
            except BaseException:
                # Nothing will drain this slot now; leaving it armed would
                # freeze the input for good.
                slot.disarm()
                raise
            return True
        except Exception:
            # Open failure set: widget lookups race window teardown, and
            # prepare_mirror_frame runs PIL convert/tobytes plus
            # GdkPixbuf.new_from_bytes. Contain all of it -- the mirror is
            # best-effort and we run under the media tick, whose catch-all
            # backs off 0.25s per exception. A failing preview must never
            # throttle the deck writer loop.
            log.opt(exception=True).warning(f"Failed to mirror {identifier} into the UI")
            return False

    def _drain_mirror(self, controller, identifier) -> bool:
        # Main loop: paint the newest frame this input has produced. Returns
        # False -- a GLib idle/timeout callback that returns truthy re-arms
        # forever.
        slot = self._mirror_slots.get((controller, identifier))
        if slot is None:
            return False
        payload = slot.take()
        if payload is None:
            return False
        try:
            # Resolved again here rather than captured at push time: the
            # binding is by deck-stack-child identity, and a grid rebuilt in
            # the gap would otherwise be handed a frame for its orphaned
            # predecessor.
            child = self._children.get(controller)
            if child is None:
                # Unbound between the push and this paint: drop the slot too.
                # A push that raced unbind() re-creates one, and leaving it
                # keyed by a dead controller pins that controller's whole
                # graph -- one leak per replug.
                self._mirror_slots.pop((controller, identifier), None)
            widget = None if child is None else self._mirror_widget(child, identifier)
            if not self._window_mapped or widget is None:
                # Accepted then dropped: push_input_image already answered
                # True for this frame, so nothing else will record it.
                mark_dirty(controller, identifier)
                return False
            widget.paint_mirror_frame(payload)
        except Exception:
            log.opt(exception=True).warning(f"Failed to paint the {identifier} mirror")
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
            return self._mirror_widget(child, identifier)
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
