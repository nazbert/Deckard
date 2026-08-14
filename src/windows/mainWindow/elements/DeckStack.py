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
from src.backend import ui_port
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

        self.deck_names: list[str] = []
        self.deck_numbers: list[str] = []

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

        # Clear the earlier binding before the construction of the new child.
        # KeyGrid.__init__ calls load_from_changes() during the construction,
        # and its touchscreen branch replays the dirty markers into whatever
        # screenbar it resolves. On a window rebuild a stale binding feeds
        # those markers to dead widgets, and the new screenbar has nothing to
        # replay on the map. Without a binding the replay defers, and the
        # markers survive for the new widgets.
        #
        # The lookup is duck-typed and not an isinstance(GtkUIAdapter) test. A
        # wrapper port, such as a recording port in a test or an IPC
        # forwarder, implements the same bind and unbind pair without
        # inheriting, and an isinstance gate drops every binding for it.
        adapter = ui_port.get()
        unbind = getattr(adapter, "unbind", None)
        if callable(unbind):
            unbind(deck_controller)
        page = DeckStackChild(self, deck_controller)
        self.add_titled(page, deck_number, deck_type)
        # Bind by reference, and only after the child reaches the stack. A
        # lookup by stack-child name reads the serial from the device again,
        # and it misses for good when either read is wrong, which USB
        # contention at boot causes, or when the window rebuilds. A bind after
        # add_titled also keeps an exception during the construction from
        # leaving the controller bound to a child that is not in the stack.
        bind = getattr(adapter, "bind", None)
        if callable(bind):
            bind(deck_controller, page)

        page.page_settings.deck_config.grid.select_key(0, 0)

        self.main_window.change_ui_to_connected_deck()

        self.main_window.reload_sidebar()
            
    def get_page_attributes(self, deck_controller) -> tuple | None:
        if deck_controller in self.deck_attributes:
            return self.deck_attributes[deck_controller]
        
        deck_type = deck_controller.deck.deck_type()
        try:
            # Use the cached accessor of the controller, not a fresh device
            # read. This string becomes the stack-child name, and every reader
            # must see one value, even when a later device read differs.
            serial_number = deck_controller.serial_number()
        except Exception as e:
            log.error(e)
            return None
        self.deck_numbers.append(serial_number)
        deck_number = str(serial_number)

        if deck_type not in self.deck_names:
            self.deck_names.append(deck_type)
            self.deck_attributes[deck_controller] = deck_number, deck_type
            return deck_number, deck_type
        # The name exists, so add a "(n)" suffix. Never change the digits in
        # the model name, because that turns a second "Stream Deck MK.2" into
        # "Stream Deck MK.3".
        base_type = deck_type
        suffix = 2
        while deck_type in self.deck_names:
            deck_type = f"{base_type} ({suffix})"
            suffix += 1

        self.deck_names.append(deck_type)

        self.deck_attributes[deck_controller] = deck_number, deck_type

        return deck_number, deck_type

    def remove_page(self, deck_controller) -> None:
        adapter = ui_port.get()
        unbind = getattr(adapter, "unbind", None)
        if callable(unbind):
            unbind(deck_controller)

        was_visible: bool = False
        for i, page in enumerate(self.get_pages()):  # type: ignore[arg-type, var-annotated]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that
            if page.get_child().deck_controller == deck_controller:
                if self.get_visible_child() == page.get_child():
                    was_visible = True
                # Remove from deck_names
                self.deck_names.remove(page.get_title())
                # Remove page from stack
                self.remove(page.get_child())
                break

        # Drop the cached attributes of the controller, whether or not it was
        # visible. deck_attributes is keyed by the controller object, so an
        # entry that stays keeps the dead controller reachable, and each
        # unplug and replug adds one more stale entry.
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
        if len(pages) == 0:  # type: ignore[arg-type]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that
            self.main_window.change_ui_to_no_connected_deck()
            return

        self.set_visible_child(pages[0].get_child())  # type: ignore[index]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that

    def focus_controller(self, deck_controller) -> None:
        for page in self.get_pages():  # type: ignore[attr-defined]  # gi stub: PyGObject's Gio.ListModel override makes the returned SelectionModel iterable/sized/indexable; gi-stubs declares none of that
            if page.get_child().deck_controller == deck_controller:
                self.set_visible_child(page.get_child())
                return
            
    def get_visible_child(self) -> DeckStackChild | None:
        # None while the stack is empty (no deck connected yet).
        return super().get_visible_child()  # type: ignore[return-value]  # gi stub: Gtk.Stack.get_visible_child is typed Gtk.Widget | None; every child of this stack is a DeckStackChild