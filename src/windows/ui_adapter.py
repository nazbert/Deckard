"""GTK side of the engine-to-UI port.

GtkUIAdapter implements src.backend.ui_port.UIPort against the real widget
tree, so the engine touches no widget. Every method accepts a call from any
thread and returns without a block on the main loop. Widget changes marshal
with GLib.idle_add. Do not use run_on_main here, because a wedged main loop
must not stall the media writer. on_deck_layout_changed is the one exception,
and its docstring says why.
"""
# This module imports nothing from src.windows at module scope, because
# KeyGrid imports it for mark_dirty. The one widget import is function-local,
# in the rotation path.
import threading
import time

from gi.repository import GLib
from loguru import logger as log

import globals as gl
from src.backend import ui_port
from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.backend.DeckManagement.InputIdentifier import Input

# Seconds between on-screen touchscreen previews. The physical touchscreen
# still gets every frame. Only the mirror takes the limit, because one strip
# frame repaints a preview as wide as the whole deck.
TOUCHSCREEN_UI_INTERVAL_S = 0.1
# Key previews paint as fast as the main loop drains them. The slot bounds it.
KEY_UI_INTERVAL_S = 0.0


def mark_dirty(controller, identifier) -> None:
    """Record a frame that the adapter accepted and then dropped.

    push_input_image returns True as soon as a frame reaches the mirror slot of
    the input, and the window can unmap before that paint runs, so no engine
    call is left to return False. load_from_changes replays what lands here.
    """
    # The markers dict lives on the controller, so a detached UI client can
    # ask the engine to composite again.
    try:
        controller.ui_image_changes_while_hidden[identifier] = True
    except Exception:
        # A controller torn down mid-flight has nothing left to replay to.
        log.opt(exception=True).debug("Could not record a dropped preview frame")


class _MirrorSlot:
    """Latest-wins hand-off of the preview frames of one input.

    A producer leaves a paint-ready payload here and arms at most one main-loop
    callback. That callback paints whatever the slot holds when it runs, so a
    backlogged loop keeps one callback and one payload per input, and the
    newest frame is the one that lands.
    """

    __slots__ = ("_interval", "_lock", "_pending", "_armed", "_last_drain")

    def __init__(self, interval: float = 0.0) -> None:
        # A floor on the drain rate, for a preview that is worth a limit of its
        # own. 0 drains as fast as the loop allows. A delayed callback still
        # flushes a held frame, so the last frame of a burst lands.
        self._interval = interval
        self._lock = threading.Lock()
        self._pending: object | None = None
        self._armed: bool = False
        # Infinitely far in the past, so the first frame paints at once.
        self._last_drain: float = float("-inf")

    def offer(self, payload: object) -> float | None:
        """Producer side, any thread. Makes payload the frame to paint.

        It replaces a frame that no callback painted yet. Returns the seconds
        to wait before the drain, or None when a drain is already armed. That
        callback then takes this payload, which keeps the callback count at one.
        """
        with self._lock:
            self._pending = payload
            if self._armed:
                return None
            self._armed = True
            return max(0.0, self._interval - (time.monotonic() - self._last_drain))

    def take(self) -> object | None:
        """Main loop. Returns the frame to paint and disarms the slot.

        Returns None when the slot is empty, so a callback that a producer
        armed in the gap between this call and the paint does nothing.
        """
        with self._lock:
            payload, self._pending = self._pending, None
            self._armed = False
            if payload is not None:
                self._last_drain = time.monotonic()
            return payload

    def disarm(self) -> None:
        """Undo an offer whose callback never reached the loop.

        The payload stays. Without this the slot stays armed, and the preview
        of the input freezes with a frame behind it.
        """
        with self._lock:
            self._armed = False


class GtkUIAdapter(ui_port.UIPort):
    def __init__(self):
        # Maps a controller to its DeckStackChild. DeckStack.add_page and
        # DeckStack.remove_page maintain it. The bind uses object identity at
        # add time, with no serial match and no ListModel scan from the media
        # thread.
        self._children: dict = {}
        self._window = None
        # The map and unmap handlers of the window write this bool, and the
        # media thread reads it without a lock. It replaces an off-main
        # main_win.get_mapped() widget read.
        self._window_mapped: bool = False
        # Maps (controller, identifier) to a _MirrorSlot. One slot per input,
        # so a stalled main loop holds one frame per input, not a queue.
        self._mirror_slots: dict = {}
        # Maps a controller to a bool. This is the page-sync coalescer.
        self._page_sync_queued: dict = {}

    # Setup

    def attach_window(self, window) -> None:
        """Bind to a built MainWindow.

        It runs after the constructor, because the map and unmap handlers need
        a real window. The adapter installs before the constructor, because
        every boot-time add_page runs inside MainWindow.build().
        """
        self._window = window
        try:
            self._window_mapped = bool(window.get_mapped())
            window.connect("map", self._on_window_map)
            window.connect("unmap", self._on_window_unmap)
        except Exception:
            log.opt(exception=True).warning("Could not track the main window's mapped state")
        # Re-scan the deck stack, so a child that the constructor added binds
        # too.
        self.rescan_children()
        self.reconcile_children()

    def reconcile_children(self) -> None:
        """Heal the decks that the window constructor could not see.

        rescan_children re-binds only the children that exist, so this method
        reconciles both directions against the deck manager list.
        """
        # on_deck_added and on_deck_removed do nothing while _window is None,
        # which is the period that MainWindow.__init__ occupies. A deck that
        # the USB monitor plugs in then gets no stack child, and a deck that it
        # unplugs leaves a stale one.
        window = self._window
        if not recursive_hasattr(window, "leftArea.deck_stack"):
            return
        deck_stack = window.leftArea.deck_stack
        registered = getattr(getattr(gl, "deck_manager", None), "deck_controller", None)
        if registered is None:
            # No deck manager to reconcile against. Return instead of reading
            # this as an empty deck list, because the removal pass below then
            # tears down every bound child. main.py builds gl.deck_manager
            # before App, so this state does not occur.
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
        """Bind every controller whose DeckStackChild is in the stack.

        This makes the bind independent of the adapter install order, and it
        heals a rebuilt window.
        """
        window = self._window
        if not recursive_hasattr(window, "leftArea.deck_stack"):
            return
        for page in window.leftArea.deck_stack.get_pages():
            if page is None:
                # The ListModel iteration reads the length once, so a removed
                # trailing index yields None. Only trailing entries are None.
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
        # Snapshot the keys, then delete without a KeyError. This method runs
        # off the main loop, on the USB monitor, boot rescan and flatpak poll
        # threads, and it is not the only writer. The media thread creates a
        # slot at the first mirror of an input, so a scan of the live dict can
        # see the size change. Every armed drain for this controller pops its
        # own slot from the GTK loop once the child goes, so a key in the
        # snapshot can be gone already. A raise here leaves on_deck_removed,
        # skips close(), and strands the media thread and the USB handle.
        for key in [k for k in list(self._mirror_slots) if k[0] is controller]:
            self._mirror_slots.pop(key, None)

    def _on_window_map(self, *args) -> None:
        self._window_mapped = True

    def _on_window_unmap(self, *args) -> None:
        self._window_mapped = False

    # Resolvers

    def _grid(self, child):
        if not recursive_hasattr(child, "page_settings.deck_config.grid"):
            return None
        return child.page_settings.deck_config.grid

    def _screenbar(self, child):
        if not recursive_hasattr(child, "page_settings.deck_config.screenbar.image"):
            return None
        return child.page_settings.deck_config.screenbar

    def _mirror_widget(self, child, identifier):
        """The widget that mirrors identifier, or None when there is none.

        This raises during a grid rebuild, because buttons[x][y] can be short
        of these coordinates, so every caller contains the exception.
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

    # Render mirror

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

            # Convert on this thread, the media thread for a live frame.
            # prepare_mirror_frame uses only PIL and GdkPixbuf, so the main
            # loop receives the finished payload.
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
                # Both arms use idle priority. A pixbuf update above the GTK
                # layout and draw priority of 120 starves the redraw that it
                # feeds. timeout_add defaults to priority 0, so the delayed
                # arm names the priority.
                if delay_s <= 0:
                    GLib.idle_add(self._drain_mirror, controller, identifier)
                else:
                    GLib.timeout_add(int(delay_s * 1000) + 1, self._drain_mirror,
                                     controller, identifier,
                                     priority=GLib.PRIORITY_DEFAULT_IDLE)
            except BaseException:
                # No callback drains this slot now, and an armed slot freezes
                # the input.
                slot.disarm()
                raise
            return True
        except Exception:
            # The failure set is open. A widget lookup races the window
            # teardown, and prepare_mirror_frame runs PIL and
            # GdkPixbuf.new_from_bytes. Contain all of it. This code runs under
            # the media tick, whose catch-all waits 0.25 s per exception, and a
            # failed preview must not throttle the deck writer loop.
            log.opt(exception=True).warning(f"Failed to mirror {identifier} into the UI")
            return False

    def _drain_mirror(self, controller, identifier) -> bool:
        # On the main loop, paint the newest frame of this input. Return
        # False, because a GLib callback that returns a true value re-arms.
        slot = self._mirror_slots.get((controller, identifier))
        if slot is None:
            return False
        payload = slot.take()
        if payload is None:
            return False
        try:
            # Resolve again here instead of a capture at push time. The bind
            # uses deck-stack-child identity, and a grid that rebuilds in the
            # gap would else receive a frame for its orphaned predecessor.
            child = self._children.get(controller)
            if child is None:
                # The unbind landed between the push and this paint, so drop
                # the slot too. A push that races unbind() makes a new one, and
                # a slot keyed by a dead controller pins its whole graph.
                self._mirror_slots.pop((controller, identifier), None)
            widget = None if child is None else self._mirror_widget(child, identifier)
            if not self._window_mapped or widget is None:
                # The adapter accepted and then dropped this frame.
                # push_input_image already returned True, so nothing else
                # records it.
                mark_dirty(controller, identifier)
                return False
            widget.paint_mirror_frame(payload)
        except Exception:
            log.opt(exception=True).warning(f"Failed to paint the {identifier} mirror")
            mark_dirty(controller, identifier)
        return False

    # Deck sync

    def on_page_changed(self, controller) -> None:
        # Coalesce the page-load completions into one pending idle, so a burst
        # of page changes does not queue a sidebar rebuild for each one. Each
        # callback renders the live state, so the last completion wins. The
        # check-then-set race between the two trigger threads costs at most two
        # idles that render the same state.
        if self._page_sync_queued.get(controller):
            return
        self._page_sync_queued[controller] = True
        GLib.idle_add(self._run_page_changed, controller)

    def _run_page_changed(self, controller) -> bool:
        # Use pop, not an assignment of False. An idle queued before unbind()
        # still runs after it, and a re-inserted key pins the whole graph of an
        # unplugged controller.
        self._page_sync_queued.pop(controller, None)
        window = self._window
        if not recursive_hasattr(window, "sidebar"):
            return False
        child = self._children.get(controller)
        if child is None:
            return False
        # The sidebar mirrors the selected input of the visible deck, so a
        # page change on a background deck must not reload it.
        if window.leftArea.deck_stack.get_visible_child() is not child:
            return False
        sidebar = window.sidebar
        # Do not pull the user out of a sub-view. Sidebar.load_for_* sets
        # main_stack back to the input editor, so a refresh while the
        # ActionChooser, the ActionConfigurator or the error page is up moves a
        # user away in the middle of an edit.
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
        """The sidebar, only while it shows identifier of controller.

        This runs on the main loop, and it holds the widget reads.
        """
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
        """Rebuild the key grid of the deck for a new rotation.

        This runs inline on the main loop, because the one caller of
        set_rotation runs there and reloads the page at once. An idled rebuild
        lets those repaints reach the grid from before the rotation, where the
        transposed buttons raise IndexError and the frames drop with no marker.
        """
        if threading.current_thread() is threading.main_thread():
            self._run_deck_layout_changed(controller)
            return
        GLib.idle_add(self._run_deck_layout_changed, controller)

    def _run_deck_layout_changed(self, controller) -> bool:
        # Function-local, because KeyGrid imports this module for mark_dirty.
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

    # Deprecated queries

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

    # App level

    def on_deck_added(self, controller) -> None:
        window = self._window
        if not recursive_hasattr(window, "leftArea.deck_stack"):
            return
        GLib.idle_add(window.leftArea.deck_stack.add_page, controller)

    def on_deck_removed(self, controller) -> None:
        # Queue the detach idle here, before the return. The caller starts the
        # slow close thread at once, and a fast unplug and replug must not race
        # a late detach against a new add_page idle, which leaves two stack
        # children for one serial.
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
