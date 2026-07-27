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
from gi.repository import Gtk

# Import Python modules 
from loguru import logger as log

# Import globals

# Import own modules
from src.windows.mainWindow.elements.DeckStackChild import DeckStackChild

# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.windows.mainWindow.elements.leftArea import LeftArea
    from src.windows.mainWindow.mainWindow import MainWindow

class DeckStack(Gtk.Stack):
    """
    A deck with childs for each connected deck
    """
    def __init__(self, main_window: "MainWindow", left_area: "LeftArea", deck_manager, **kwargs):
        super().__init__(**kwargs)
        self.deck_manager = deck_manager
        self.main_window = main_window
        self.left_area = left_area

        self.deck_names = []
        self.deck_numbers = []

        self.deck_attributes: dict = {}

    def on_switch(self, widget, *args):
        # Update page selector
        self.main_window.sidebar.page_selector.update_selected()
        self.main_window.deck_settings_button.update_state()

        child: DeckStackChild = self.get_visible_child()

    def build(self):
        self.connect("notify::visible-child-name", self.on_switch)

    def add_pages(self):
        for deck_controller in self.deck_manager.deck_controller:
            self.add_page(deck_controller)

        if len(self.deck_manager.deck_controller) == 0:
            self.main_window.change_ui_to_no_connected_deck()

    def add_page(self, deck_controller):
        attr = self.get_page_attributes(deck_controller)
        if attr is None:
            return
        deck_number, deck_type = attr

        # Clear any previous binding BEFORE constructing the new child
        # (issue #156): KeyGrid.__init__ runs load_from_changes() during
        # construction, and its touchscreen branch replays dirty markers
        # against deck_controller.own_deck_stack_child -- on a window
        # rebuild that still points at the ORPHANED old child, which would
        # consume the markers into dead widgets and leave the new screenbar
        # with nothing to replay on map. Unbound, that replay defers and
        # the markers survive for the new widgets.
        deck_controller.own_deck_stack_child = None
        deck_controller.own_key_grid = None
        page = DeckStackChild(self, deck_controller)
        self.add_titled(page, deck_number, deck_type)
        # Bind by reference only once the child is actually in the stack:
        # resolving by stack-child NAME re-read the serial from the device
        # and silently missed forever if either read was wrong (USB
        # contention at boot) or the window was rebuilt. Binding after
        # add_titled also means an exception mid-construction can never
        # leave the controller bound to a child that is not in the stack.
        deck_controller.own_deck_stack_child = page

        page.page_settings.deck_config.grid.select_key(0, 0)

        self.main_window.change_ui_to_connected_deck()

        self.main_window.reload_sidebar()
            
    def get_page_attributes(self, deck_controller) -> tuple:
        if deck_controller in self.deck_attributes:
            return self.deck_attributes[deck_controller]
        
        deck_type = deck_controller.deck.deck_type()
        try:
            # The controller's cached accessor, not a fresh device read: this
            # string becomes the stack-child name, and every consumer must
            # agree on one value even if a later device read would differ
            # (issue #156).
            serial_number = deck_controller.serial_number()
        except Exception as e:
            log.error(e)
            return
        self.deck_numbers.append(serial_number)
        deck_number = str(serial_number)

        if deck_type not in self.deck_names:
            self.deck_names.append(deck_type)
            self.deck_attributes[deck_controller] = deck_number, deck_type
            return deck_number, deck_type
        # Name already exists: disambiguate with a "(n)" suffix. Never mutate
        # the model name's own digits -- that turned a second
        # "Stream Deck MK.2" into "Stream Deck MK.3".
        base_type = deck_type
        suffix = 2
        while deck_type in self.deck_names:
            deck_type = f"{base_type} ({suffix})"
            suffix += 1

        self.deck_names.append(deck_type)

        self.deck_attributes[deck_controller] = deck_number, deck_type

        return deck_number, deck_type

    def remove_page(self, deck_controller) -> str:
        was_visible: bool = False
        for i, page in enumerate(self.get_pages()):
            if page.get_child().deck_controller == deck_controller:
                if self.get_visible_child() == page.get_child():
                    was_visible = True
                # Remove from deck_names
                self.deck_names.remove(page.get_title())
                # Remove page from stack
                self.remove(page.get_child())
                break

        # Purge the controller's cached attributes regardless of whether it
        # was ever visible (plan P1.3/design doc bug 2): left in place, these
        # kept the dead controller reachable (deck_attributes is keyed by the
        # controller object itself) and grew one stale entry per unplug/
        # replug forever.
        attr = self.deck_attributes.pop(deck_controller, None)
        if attr is not None:
            deck_number, _deck_type = attr
            if deck_number in self.deck_numbers:
                self.deck_numbers.remove(deck_number)

        if not was_visible:
            return
        
        # Reload righ area
        self.main_window.reload_sidebar()
            
        pages = self.get_pages()
        # Show message if no decks are connected
        if len(pages) == 0:
            self.main_window.change_ui_to_no_connected_deck()
            return

        self.set_visible_child(pages[0].get_child())

    def focus_controller(self, deck_controller) -> None:
        for page in self.get_pages():
            if page.get_child().deck_controller == deck_controller:
                self.set_visible_child(page.get_child())
                return
            
    def get_visible_child(self) -> DeckStackChild:
        return super().get_visible_child()