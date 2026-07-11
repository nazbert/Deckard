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
import gi

from GtkHelper.GtkHelper import RevertButton
from src.backend.DeckManagement.InputIdentifier import InputIdentifier, Input
from src.backend.DeckManagement.HelperMethods import add_default_keys, is_video
from src.backend.DeckManagement.ImageHelpers import image2pixbuf

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Pango, GLib

# Import Python modules
from loguru import logger as log

# Import globals
import globals as gl


def build_preview_pixbuf(image_path: str | None):
    """Pixbuf for paths Gtk.Picture cannot render directly (videos, via their
    thumbnail); None means set_filename can handle the path itself."""
    if image_path and is_video(image_path):
        try:
            return image2pixbuf(gl.media_manager.get_thumbnail(image_path))
        except Exception as e:
            log.error(f"Could not build video preview thumbnail for {image_path}: {e}")
    return None


class BackgroundEditor(Gtk.Box):
    def __init__(self, sidebar, **kwargs):
        self.sidebar = sidebar
        super().__init__(**kwargs)
        self.build()

    def build(self):
        self.clamp = Adw.Clamp()
        self.append(self.clamp)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.clamp.set_child(self.main_box)

        self.background_group = BackgroundGroup(self.sidebar)
        self.main_box.append(self.background_group)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.background_group.load_for_identifier(identifier, state)


class BackgroundGroup(Adw.PreferencesGroup):
    def __init__(self, sidebar, **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar

        self.build()

    def build(self):
        self.expander = BackgroundExpanderRow(self)
        self.add(self.expander)

        return

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.expander.load_for_identifier(identifier, state)

class BackgroundExpanderRow(Adw.ExpanderRow):
    def __init__(self, label_group):
        super().__init__(title=gl.lm.get("background-editor.header"), subtitle=gl.lm.get("background-editor-expander.subtitle"))
        self.label_group = label_group
        self.active_identifier: InputIdentifier = None
        self.active_state = None
        self.build()

    def build(self):
        self.color_row = ColorRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.color_row)

        self.image_row = ImageRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.image_row)

        self.video_loop_row = VideoLoopRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.video_loop_row)

        self.video_fps_row = VideoFpsRow(sidebar=self.label_group.sidebar, expander=self)
        self.add_row(self.video_fps_row)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.active_identifier = identifier
        self.active_state = state

        self.color_row.load_for_identifier(identifier, state)

        # Only show image row for touchscreens
        is_touchscreen = isinstance(identifier, Input.Touchscreen)
        self.image_row.set_visible(is_touchscreen)
        if is_touchscreen:
            self.image_row.load_for_identifier(identifier, state)

        self.update_video_rows()

    def update_video_rows(self) -> bool:
        # Loop/FPS only exist while a video is configured. For the
        # touchscreen that is its background image; for keys/dials it is
        # their media (FPS row only -- GIFs have their own timeline, and
        # media loop stays a page-dict/plugin concern for now).
        show_loop = False
        show_fps = False
        active_page = gl.app.main_win.get_active_page()
        if isinstance(self.active_identifier, Input.Touchscreen):
            path = active_page.get_background_image(identifier=self.active_identifier, state=self.active_state)
            show_loop = show_fps = bool(path and is_video(path))
        elif isinstance(self.active_identifier, (Input.Key, Input.Dial)):
            path = active_page.get_media_path(identifier=self.active_identifier, state=self.active_state)
            show_fps = bool(path and is_video(path) and not str(path).lower().endswith(".gif"))
        self.video_loop_row.set_visible(show_loop)
        self.video_fps_row.set_visible(show_fps)
        if show_loop:
            self.video_loop_row.load_for_identifier(self.active_identifier, self.active_state)
        if show_fps:
            self.video_fps_row.load_for_identifier(self.active_identifier, self.active_state)
        return False  # usable directly as a GLib.idle_add callback

class ColorRow(Adw.PreferencesRow):
    def __init__(self, sidebar, expander: BackgroundExpanderRow, **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        self.active_identifier: InputIdentifier = None
        self.active_state = None
        self.build()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label=gl.lm.get("background-editor.color.label"), xalign=0, hexpand=True)
        self.main_box.append(self.label)
        self.button = ColorButton(self)
        self.main_box.append(self.button)

        self.color_dialog = Gtk.ColorDialog(title=gl.lm.get("background-editor.color.dialog.title"))

        self.button.button.set_dialog(self.color_dialog)

        self.connect_signals()

    def connect_signals(self):
        self.button.button.connect("notify::rgba", self.on_change_color)
        self.button.revert_button.connect("clicked", self.on_revert)

    def disconnect_signals(self):
        try:
            self.button.button.disconnect_by_func(self.on_change_color)
        except:
            pass

    def set_color(self, color_values: list):
        if len(color_values) == 3:
            color_values.append(255)
        color = Gdk.RGBA()
        color.parse(f"rgba({color_values[0]}, {color_values[1]}, {color_values[2]}, {color_values[3]/255})")
        self.button.button.set_rgba(color)

    def on_change_color(self, *args):
        color = self.button.button.get_rgba()
        green = round(color.green * 255)
        blue = round(color.blue * 255)
        red = round(color.red * 255)
        alpha = round(color.alpha * 255)

        active_page = gl.app.main_win.get_active_page()
        active_page.set_background_color(identifier=self.active_identifier, state=self.active_state, color=[red, green, blue, alpha], update_ui=False)

        self.button.revert_button.set_visible(True)

    def on_revert(self, *args):
        self.disconnect_signals()
        active_page = gl.app.main_win.get_active_page()
        active_page.set_background_color(identifier=self.active_identifier, state=self.active_state, color=None, update_ui=True)
        self.button.revert_button.set_visible(False)
        self.connect_signals()

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.disconnect_signals()

        self.active_identifier = identifier
        self.active_state = state

        active_page = gl.app.main_win.get_active_page()

        c_input = active_page.deck_controller.get_input(identifier)
        if c_input is None:
            log.error("Input not found")
            return
        
        c_state = c_input.states.get(state)
        if c_state is None:
            log.error("State not found")
            return

        color = active_page.get_background_color(identifier=identifier, state=self.active_state)
        color = c_state.background_manager.get_composed_color()

        self.set_color(color)

        self.button.revert_button.set_visible(c_state.background_manager.get_use_page_background())

        self.connect_signals()

class ColorButton(Gtk.Box):
    def __init__(self, color_row: ColorRow, **kwargs):
        super().__init__(css_classes=["linked"], **kwargs)
        
        self.button = Gtk.ColorDialogButton()
        self.revert_button = RevertButton()

        self.append(self.button)
        self.append(self.revert_button)

class ResetColorButton(Adw.PreferencesRow):
    def __init__(self, color_row: ColorRow, **kwargs):
        super().__init__(**kwargs, css_classes=["no-padding", "reset-button"])
        self.color_row: ColorRow = color_row

        self.button = Gtk.Button(hexpand=True, vexpand=True, overflow=Gtk.Overflow.HIDDEN,
                                 css_classes=["no-margin", "invisible"],
                                 label=gl.lm.get("background-editor.color.reset"),
                                 margin_bottom=5, margin_top=5)
        self.button.connect("clicked", self.on_click)
        self.set_child(self.button)

    def on_click(self, button):
        active_page = gl.app.main_win.get_active_page()
        #TODO: Detatch signal from button
        active_page.set_background_color(identifier=self.color_row.active_identifier, state=self.color_row.active_state, color=None, update_ui=True)

    def update(self):
        color = self.color_row.button.get_rgba()
        green = round(color.green * 255)
        blue = round(color.blue * 255)
        red = round(color.red * 255)
        alpha = round(color.alpha * 255)

        # Only show button if color is not the default of [0, 0, 0, 0]
        if [red, green, blue, alpha] == [0, 0, 0, 0]:
            self.set_visible(False)
        else:
            self.set_visible(True)

class VideoLoopRow(Adw.PreferencesRow):
    def __init__(self, sidebar, expander: BackgroundExpanderRow, **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        self.active_identifier: InputIdentifier = None
        self.active_state = None
        self.build()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="Loop", xalign=0, hexpand=True)
        self.main_box.append(self.label)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.main_box.append(self.switch)

        self.connect_signals()

    def connect_signals(self):
        self.switch.connect("notify::active", self.on_toggle)

    def disconnect_signals(self):
        try:
            self.switch.disconnect_by_func(self.on_toggle)
        except:
            pass

    def on_toggle(self, *args):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_background_loop(identifier=self.active_identifier, state=self.active_state,
                                        loop=self.switch.get_active(), update=True)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.disconnect_signals()
        self.active_identifier = identifier
        self.active_state = state
        active_page = gl.app.main_win.get_active_page()
        self.switch.set_active(active_page.get_background_loop(identifier=identifier, state=state))
        self.connect_signals()


class VideoFpsRow(Adw.PreferencesRow):
    def __init__(self, sidebar, expander: BackgroundExpanderRow, **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        self.active_identifier: InputIdentifier = None
        self.active_state = None
        self.build()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="FPS", xalign=0, hexpand=True)
        self.main_box.append(self.label)

        # 30 = MediaPlayerThread.FPS, the loop's render ceiling -- the same
        # range every other fps spinner in the app offers.
        self.spinner = Gtk.SpinButton.new_with_range(1, 30, 1)
        self.spinner.set_valign(Gtk.Align.CENTER)
        self.main_box.append(self.spinner)

        self.connect_signals()

    def connect_signals(self):
        self.spinner.connect("value-changed", self.on_change)

    def disconnect_signals(self):
        try:
            self.spinner.disconnect_by_func(self.on_change)
        except:
            pass

    def _uses_media_fps(self) -> bool:
        # Keys/dials cap their MEDIA video; the touchscreen caps its
        # background video.
        return isinstance(self.active_identifier, (Input.Key, Input.Dial))

    def on_change(self, *args):
        active_page = gl.app.main_win.get_active_page()
        fps = int(self.spinner.get_value())
        if self._uses_media_fps():
            active_page.set_media_fps(identifier=self.active_identifier, state=self.active_state,
                                      fps=fps, update=True)
        else:
            active_page.set_background_fps(identifier=self.active_identifier, state=self.active_state,
                                           fps=fps, update=True)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.disconnect_signals()
        self.active_identifier = identifier
        self.active_state = state
        active_page = gl.app.main_win.get_active_page()
        if self._uses_media_fps():
            self.spinner.set_value(active_page.get_media_fps(identifier=identifier, state=state))
        else:
            self.spinner.set_value(active_page.get_background_fps(identifier=identifier, state=state))
        self.connect_signals()


class ImageRow(Adw.PreferencesRow):
    def __init__(self, sidebar, expander: BackgroundExpanderRow, **kwargs):
        super().__init__(**kwargs)
        self.sidebar = sidebar
        self.expander = expander
        self.active_identifier: InputIdentifier = None
        self.active_state = None
        self.build()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True,
                                margin_start=15, margin_end=15, margin_top=15, margin_bottom=15,
                                spacing=10)
        self.set_child(self.main_box)

        self.label = Gtk.Label(label="Background", xalign=0, hexpand=True)
        self.main_box.append(self.label)
        
        # Image preview with constrained size
        self.preview_frame = Gtk.Frame(css_classes=["card"])
        self.preview_frame.set_size_request(48, 48)
        self.preview = Gtk.Picture(
            content_fit=Gtk.ContentFit.COVER,
            overflow=Gtk.Overflow.HIDDEN,
            width_request=48,
            height_request=48,
        )
        self.preview_frame.set_child(self.preview)
        self.preview_frame.set_visible(False)
        self.main_box.append(self.preview_frame)
        
        self.button_box = Gtk.Box(css_classes=["linked"])
        self.main_box.append(self.button_box)
        
        self.select_button = Gtk.Button(icon_name="folder-open-symbolic")
        self.select_button.connect("clicked", self.on_select_image)
        self.button_box.append(self.select_button)
        
        self.clear_button = Gtk.Button(icon_name="edit-clear-symbolic")
        self.clear_button.connect("clicked", self.on_clear_image)
        self.clear_button.set_visible(False)
        self.button_box.append(self.clear_button)

    def on_select_image(self, button):
        active_page = gl.app.main_win.get_active_page()
        current_path = active_page.get_background_image(identifier=self.active_identifier, state=self.active_state)
        gl.app.let_user_select_asset(default_path=current_path, callback_func=self.set_background_image)

    def set_background_image(self, file_path: str) -> None:
        if not file_path:
            return
        active_page = gl.app.main_win.get_active_page()
        active_page.set_background_image(identifier=self.active_identifier, state=self.active_state, path=file_path, update=True)
        # May run off-main (the custom-assets chooser delivers selections on a
        # callback thread) -- widget mutations must be marshalled onto the GTK
        # main loop.
        GLib.idle_add(self.clear_button.set_visible, True)
        GLib.idle_add(self.expander.update_video_rows)
        self.update_preview(file_path)

    def on_clear_image(self, button):
        active_page = gl.app.main_win.get_active_page()
        active_page.set_background_image(identifier=self.active_identifier, state=self.active_state, path=None, update=True)
        self.clear_button.set_visible(False)
        self.expander.update_video_rows()
        self.update_preview(None)

    def update_preview(self, image_path: str | None):
        # Thread-safe: any thumbnail decode happens here (possibly off-main);
        # the actual widget updates are marshalled to the GTK main loop.
        GLib.idle_add(self._apply_preview, image_path, build_preview_pixbuf(image_path))

    def _apply_preview(self, image_path: str | None, pixbuf) -> bool:
        if image_path:
            if pixbuf is not None:
                self.preview.set_pixbuf(pixbuf)
            else:
                self.preview.set_filename(image_path)
            self.preview_frame.set_visible(True)
        else:
            self.preview.set_filename(None)
            self.preview_frame.set_visible(False)
        return False

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.active_identifier = identifier
        self.active_state = state

        active_page = gl.app.main_win.get_active_page()
        image_path = active_page.get_background_image(identifier=identifier, state=state)
        self.clear_button.set_visible(image_path is not None)
        self.update_preview(image_path)