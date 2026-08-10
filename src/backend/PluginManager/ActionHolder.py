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
from copy import deepcopy

# Import own modules
from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.PageManagement.Page import Page
from src.backend.DeckManagement.deck_controller.controller import DeckController
from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier

# Import typing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.PluginManager.PluginBase import PluginBase

# Import gtk
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from packaging import version

import globals as gl

from loguru import logger as log

class ActionHolder:
    """
    Holder for ActionCore containing important information that can be used as long as the ActionCore is not initialized
    """
    def __init__(self,
        plugin_base: "PluginBase",
        action_name: str,
        action_core: type[ActionCore] = None,
        action_base: type[ActionBase] = None,
        icon: Gtk.Widget = None,
        min_app_version: str = None,
        action_id: str = None,
        action_id_suffix: str = None,
        action_support: dict[type[InputIdentifier], ActionInputSupport] = None,
        *args, **kwargs):

        if action_support is None:
            action_support = {
                Input.Key: ActionInputSupport.UNTESTED,
                Input.Dial: ActionInputSupport.UNTESTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED
            }

        ## Verify variables
        if action_name in ["", None]:
            raise ValueError("Please specify an action name")
        if action_id in ["", None] and action_id_suffix in ["", None]:
            raise ValueError("Please specify an action id or an action id suffix")
        
        if icon is None:
            # ActionHolders are built in plugin __init__, which runs on a
            # store worker thread on the install path. GTK4 is
            # main-thread-only -- off-main widget construction is the
            # segfault/abort class -- so the default icon is marshalled onto
            # the main loop. Inline (zero-cost) on the normal startup path,
            # which already runs on main.
            from src.backend.main_loop import run_on_main
            icon = run_on_main(lambda: Gtk.Image(icon_name="insert-image-symbolic"))

        self.plugin_base = plugin_base
        self.action_core = action_core if action_core else action_base #backwards compatibility
        self.action_id_suffix = action_id_suffix
        self.action_id = action_id or f"{plugin_base.get_plugin_id()}::{action_id_suffix}"
        self.action_name = action_name
        self.icon = icon
        self.min_app_version = min_app_version
        self.action_support = deepcopy(action_support)
        
    def get_is_compatible(self) -> bool:
        if self.min_app_version is not None:
            if version.parse(gl.app_version) < version.parse(self.min_app_version):
                return False
            
        return True

    @log.catch
    def init_and_get_action(self, deck_controller: DeckController, page: Page, state: int, input_ident: InputIdentifier) -> ActionCore | None:
        if not self.get_is_compatible():
            return None

        if self.action_core is None:
            # Neither action_core nor action_base was given to __init__; the
            # call below used to raise TypeError into @log.catch, which
            # swallowed it and returned None anyway.
            log.error(f"Action holder {self.action_id} has no action class")
            return None

        return self.action_core(
            action_id=self.action_id,
            action_name=self.action_name,
            deck_controller=deck_controller,
            page=page,
            input_ident=input_ident,
            plugin_base=self.plugin_base,
            state=state
        )
    
    def get_input_compatibility(self, identifier: InputIdentifier) -> ActionInputSupport:
        return self.action_support.get(type(identifier), ActionInputSupport.UNSUPPORTED)