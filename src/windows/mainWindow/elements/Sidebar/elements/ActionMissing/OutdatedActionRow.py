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

from src.backend.DeckManagement.InputIdentifier import InputIdentifier
from src.windows.mainWindow.elements.Sidebar.elements.ActionMissing.MissingRow import MissingRow

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")


class OutdatedActionRow(MissingRow):
    # The signature matches the sibling MissingActionButtonRow, and the call
    # site in ActionManager.load_for_actions. A signature of the form (index,
    # state, coords, dial, touch) predates InputIdentifier and passes coords
    # to a parent that has no such parameter, which raises TypeError for every
    # outdated action.
    def __init__(self, action_id:str, identifier: InputIdentifier, state:int, index: int):
        super().__init__(
            action_id=action_id,
            identifier=identifier,
            index=index,
            state=state,
            install_label="Update outdated plugin",
            install_failed_label="Update failed",
            installing_label="Updating..."
        )