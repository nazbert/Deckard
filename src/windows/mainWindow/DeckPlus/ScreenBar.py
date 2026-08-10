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

        # Both None outside a drag -- reset on every press (see on_drag_*).
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
        # Apply changes accumulated before creation of self, or while the
        # window was hidden (mem plan P5.4): the entry is a dirty MARKER, not
        # a stashed PIL image -- recomposite the current frame and push it
        # through the same set-image path a live update would use.
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
        
        states = self.identifier.get_states(active_page)
        if str(screen.state) not in states:
            return

        del states[str(screen.state)]
        active_page.save()
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

        # next() on a count is atomic; a read-modify-write on latest_task_id
        # would hand two frames the same id now that producers are threads,
        # letting a stale one pass the check in set_pixbuf_and_del.
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
        # Callable from any thread; the map-time replay path. Live frames come
        # through the UI adapter, which calls the same two halves but
        # coalesces the paints into one per input.
        # Default idle priority: high-priority pixbuf updates every frame can
        # starve the main loop's layout/draw.
        GLib.idle_add(self.paint_mirror_frame, self.prepare_mirror_frame(image))

    def prepare_mirror_frame(self, image: Image.Image) -> tuple[Any, int]:
        """The paint-ready payload for `paint_mirror_frame`.

        Any thread: the thumbnail and both conversions are pure PIL +
        GdkPixbuf, so they run on the caller (the media thread for live
        frames) and only the paint needs the loop. The mapped check lives in
        set_pixbuf_and_del - widget state can't be read from off-main.

        The task id travels WITH the pixbuf so a paint that lost the race to a
        newer frame still drops out in set_pixbuf_and_del.
        """
        width = 385 #TODO: Find a better way to do this
        thumbnail = image.copy()
        thumbnail.thumbnail((width, width/8))

        pixbuf = image2pixbuf(thumbnail.convert("RGBA"), force_transparency=True)
        self.latest_task_id = self.get_new_task_id()
        task_id = self.latest_task_id

        thumbnail.close()
        del thumbnail

        self._push_dial_preview(image)
        return pixbuf, task_id

    def paint_mirror_frame(self, payload: tuple[Any, int]) -> bool:
        # Main loop only. Returns False: a GLib idle callback that returns
        # truthy re-arms forever.
        pixbuf, task_id = payload
        self.set_pixbuf_and_del(pixbuf, task_id)
        return False

    def _push_dial_preview(self, image: Image.Image) -> None:
        # The sidebar's icon preview for a selected dial is cropped out of the
        # strip frame, so it rides along with the conversion above -- on the
        # producer thread, off the loop.
        if gl.app is None or not recursive_hasattr(gl, "app.main_win.sidebar"):
            return

        identifier = gl.app.main_win.sidebar.active_identifier
        if isinstance(identifier, Input.Dial):
            # Own controller, not whichever deck happens to be visible: this
            # widget belongs to one deck, and resolving via the deck stack
            # would be a GTK read on the producer thread.
            touch_screen = self.screenbar.deck_controller.get_input(Input.Touchscreen("sd-plus"))
            if touch_screen is None:
                return
            dial_image_area = touch_screen.get_dial_image_area(identifier)

            dial_image = image.crop(dial_image_area)

            # Converts on this thread as well and idles only its own paint.
            gl.app.main_win.sidebar.key_editor.icon_selector.set_image(dial_image)

    def set_pixbuf_and_del(self, pixbuf, task_id: int = None):
        if task_id is not None:
            if task_id != self.latest_task_id:
                log.debug("Screenbar: Abort task")
                return
        # Skip if the widget was unmapped between queuing and running this
        # callback: painting a disposed widget crashes GTK.
        try:
            if not self.get_mapped():
                # Replay this pixbuf on map. Secondary net only: the P5.4
                # dirty-mark path recomposites a fresh frame when the window
                # comes back, which is what normally repaints the preview.
                self.on_map_tasks = [lambda: self.set_pixbuf_and_del(pixbuf)]
                return
            self.set_pixbuf(pixbuf)
        except Exception as e:
            log.debug(f"Screenbar mirror paint skipped: {e}")