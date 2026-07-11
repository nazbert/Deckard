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
# Import gtk modules
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GdkPixbuf, GLib, Pango

# Import python modules
from loguru import logger as log

class Preview(Gtk.FlowBoxChild):
    def __init__(self, image_path: str = None, text:str = None, can_be_deleted: bool = False):
        super().__init__()
        self.set_css_classes(["asset-preview"])
        self.set_margin_start(5)
        self.set_margin_end(5)
        self.set_margin_top(5)
        self.set_margin_bottom(5)

        self.pixbuf: GdkPixbuf.Pixbuf = None
        self.can_be_deleted = can_be_deleted

        self._build()

        if image_path is not None:
            self.set_image(image_path)
        if text is not None:
            self.set_text(text)

    def _build(self):
        self.overlay = Gtk.Overlay()
        self.set_child(self.overlay)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=250, height_request=180)
        self.overlay.set_child(self.main_box)

        self.picture = Gtk.Picture(width_request=250, height_request=180, overflow=Gtk.Overflow.HIDDEN, content_fit=Gtk.ContentFit.COVER,
                                   hexpand=False, vexpand=False, keep_aspect_ratio=True)
        
        self.picture.set_pixbuf(self.pixbuf)
        self.main_box.append(self.picture)

        # Shown instead of the picture when the file can't be decoded (#112).
        # Hidden by default; set_image toggles it so recycled cells recover.
        self.broken_icon = Gtk.Image(icon_name="image-missing-symbolic", pixel_size=48,
                                     halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                     visible=False, tooltip_text="Could not load this file")
        self.overlay.add_overlay(self.broken_icon)

        self.label = Gtk.Label(xalign=0.5, hexpand=False, ellipsize=Pango.EllipsizeMode.END, max_width_chars=20,
                               margin_start=20, margin_end=20)
        self.main_box.append(self.label)

        self.info_button = Gtk.Button(icon_name="help-about-symbolic", halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=5, margin_bottom=5)
        self.info_button.connect("clicked", self.on_click_info)
        self.overlay.add_overlay(self.info_button)

        self.remove_button = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.END, halign=Gtk.Align.END, margin_end=5, margin_bottom=5, visible=self.can_be_deleted)
        self.remove_button.connect("clicked", self.on_click_remove)
        self.overlay.add_overlay(self.remove_button)

    def set_image(self, path:str):
        # The None check must run BEFORE any str() coercion (str(None) is the
        # truthy "None"), and the decode must be guarded: a corrupt/unreadable
        # file raises GLib.Error and previously killed the (idle) callback,
        # leaving the recycled cell showing a stale image (#112).
        if path is None:
            self.show_broken_image()
            return

        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path),
                                                             width=250,
                                                             height=180,
                                                             preserve_aspect_ratio=True)
        except GLib.Error as e:
            # Expected for a corrupt/unreadable file -- the message says why.
            log.warning(f"Could not load asset preview for {path}: {e}")
            self.show_broken_image()
            return
        except Exception as e:
            # Unexpected (programming error, not a poison file): keep the
            # traceback so it stays distinguishable in the logs.
            log.opt(exception=True).warning(f"Unexpected error loading asset preview for {path}: {e}")
            self.show_broken_image()
            return

        self.pixbuf = pixbuf
        self.picture.set_pixbuf(self.pixbuf)
        self.broken_icon.set_visible(False)

    def show_broken_image(self) -> None:
        """Marks this preview as broken: clears any (possibly recycled)
        pixbuf and shows the themed "image-missing" icon instead."""
        self.pixbuf = None
        self.picture.set_pixbuf(None)
        self.broken_icon.set_visible(True)

    def set_text(self, text:str):
        self.label.set_text(text)

    def on_click_info(self, *args):
        pass

    def on_click_remove(self, *args):
        pass