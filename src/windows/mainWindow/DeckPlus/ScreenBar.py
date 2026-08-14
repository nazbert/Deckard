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
# Import gtk modules
import itertools
import time
import gi
from loguru import logger as log

from PIL import Image

from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.DeckManagement.HelperMethods import recursive_hasattr

from StreamDeck.Devices.StreamDeck import TouchscreenEventType

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import globals as gl

from gi.repository import Gtk, GLib, Gio

from collections.abc import Callable
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.mainWindow.elements.PageSettingsPage import PageSettingsPage

# An icon selector, a dial pixbuf and its task id, or None when the user
# selected no dial.
DialPreview = tuple[Any, Any, int] | None
# One mirrored strip frame carries a pixbuf, a task id and a dial preview.
MirrorFrame = tuple[Any, int, DialPreview]

class ScreenBar(Gtk.Frame):
    def __init__(self, page_settings_page: "PageSettingsPage", identifier: Input.Touchscreen, **kwargs):
        self.page_settings_page = page_settings_page
        self.deck_controller = page_settings_page.deck_controller
        self.identifier = identifier

        super().__init__(**kwargs)
        self.set_css_classes(["key-button-frame-hidden"])
        self.set_halign(Gtk.Align.CENTER)
        self.set_hexpand(True)
        # self.set_size_request(80, 10)

        self.pixbuf = None

        # self.image = Gtk.Image(css_classes=["key-image", "plus-screenbar"], hexpand=True, vexpand=True)
        # self.image.set_overflow(Gtk.Overflow.HIDDEN)
        # self.image.set_from_file("Assets/800_100.png")

        self.image = ScreenBarImage(self)
        self.image.set_image(Image.new("RGBA", (800, 100), (0, 0, 0, 0)))
        self.set_child(self.image)

        # self.set_child(self.image)
        focus_controller = Gtk.EventControllerFocus()
        self.image.add_controller(focus_controller)
        focus_controller.connect("enter", self.on_focus_in)

        self.click_ctrl = Gtk.GestureClick().new()
        self.click_ctrl.connect("pressed", self.on_click)
        self.click_ctrl.connect("released", self.on_released)
        self.click_ctrl.set_button(0)
        self.image.add_controller(self.click_ctrl)

        # Make image focusable
        self.set_focus_child(self.image)
        self.image.set_focusable(True)

        self.min_drag_distance = 20
        self.long_press_treshold = 0.5

        # Both stay None outside a drag, and every press resets them. See the
        # on_drag_ handlers.
        self.drag_start_xy: tuple[int, int] | None = None
        self.drag_start_time: float | None = None

        ## Actions
        self.action_group = Gio.SimpleActionGroup()
        self.insert_action_group("screen", self.action_group)

        self.remove_action = Gio.SimpleAction.new("remove", None)
        self.remove_action.connect("activate", self.on_remove)
        self.action_group.add_action(self.remove_action)

        ## Shortcuts
        self.shortcut_controller = Gtk.ShortcutController()
        self.add_controller(self.shortcut_controller)

        remove_shortcut_action = Gtk.CallbackAction.new(self.on_remove)  # type: ignore[arg-type]  # gi stub: GtkShortcutFunc is typed Callable[..., bool]; PyGObject coerces a None return to False, which is this handler's existing behaviour

        self.remove_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Delete"), remove_shortcut_action)
        self.shortcut_controller.add_shortcut(self.remove_shortcut)

        self.connect("map", self.on_map)

        self.load_from_changes()

    def on_map(self, widget):
        self.load_from_changes()

    def load_from_changes(self) -> None:
        # Apply the changes that arrived before this widget existed, or while
        # the window was hidden. Each entry is a dirty marker and not a stored
        # PIL image, so this composites the current frame again and pushes it
        # through the set-image path that a live update uses.
        if not hasattr(self.deck_controller, "ui_image_changes_while_hidden"):
            return
        tasks = self.deck_controller.ui_image_changes_while_hidden

        if self.identifier in tasks:
            controller_input = self.deck_controller.get_input(self.identifier)
            if controller_input is not None:
                try:
                    self.image.set_image(controller_input.get_current_image())
                except Exception:
                    log.exception(f"Failed to recomposite {self.identifier} on map")
            try:
                tasks.pop(self.identifier)
            except KeyError:
                pass

    def on_click(self, gesture, n_press, x, y):
        # print(f"Click: {self.parse_xy(x, y)}")
        self.drag_start_xy = None
        self.drag_start_time = None
        if gesture.get_current_button() == 1 and n_press == 1:
            if self.image.has_focus():
                self.drag_start_xy = self.parse_xy(x, y)
                self.drag_start_time = time.time()
            # Single left click
            # Select key
            self.image.grab_focus()

            controller_input = self.page_settings_page.deck_controller.get_input(self.identifier)
            state = controller_input.get_active_state().state
            gl.app.main_win.sidebar.load_for_identifier(self.identifier, state)
            
        elif gesture.get_current_button() == 1 and n_press == 2:
            pass
            # Double left click
            # Simulate key press
            # self.simulate_press()

    def on_released(self, gesture, n_press, x, y):
        if None in [self.drag_start_xy, self.drag_start_time]:
            return
        # print(f"Release: {self.parse_xy(x, y)}")
        x, y = self.parse_xy(x, y)
        start_x, start_y = self.drag_start_xy
        drag_distance = abs(x - start_x) + abs(y - start_y)

        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return

        if drag_distance > self.min_drag_distance:
            # print(f"Drag from {start_x}, {start_y} to {x}, {y}")
            value = {
                "x": start_x,
                "y": start_y,
                "x_out": x,
                "y_out": y
            }
            # controller.touchscreen_event_callback(controller.deck, TouchscreenEventType.DRAG, value)
            controller.event_callback(self.identifier, TouchscreenEventType.DRAG, value)
            return
        
        if time.time() - self.drag_start_time >= self.long_press_treshold:
            controller.event_callback(self.identifier, TouchscreenEventType.LONG, {"x": x, "y": y})
        
        else:
            controller.event_callback(self.identifier, TouchscreenEventType.SHORT, {"x": x, "y": y})

    def parse_xy(self, x, y) -> tuple[int, int]:
        width = self.image.get_width()
        height = self.image.get_height()

        # Map xy to 800x100
        x, y = int(x * 800 / width), int(y * 100 / height)

        x = max(0, min(x, 800))
        y = max(0, min(y, 100))

        return x, y


    def on_focus_in(self, *args):
        self.set_border_active(True)

    def set_border_active(self, active: bool):
        if active:
            if self.page_settings_page.deck_config.active_widget not in [self, None]:
                self.page_settings_page.deck_config.active_widget.set_border_active(False)
            self.page_settings_page.deck_config.active_widget = self
            self.set_css_classes(["key-button-frame"])
        else:
            self.set_css_classes(["key-button-frame-hidden"])
            self.page_settings_page.deck_config.active_widget = None

    def on_remove(self, *args) -> None:
        if gl.app is None:
            return
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        
        active_page = controller.active_page
        if active_page is None:
            return

        screen = controller.get_input(self.identifier)

        state_key = str(screen.state)
        if state_key not in self.identifier.get_states(active_page):
            return

        # The removal of the state is the save. The block holds the page
        # lock, so a write in flight cannot snapshot the removal half done,
        # and the exit marks the page once. Read the dict again inside the
        # block, because the check above read it without the lock.
        with active_page.edit():
            self.identifier.get_states(active_page).pop(state_key, None)

        active_page.load()

        active_page.reload_similar_pages(identifier=self.identifier, reload_self=True)

        # Reload ui
        gl.app.main_win.sidebar.load_for_identifier(self.identifier, screen.state)

class ScreenBarImage(Gtk.Picture):
    def __init__(self, screenbar: ScreenBar, **kwargs):
        super().__init__(keep_aspect_ratio=True, can_shrink=True, content_fit=Gtk.ContentFit.SCALE_DOWN,
                         halign=Gtk.Align.CENTER, hexpand=False, width_request=80, height_request=10,
                         valign=Gtk.Align.CENTER, vexpand=False, css_classes=["plus-screenbar-image"],
                         **kwargs)
        
        self.screenbar = screenbar

        self.on_map_tasks: list[Callable[[], Any]] = []
        self.connect("map", self.on_map)

        # next() on a count is atomic, so two frames never take the same id,
        # and the producers are threads. The publish is a plain store, so two
        # producers can land their ids out of order and the older one wins the
        # check in set_pixbuf_and_del. The next frame corrects that, and one
        # producer per screenbar is the normal case.
        self.task_ids = itertools.count()
        # None until the first frame is queued.
        self.latest_task_id: int | None = None

    def on_map(self, *args):
        for task in self.on_map_tasks:
            task()
        self.on_map_tasks.clear()

    def get_new_task_id(self):
        return next(self.task_ids)

    def set_image(self, image: Image.Image):
        # Callable from any thread. This is the map-time replay path. A live
        # frame arrives through the UI adapter, which calls the same two
        # halves and coalesces the paints into one per input. The idle takes
        # the default priority, because a high-priority pixbuf update on every
        # frame starves the layout and draw of the main loop.
        GLib.idle_add(self.paint_mirror_frame, self.prepare_mirror_frame(image))

    def prepare_mirror_frame(self, image: Image.Image) -> MirrorFrame:
        """The paint-ready payload for paint_mirror_frame.

        Any thread may call it. The thumbnail and every conversion use only PIL
        and GdkPixbuf, so they run on the caller, which is the media thread for
        a live frame. The mapped check lives in set_pixbuf_and_del, because
        widget state needs the main thread.
        """
        width = 385 #TODO: Find a better way to do this
        thumbnail = image.copy()
        thumbnail.thumbnail((width, width/8))

        pixbuf = image2pixbuf(thumbnail.convert("RGBA"), force_transparency=True)
        # The task id travels with the pixbuf, so a paint that lost the race to
        # a newer frame drops out in set_pixbuf_and_del. The stamp goes on this
        # widget while the mirror drain resolves the screenbar from the deck
        # stack again, so it decides only between frames of one widget. A
        # screenbar replaced between a push and its paint takes the id with it,
        # and the replacement stamps its own frames from its own counter.
        self.latest_task_id = self.get_new_task_id()
        task_id = self.latest_task_id

        thumbnail.close()
        del thumbnail

        return pixbuf, task_id, self._prepare_dial_preview(image)

    def paint_mirror_frame(self, payload: MirrorFrame) -> bool:
        # Main loop only. It returns False, because a GLib idle callback that
        # returns a true value re-arms.
        pixbuf, task_id, dial = payload
        self.set_pixbuf_and_del(pixbuf, task_id)
        if dial is not None:
            # This code already runs on the loop, so it needs no idle of its
            # own, and the payload of the strip keeps it coalesced.
            icon_selector, dial_pixbuf, dial_task_id = dial
            icon_selector.set_pixbuf_and_del(dial_pixbuf, dial_task_id)
        return False

    def _prepare_dial_preview(self, image: Image.Image) -> DialPreview:
        """The icon preview of the sidebar for a selected dial.

        The crop comes out of this same strip frame, and the conversion runs
        here on the producer. It travels in the payload of the strip, because
        the crop matches the frame it came from, so one payload keeps the two
        in step and costs no second callback. A direct call to
        IconSelector.set_image would add one uncoalesced idle per frame.
        """
        if gl.app is None or not recursive_hasattr(gl, "app.main_win.sidebar"):
            return None

        identifier = gl.app.main_win.sidebar.active_identifier
        if not isinstance(identifier, Input.Dial):
            return None
        # Use the own controller, not the visible deck. This widget belongs
        # to one deck, and a lookup through the deck stack is a GTK read on
        # the producer thread.
        touch_screen = self.screenbar.deck_controller.get_input(Input.Touchscreen("sd-plus"))
        if touch_screen is None:
            return None

        icon_selector = gl.app.main_win.sidebar.key_editor.icon_selector
        dial_image = image.crop(touch_screen.get_dial_image_area(identifier))
        pixbuf = image2pixbuf(dial_image.convert("RGBA"), force_transparency=True)
        # The same read-modify-write as the screenbar stamp. This frame reads
        # the id back after the store, so with two producers it can read the
        # id of a newer frame, and this frame drops in set_pixbuf_and_del. The
        # next frame corrects that, and one producer per screenbar is normal.
        icon_selector.latest_task_id = icon_selector.get_new_task_id()
        return icon_selector, pixbuf, icon_selector.latest_task_id

    def set_pixbuf_and_del(self, pixbuf, task_id: int = None):
        if task_id is not None:
            if task_id != self.latest_task_id:
                log.debug("Screenbar: Abort task")
                return
        # Skip when the widget unmapped between the queue and this callback,
        # because a paint on a disposed widget crashes GTK.
        try:
            if not self.get_mapped():
                # Replay this pixbuf on the map. This is a second net. The
                # dirty-mark path composites a fresh frame when the window
                # returns, and that repaints the preview.
                self.on_map_tasks = [lambda: self.set_pixbuf_and_del(pixbuf)]
                return
            self.set_pixbuf(pixbuf)
        except Exception as e:
            log.debug(f"Screenbar mirror paint skipped: {e}")