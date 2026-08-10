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

from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier
from src.windows.mainWindow.elements.PageSelector import PageSelector
from src.windows.mainWindow.elements.Sidebar.elements.StateSwitcher import StateSwitcher
from src.windows.mainWindow.elements.Sidebar.elements.ScreenEditor import ScreenEditor


gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

# Import Python modules
from loguru import logger as log

# Import own modules
from src.windows.mainWindow.elements.Sidebar.elements.IconSelector import IconSelector
from src.windows.mainWindow.elements.Sidebar.elements.LabelEditor import LabelEditor
from src.windows.mainWindow.elements.Sidebar.elements.ActionManager import ActionManager
from src.windows.mainWindow.elements.Sidebar.elements.ActionChooser import ActionChooser
from src.windows.mainWindow.elements.Sidebar.elements.ActionConfigurator import ActionConfigurator
from src.windows.mainWindow.elements.Sidebar.elements.BackgroundEditor import BackgroundEditor
from src.windows.mainWindow.elements.Sidebar.elements.ImageEditor import ImageEditor
from GtkHelper.GtkHelper import ErrorPage

# Import globals
import globals as gl

class Sidebar(Adw.NavigationPage):
    def __init__(self, main_window, **kwargs):
        super().__init__(hexpand=True, title="Sidebar", **kwargs)
        self.main_window = main_window
        self.active_identifier: InputIdentifier = None
        self.active_state: int = None
        
        """
        To save performance and memory, we only load the thumbnail when the user sees the row
        """
        self.on_map_tasks: list = []
        self.connect("map", self.on_map)

        self.build()

    def on_map(self, widget):
        for f in self.on_map_tasks:
            f()
        self.on_map_tasks.clear()

    def build(self):
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.set_child(self.main_box)

        self.header = Adw.HeaderBar(css_classes=["flat"], show_back_button=False)
        self.main_box.append(self.header)

        self.main_stack = Gtk.Stack(transition_duration=200, transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.main_box.append(self.main_stack)

        self.configurator_stack = Gtk.Stack()
        self.main_stack.add_named(self.configurator_stack, "configurator_stack")

        self.key_editor = KeyEditor(self)
        self.configurator_stack.add_named(self.key_editor, "key_editor")

        self.screen_editor = ScreenEditor(self)
        self.configurator_stack.add_named(self.screen_editor, "screen_editor")

        self.action_chooser = ActionChooser(self)
        self.main_stack.add_named(self.action_chooser, "action_chooser")

        self.action_configurator = ActionConfigurator(self)
        self.main_stack.add_named(self.action_configurator, "action_configurator")

        self.error_page = ErrorPage(self)
        self.main_stack.add_named(self.error_page, "error_page")

        self.page_selector = PageSelector(self.main_window, gl.page_manager, halign=Gtk.Align.CENTER)
        self.header.set_title_widget(self.page_selector)

        self.load_for_identifier(Input.Key("0x0"), 0)

    def let_user_select_action(self, callback_function, identifier: InputIdentifier, *callback_args, **callback_kwargs):
        """
        Show the action chooser to let the user select an action.
        The callback_function will be called with the following parameters:
             - action_object: The action object that was selected.
             - args: The args passed to this function
             - kwargs: The kwargs passed to this function

        Parameters:
            callback_function (function): The callback function to be called after the action is selected.
            *callback_args: Variable length argument list to be passed to the callback function.
            **callback_kwargs: Arbitrary keyword arguments to be passed to the callback function.

        Returns:
            None
        """
        self.action_chooser.show(callback_function=callback_function,
                                 current_stack_page=self.main_stack.get_visible_child(),
                                 identifier=identifier,
                                 callback_args=callback_args,
                                 callback_kwargs=callback_kwargs)

    def show_action_configurator(self):
        self.main_stack.set_visible_child(self.action_configurator)

    def load_for_key(self, identifier: Input.Key, state: int):
        if not isinstance(identifier, Input.Key):
            raise ValueError
        self.active_identifier = identifier
        self.active_state = state

        self.main_stack.set_visible_child(self.configurator_stack)
        self.configurator_stack.set_visible_child(self.key_editor)
        self.key_editor.state_switcher.select_state(state)
        if not self.get_mapped():
            self.on_map_tasks.clear()
            self.on_map_tasks.append(lambda: self.load_for_key(identifier, state))
            return
        # Verify that a controller is selected
        if self.main_window.leftArea.deck_stack.get_visible_child() is None:
            self.error_page.set_error_text(gl.lm.get("right-area-no-deck-selected-error"))
            # self.error_page.set_reload_func(self.main_window.sidebar.load_for_coords)
            # self.error_page.set_reload_args([coords])
            self.show_error()
            return
        if gl.app is None:
            return
        # Verify is page is loaded on current controller
        visible_child = gl.app.main_win.leftArea.deck_stack.get_visible_child()
        if visible_child is None:
            return
        controller = visible_child.deck_controller
        if controller is None:
            return
        if controller.active_page is None:
            # self.error_page.set_error_text(gl.lm.get("right-area-no-page-selected-error"))
            # self.error_page.set_reload_args([None])
            #FIXME: User is unable to change or create pages when the error is shown
            self.show_error()
            return

        self.hide_error()

        self.key_editor.load_for_identifier(identifier, state)

    def load_for_dial(self, identifier: Input.Dial, state: int):
        self.active_identifier = identifier
        self.active_state = state
        self.main_stack.set_visible_child(self.configurator_stack)
        self.configurator_stack.set_visible_child(self.key_editor)
        self.key_editor.load_for_identifier(identifier, state)

    def load_for_touchscreen(self, identifier: Input.Touchscreen, state: int):
        self.active_identifier = identifier
        self.active_state = state
        self.main_stack.set_visible_child(self.configurator_stack)
        self.configurator_stack.set_visible_child(self.screen_editor)
        self.screen_editor.load_for_identifier(identifier, state)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        if isinstance(identifier, Input.Key):
            self.load_for_key(identifier, state)
        elif isinstance(identifier, Input.Dial):
            self.load_for_dial(identifier, state)
        elif isinstance(identifier, Input.Touchscreen):
            self.load_for_touchscreen(identifier, state)

    def show_error(self):
        if self.main_stack.get_visible_child() == self.error_page:
            return
        
        self.main_stack.set_transition_duration(0)
        self.main_stack.set_visible_child(self.error_page)
        self.main_stack.set_transition_duration(200)


    def hide_error(self):
        if self.main_stack.get_visible_child() != self.error_page:
            return

        self.main_stack.set_transition_duration(0)
        # key_editor is a child of configurator_stack, not of main_stack --
        # targeting it here was a GTK-warning no-op that left the error page
        # up. configurator_stack still shows whichever editor was last
        # selected inside it.
        self.main_stack.set_visible_child(self.configurator_stack)
        self.main_stack.set_transition_duration(200)

    def update(self):
        identifier = self.active_identifier
        state = self.active_state
        # Refresh follows the input's OWN current state: the remembered
        # active_state can belong to a previous page's input (page changes
        # keep the sidebar selection), and replaying it would repaint the
        # device from a UI-refresh path (KeyEditor.load_for_identifier calls
        # c_input.set_state) and ERROR-spam whenever the new page's input
        # has fewer states. User-driven state selection still passes its
        # state explicitly through load_for_*.
        controller = self.main_window.get_active_controller()
        if controller is not None and identifier is not None:
            c_input = controller.get_input(identifier)
            if c_input is not None:
                state = c_input.state
                self.active_state = state
        self.load_for_identifier(identifier, state)


class KeyEditor(Gtk.Box):
    def __init__(self, sidebar: Sidebar, **kwargs):
        self.sidebar:Sidebar = sidebar
        super().__init__(**kwargs)
        self.set_orientation(Gtk.Orientation.VERTICAL)

        self.scrolled_window = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.append(self.scrolled_window)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.scrolled_window.set_child(self.main_box)

        self.state_switcher = StateSwitcher("keys", margin_start=20, margin_end=20, margin_top=10, margin_bottom=10, hexpand=True)
        self.state_switcher.add_switch_callback(self.on_state_switch)
        self.state_switcher.add_add_new_callback(self.on_add_new_state)
        self.state_switcher.set_n_states(0)
        self.main_box.append(self.state_switcher)

        self.icon_selector = IconSelector(sidebar, halign=Gtk.Align.CENTER, margin_top=30)
        self.main_box.append(self.icon_selector)

        self.image_editor = ImageEditor(sidebar, margin_top=90)
        self.main_box.append(self.image_editor)

        self.background_editor = BackgroundEditor(sidebar, margin_top=25)
        self.main_box.append(self.background_editor)

        self.label_editor = LabelEditor(sidebar, margin_top=25)
        self.main_box.append(self.label_editor)

        self.action_editor = ActionManager(sidebar, margin_top=25, width_request=400)
        self.main_box.append(self.action_editor)

        self.remove_state_button = Gtk.Button(label="Remove State", css_classes=["destructive-action"], margin_top=15, margin_bottom=15, margin_start=15, margin_end=15)
        self.remove_state_button.connect("clicked", self.on_remove_state)
        self.append(self.remove_state_button)

    def on_state_switch(self, *args):
        state = self.state_switcher.get_selected_state()

        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        
        controller_input = controller.get_input(self.sidebar.active_identifier)
        log.info(f"Going to state {state} from {controller_input.state}")
        controller_input.set_state(state=state, update_sidebar=True)

    def on_add_new_state(self, state):
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        
        c_input = controller.get_input(self.sidebar.active_identifier)
        c_input.add_new_state()

        self.remove_state_button.set_visible(self.state_switcher.get_n_states() > 1)

    def on_remove_state(self, button):
        if self.state_switcher.get_n_states() <= 1:
            return

        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        
        active_state = self.state_switcher.get_selected_state()
        
        c_input = controller.get_input(self.sidebar.active_identifier)
        c_input.remove_state(active_state)

        self.remove_state_button.set_visible(self.state_switcher.get_n_states() > 1)

    def load_for_identifier(self, identifier: InputIdentifier, state: int):
        self.sidebar.active_identifier = identifier

        if gl.app is None:
            return
        controller = gl.app.main_win.get_active_controller()
        if controller is None:
            return
        
        c_input = controller.get_input(identifier)

        self.state_switcher.load_for_identifier(identifier, state)
        c_input.set_state(state)

        self.remove_state_button.set_visible(self.state_switcher.get_n_states() > 1)

        self.icon_selector.load_for_identifier(identifier, state)
        self.image_editor.load_for_identifier(identifier, state)
        self.label_editor.load_for_identifier(identifier, state)
        self.action_editor.load_for_identifier(identifier, state)
        self.background_editor.load_for_identifier(identifier, state)
