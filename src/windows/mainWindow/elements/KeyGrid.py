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

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerKey
from src.backend.DeckManagement.InputIdentifier import Input


gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GLib, Gio

# Import Python modules 
from loguru import logger as log

# Imort globals
import globals as gl

# Import own modules
from src.backend.DeckManagement.ImageHelpers import image2pixbuf
from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.windows.ui_adapter import mark_dirty

from typing import Any

class KeyGrid(Gtk.Grid):
    """
    Child of PageSettingsPage
    Key grid for the button config
    """
    def __init__(self, deck_controller, page_settings_page, **kwargs):
        super().__init__(**kwargs)
        self.deck_controller = deck_controller
        self.page_settings_page = page_settings_page

        self.selected_key = None # The selected key, indicated by a blue frame around it

        [y, x] = self.deck_controller.deck.key_layout()
        self.buttons = [[None] * y for i in range(x)]

        # Store the copied key from the page
        self.copied_key:dict = None

        self.build()

        self.connect("map", self.on_map)
        self.connect("unmap", self.on_unmap)

        self.load_from_changes()

        GLib.idle_add(self.select_key, 0, 0)

    def regenerate_buttons(self):
        [y, x] = self.deck_controller.deck.key_layout()
        self.buttons = [[None] * y for i in range(x)]
    
    def build(self):
        self.clear()

        layout = self.deck_controller.deck.key_layout()
        for x in range(layout[1]):
            for y in range(layout[0]):
                button = KeyButton(self, (x, y))
                self.attach(button, x, y, 1, 1)
                button._set_visible(False) # Hide buttons per default - they will be shown when the the grid is mapped to prevent large grids to resize every child
                self.buttons[x][y] = button
        return
        log.debug(self.deck_controller.deck.key_layout())
        l = Gtk.Label(label="Key Grid")
        self.attach(l, 0, 0, 1, 1)

    def load_from_changes(self):
        # Apply the changes that arrived before this widget existed, or while
        # the window was hidden. Each entry is a dirty marker and not a stored
        # PIL image, so this composites the current frame for each dirty
        # identifier and pushes it through the set-image path that a live
        # update uses.
        if not hasattr(self.deck_controller, "ui_image_changes_while_hidden"):
            return
        tasks = self.deck_controller.ui_image_changes_while_hidden
        for identifier in list(tasks.keys()):
            if isinstance(identifier, Input.Key):
                x, y = identifier.coords
                self._push_current_image(identifier, self.buttons[x][y])
                try:
                    tasks.pop(identifier)
                except KeyError:
                    pass
            elif isinstance(identifier, Input.Touchscreen):
                # ScreenBar.load_from_changes normally consumes this entry,
                # because it owns the widget that shows it. When the map
                # handler of that widget has not run, or never runs, consume
                # the entry here instead of leaking it. The tasks.pop() call
                # carries the same guard as the Key branch above, so the first
                # widget wins and the second does nothing.
                screenbar = self._find_screenbar()
                if screenbar is not None:
                    self._push_current_image(identifier, screenbar.image)
                    try:
                        tasks.pop(identifier)
                    except KeyError:
                        pass

    def _find_screenbar(self):
        """The sibling screenbar, found by a walk up the widget tree.

        The lookup is duck-typed, because an import of DeckStackChild or
        DeckConfig here is a cycle, and the engine caches no child to read.
        The lookup may fail. During __init__ this grid is not in the widget
        tree, because DeckConfig.build appends the grid before the screenbar
        exists, so the touchscreen replay waits for
        ScreenBar.load_from_changes.
        """
        widget = self.get_parent()
        while widget is not None:
            if recursive_hasattr(widget, "screenbar.image"):
                return widget.screenbar
            widget = widget.get_parent()
        return None

    def _push_current_image(self, identifier, widget) -> None:
        controller_input = self.deck_controller.get_input(identifier)
        if controller_input is None:
            return
        try:
            image = controller_input.get_current_image()
        except Exception:
            log.exception(f"Failed to recomposite {identifier} on map")
            return
        widget.set_image(image)
        
    def select_key(self, x: int, y: int):
        self.buttons[x][y].on_focus_in()
        self.buttons[x][y].image.grab_focus()

    def on_map(self, widget):
        self.load_from_changes()

        # Only show buttons when the grid is mapped to prevent large grids to resize every child
        self.set_buttons_visible(True)

    def on_unmap(self, widget):
        # Only show buttons when the grid is mapped to prevent large grids to resize every child
        self.set_buttons_visible(False)

    def clear(self):
        while self.get_first_child() is not None:
            self.remove(self.get_first_child())

    def set_buttons_visible(self, visible):
        for i in range(len(self.buttons)):
            for j in range(len(self.buttons[i])):
                self.buttons[i][j]._set_visible(visible)
        return
        for x in range(self.deck_controller.deck.key_layout()[1]):
            for y in range(self.deck_controller.deck.key_layout()[0]):
                self.buttons[x][y]._set_visible(visible)


class KeyButton(Gtk.Frame):
    def __init__(self, key_grid:KeyGrid, coords, **kwargs):
        super().__init__(**kwargs)
        self.set_css_classes(["key-button-frame-hidden"])
        self.coords = coords
        self.identifier = Input.Key(f"{coords[0]}x{coords[1]}")

        self.key_grid = key_grid

        self.pixbuf = None

        # self.button = Gtk.Button(hexpand=True, vexpand=True, css_classes=["key-button"])
        # self.set_child(self.button)

        self.image = Gtk.Image(hexpand=True, vexpand=True, css_classes=["key-image", "key-button"])
        self.image.set_overflow(Gtk.Overflow.HIDDEN)
        self.image.set_size_request(75, 75)
        self.image.set_pixel_size(75)
        self.set_child(self.image)

        # self.button.connect("clicked", self.on_click)

        focus_controller = Gtk.EventControllerFocus()
        self.add_controller(focus_controller)
        focus_controller.connect("enter", self.on_focus_in)

        # Click ctrl
        self.right_click_ctrl = Gtk.GestureClick().new()
        self.right_click_ctrl.connect("pressed", self.on_click)
        self.right_click_ctrl.set_button(0)
        self.image.add_controller(self.right_click_ctrl)

        # Make image focusable
        self.set_focus_child(self.image)
        self.image.set_focusable(True)

        self.init_actions()

        self.init_dnd()

        self.init_shortcuts()

    @property
    def state(self) -> int:
        return self.get_key().state

    def init_dnd(self) -> None:
        self.drag_source = Gtk.DragSource()
        self.drag_source.connect("prepare", self.on_drag_prepare)
        self.drag_source.connect("drag-begin", self.on_drag_begin)
        # self.drag_source.connect("drag-end", self.on_drag_end)
        self.add_controller(self.drag_source)

        self.button_dnd_target = Gtk.DropTarget.new(KeyButton, Gdk.DragAction.COPY)
        self.button_dnd_target.set_gtypes([KeyButton, Gdk.FileList])
        self.button_dnd_target.connect("accept", self.on_button_accept)
        self.button_dnd_target.connect("drop", self.on_button_drop)
        self.add_controller(self.button_dnd_target)

    def on_button_accept(self, drop: Gtk.DropTarget, user_data):
        return True

    # GTK4 passes the dropped value to the drop signal, not the content
    # provider. Here that value is a KeyButton or a Gdk.FileList. See
    # set_gtypes above.
    def on_button_drop(self, drop: Gtk.DropTarget, value: Any, x, y):
        if isinstance(drop.get_value(), KeyButton):
            self.handle_key_button_drop(drop, value, x, y)
       
        elif isinstance(drop.get_value(), Gdk.FileList):
            self.handle_file_drop(drop, value, x, y)

        else:
            drop.reject()
            return False
        
    def handle_key_button_drop(self, drop: Gtk.DropTarget, value: Any, x, y):
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return

        dropped_button = drop.get_value()
        if dropped_button is None:
            return
        dropped_identifier = dropped_button.identifier

        # The swap is one edit of the page, and not two assignments with a
        # save around each. Both halves are read and written inside the block,
        # so no writer sees a page where one key holds the content of the
        # other and its own content is nowhere. A deferred write between two
        # assignments puts that broken state on disk. Both sections also read
        # under the same lock, so a half-stale copy cannot overwrite a page
        # edit from a plugin thread. Nothing here touches the file, because
        # the reloads below run outside the block, without the lock.
        with active_page.edit() as page_dict:
            own_section = page_dict.setdefault(self.identifier.input_type, {})
            dropped_section = page_dict.setdefault(dropped_identifier.input_type, {})
            target_key_dict = own_section.get(self.identifier.json_identifier, {})
            dropped_key_dict = dropped_section.get(dropped_identifier.json_identifier, {})

            own_section[self.identifier.json_identifier] = dropped_key_dict
            dropped_section[dropped_identifier.json_identifier] = target_key_dict

        active_page.switch_actions_of_inputs(self.identifier, dropped_identifier)

        # Both keys repaint, one identifier at a time. Each reload writes the
        # page out before it reads the page again, so the swap is on disk when
        # these calls return. A save after them writes back only the content
        # that the last reload read from the file.
        active_page.reload_similar_pages(self.identifier, reload_self=True)
        active_page.reload_similar_pages(dropped_identifier, reload_self=True)

        # Reload sidebar
        if gl.app is not None:
            gl.app.main_win.sidebar.update()

    # value is the dropped Gdk.FileList and not the ContentProvider.
    # on_button_drop routes here only when drop.get_value() is a Gdk.FileList,
    # because GTK passes the value to the drop signal, not the provider.
    def handle_file_drop(self, drop: Gtk.DropTarget, value: Gdk.FileList, x, y):
        files = value.get_files()
        if len(files) > 1:
            drop.reject()
            return False
        
        file = files[0]
        url = file.get_uri()
        # A remote drop carries a uri and no local path. The importer owns
        # that case, validates the extension and tells the user, so this code
        # must not return early.
        path = file.get_path()

        internal_path = gl.asset_manager_backend.add_custom_media_set_by_ui(url=url, path=path)
        # Any result that is not a path means a refusal. The import can answer
        # a rejected url with -1, which passes an is None test and reaches the
        # media path of the key.
        if not isinstance(internal_path, str) or internal_path == "":
            return False

        # Set media to key
        active_page = self.key_grid.deck_controller.active_page

        state_dict = self.identifier.ensure_state_dict(active_page, self.state)
        state_dict.setdefault("media", {
            "path": None,
            "loop": True,
            "fps": 30
        })
        state_dict["media"]["path"] = internal_path
        # Save page
        active_page.save()
        key_index = self.key_grid.deck_controller.coords_to_index(self.coords)
        self.key_grid.deck_controller.load_key(key_index, page=active_page)

        # Update icon selector if current key is selected
        if gl.app is None:
            return
        active_identifier = gl.app.main_win.sidebar.active_identifier
        if active_identifier == self.identifier:
            gl.app.main_win.sidebar.key_editor.icon_selector.load_for_identifier(self.identifier, self.get_key().state)

        
    def on_drag_begin(self, drag_source, data):
        content = data.get_content()

    def on_drag_prepare(self, drag_source, x, y):
        drag_source.set_icon(self.image.get_paintable(), self.get_width() // 2, self.get_height() // 2)
        content = Gdk.ContentProvider.new_for_value(self)
        return content

    def on_dnd_accept(self, drop, user_data):
        return True

        

    def set_image(self, image):
        # Callable from any thread. This is the map-time replay path. A live
        # frame arrives through the UI adapter, which calls the same two
        # halves and coalesces the paints into one per input. The idle takes
        # the default priority, because a high-priority pixbuf update on every
        # frame starves the layout and draw of the main loop.
        GLib.idle_add(self.paint_mirror_frame, self.prepare_mirror_frame(image))
        # image.close()
        # image = None
        # del image

    def prepare_mirror_frame(self, image):
        """The paint-ready payload for paint_mirror_frame.

        Any thread may call it. image2pixbuf uses only PIL and GdkPixbuf, so
        the conversion runs on the caller, which is the media thread for a
        live frame, and only the widget change needs the loop.

        This carries no staleness stamp, unlike the screenbar. One slot
        coalesces the live frames of a key, so they cannot queue out of order,
        and the only other producer is the map-time replay, which dispatches
        in attach order like every idle. An inversion between the two costs
        one stale frame, and the next repaint corrects it.
        """
        return image2pixbuf(image.convert("RGBA"), force_transparency=True)

    def paint_mirror_frame(self, pixbuf) -> bool:
        # Main loop only. It returns False, because a GLib idle callback that
        # returns a true value re-arms.
        self.pixbuf = pixbuf
        # update righthand side key preview if possible - before the paint
        # below, which bails out when this button is unmapped
        self.set_icon_selector_previews(pixbuf)
        # Skip if the button was unmapped between queuing and running this
        # callback: painting a disposed widget crashes GTK.
        try:
            if not self.get_mapped():
                # This is a late failure. push_input_image already returned
                # True for this frame, so the engine did not dirty-mark it.
                # Record the drop here, or load_from_changes has nothing to
                # replay on the remap and the preview goes stale.
                self._mark_dropped()
                return False
            self.image.set_from_pixbuf(self.pixbuf)
        except Exception as e:
            log.debug(f"Key mirror paint skipped: {e}")
            self._mark_dropped()
        return False

    def _mark_dropped(self) -> None:
        controller = getattr(self.key_grid, "deck_controller", None)
        if controller is not None:
            mark_dirty(controller, self.identifier)

    def set_icon_selector_previews(self, pixbuf):
        # Main loop only: the gating below reads widget state.
        if not recursive_hasattr(gl, "app.main_win.sidebar"):
            return
        sidebar = gl.app.main_win.sidebar
        if pixbuf is None:
            return
        if sidebar.key_editor.label_editor.label_group.expander.active_identifier != self.identifier:
            return
        child = gl.app.main_win.leftArea.deck_stack.get_visible_child()
        if child is None:
            return
        if child.deck_controller != self.key_grid.deck_controller:
            return
        # Update icon selector on the top of the right are
        sidebar.key_editor.icon_selector.set_pixbuf_and_del(pixbuf)
        # Update icon selector in margin editor
        # sidebar.key_editor.image_editor.image_group.expander.margin_row.icon_selector.image.set_from_pixbuf(pixbuf)

    def on_click(self, gesture, n_press, x, y):
        if gesture.get_current_button() == 1 and n_press == 1:
            # Single left click
            # Select key
            self.image.grab_focus()

        elif gesture.get_current_button() == 1 and n_press == 2:
            # Double left click
            # Simulate key press
            self.simulate_press()
            
        elif gesture.get_current_button() == 3 and n_press == 1:
            # Single right click
            # Open context menu
            popover = KeyButtonContextMenu(self)
            popover.on_open()
            popover.popup()

    def simulate_press(self):
        ## Check if double click to emulate is turned on in the settings
        if not gl.settings_manager.app().emulate_at_double_click:
            return
        
        self.key_grid.deck_controller.event_callback(self.identifier, True)
        # Release key after 100ms
        GLib.timeout_add(100, self.key_grid.deck_controller.event_callback, self.identifier, False)

    def set_border_active(self, visible):
        if visible:
            # Hide other frames
            if self.key_grid.page_settings_page.deck_config.active_widget not in [self, None]:
                # self.key_grid.selected_key.set_css_classes(["key-button-frame-hidden"])
                self.key_grid.page_settings_page.deck_config.active_widget.set_border_active(False)
            self.key_grid.page_settings_page.deck_config.active_widget = self
            self.set_css_classes(["key-button-frame"])
            self.key_grid.selected_key = self
        else:
            self.set_css_classes(["key-button-frame-hidden"])
            self.key_grid.page_settings_page.deck_config.active_widget = None

    def on_focus_in(self, *args):
        # Update settings on the righthand side of the screen
        self.update_sidebar()
        # Update preview
        if self.pixbuf is not None:
            self.set_icon_selector_previews(self.pixbuf)
        # self.set_css_classes(["key-button-frame"])
        # self.button.set_css_classes(["key-button-new-small"])
        self.set_border_active(True)

    def update_sidebar(self):
        if not recursive_hasattr(gl, "app.main_win.sidebar"):
            return
        sidebar = gl.app.main_win.sidebar
        # Check if already loaded for this coords
        if sidebar.active_identifier == self.identifier:
            if not self.get_mapped():
                return
            
        sidebar.load_for_identifier(self.identifier, self.state)

    # Modifier
    def on_copy(self, *args):
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return
        key_dict = active_page.dict.get(self.identifier.input_type, {}).get(self.identifier.json_identifier, {})
        gl.app.main_win.key_dict = key_dict
        content = Gdk.ContentProvider.new_for_value(key_dict)
        gl.app.main_win.key_clipboard.set_content(content)

    def on_cut(self, *args):
        self.on_copy()
        self.on_remove()

    def on_paste(self, *args):
        # Check if clipboard is from this Deckard instance
        if not gl.app.main_win.key_clipboard.is_local() and False:  # TODO: Rely on system keyboard - Enabling this will cause copy/paste problems on KDE/Wayland
            #TODO: Use read_value_async to read it instead - This is more like a temporary hack
            return
        
        # Remove the old action objects. Several actions can share one action
        # base, and nothing else tells those actions apart.
        self.on_remove()
        
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return
        active_page.dict.setdefault(self.identifier.input_type, {})
        active_page.dict[self.identifier.input_type].setdefault(self.identifier.json_identifier, {})
        active_page.dict[self.identifier.input_type][self.identifier.json_identifier] = gl.app.main_win.key_dict
        active_page.reload_similar_pages(self.identifier, reload_self=True)

        # Reload ui
        gl.app.main_win.sidebar.load_for_identifier(self.identifier, self.get_key().state)

    def on_paste_finished(self, result, data, user_data):
        value = gl.app.main_win.key_clipboard.read_value_finish(result=data)

    def on_remove(self, *args):
        active_page = self.key_grid.deck_controller.active_page
        if active_page is None:
            return
        x, y = self.coords
        
        if f"{x}x{y}" not in active_page.dict.get("keys", {}):
            return
        del active_page.dict["keys"][f"{x}x{y}"]
        active_page.save()
        active_page.load()

        # Remove media from key
        active_page.reload_similar_pages(self.identifier, reload_self=True)

        # Reload ui
        gl.app.main_win.sidebar.load_for_identifier(self.identifier, self.get_key().state)

    def get_key(self) -> "ControllerKey":
        controller = self.key_grid.deck_controller

        return controller.get_input(self.identifier)

    def remove_media(self) -> None:
        key = self.get_key()
        state = key.get_active_state()

        state.remove_media()

    def _set_visible(self, visible: bool):
        self.set_visible(visible)
        self.image.set_visible(visible)

    def init_actions(self):
        self.action_group = Gio.SimpleActionGroup()
        self.insert_action_group("key", self.action_group)

        self.copy_action = Gio.SimpleAction.new("copy", None)
        self.cut_action = Gio.SimpleAction.new("cut", None)
        self.paste_action = Gio.SimpleAction.new("paste", None)
        self.remove_action = Gio.SimpleAction.new("remove", None)

        self.copy_action.connect("activate", self.on_copy)
        self.cut_action.connect("activate", self.on_cut)
        self.paste_action.connect("activate", self.on_paste)
        self.remove_action.connect("activate", self.on_remove)

        self.action_group.add_action(self.copy_action)
        self.action_group.add_action(self.cut_action)
        self.action_group.add_action(self.paste_action)
        self.action_group.add_action(self.remove_action)


    def init_shortcuts(self):
        self.shortcut_controller = Gtk.ShortcutController()

        self.copy_shortcut_action = Gtk.CallbackAction.new(self.on_copy)
        self.cut_shortcut_action = Gtk.CallbackAction.new(self.on_cut)
        self.paste_shortcut_action = Gtk.CallbackAction.new(self.on_paste)
        self.remove_shortcut_action = Gtk.CallbackAction.new(self.on_remove)

        self.copy_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Primary>c"), self.copy_shortcut_action)
        self.cut_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Primary>x"), self.cut_shortcut_action)
        self.paste_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Primary>v"), self.paste_shortcut_action)
        self.remove_shortcut = Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Delete"), self.remove_shortcut_action)

        self.shortcut_controller.add_shortcut(self.copy_shortcut)
        self.shortcut_controller.add_shortcut(self.cut_shortcut)
        self.shortcut_controller.add_shortcut(self.paste_shortcut)
        self.shortcut_controller.add_shortcut(self.remove_shortcut)

        self.add_controller(self.shortcut_controller)

    def on_update(self, *args, **kwargs):
        pass


class KeyButtonContextMenu(Gtk.PopoverMenu):
    def __init__(self, key_button:KeyButton, **kwargs):
        super().__init__(**kwargs)
        self.key_button = key_button
        self._unparenting = False
        self.build()

        self.connect("closed", self.on_close)

        # gl.app.set_accels_for_action("context.test", ["<Primary>t"])

    def on_test(self, *args, **kwargs):
        pass

    def build(self):
        self.set_parent(self.key_button)
        self.set_has_arrow(False)

        self.main_menu = Gio.Menu.new()

        self.copy_paste_menu = Gio.Menu.new()
        self.remove_menu = Gio.Menu.new()

        # Add actions to menus
        self.copy_paste_menu.append("Copy", "key.copy")
        self.copy_paste_menu.append("Cut", "key.cut")
        self.copy_paste_menu.append("Paste", "key.paste")
        self.remove_menu.append("Remove", "key.remove")
        self.remove_menu.append("Update", "key.update")

        # Add sections to menu
        self.main_menu.append_section(None, self.copy_paste_menu)
        self.main_menu.append_section(None, self.remove_menu)

        self.set_menu_model(self.main_menu)

    def on_close(self, *args, **kwargs):
        # Unparent on an idle, not here. This code runs inside the closed
        # signal emission, and an unparent of the popover during that emission
        # can dispose the emitter under GTK. The idle also lets fast repeated
        # right-clicks each schedule their own unparent without a race, and
        # the guard queues one per menu.
        if self._unparenting:
            return
        self._unparenting = True

        def _do_unparent():
            if self.get_parent() is not None:
                self.unparent()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_do_unparent)

    def on_open(self, *args, **kwargs):
        return
        gl.app.main_win.add_accel_actions()