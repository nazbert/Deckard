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
import os
import threading
import time

import globals as gl

from loguru import logger as log
from contextlib import contextmanager
from copy import copy

from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.PageManagement import page_document, page_flush
import globals as gl

from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.DeckManagement.InputIdentifier import Input, InputEvent, InputIdentifier
from typing import Any, Iterator, TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput, ControllerInputState
    from src.backend.DeckManagement.deck_controller.label_engine import LabelManager
    from src.backend.PluginManager.ActionHolder import ActionHolder


# Inside the body of Page the name dict is the page-content property, so an
# annotation there reaches the builtin through this alias.
_Dict = dict


class Page:
    def __init__(self, json_path, deck_controller, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.json_path = json_path
        self.deck_controller = deck_controller

        # The content of the page file, shared with every other Page on this
        # path. Bound before load() below, which is the first thing to read
        # through it.
        self._document = page_document.document_for(json_path)

        # The action objects, kept so a reload can reuse them.
        self.action_objects = {}

        # Serializes the on_ready_called claim in initialize_actions. That
        # method runs outside _load_page_lock, because it can block on a
        # run_on_main marshal. Two concurrent load_page calls for one page
        # otherwise both read the flag as False and submit a second on_ready.
        self._ready_claim_lock = threading.Lock()

        self.load(load_from_file=True) #TODO: Limit the load of action objects to the available inputs

    @property
    def dict(self) -> _Dict[str, Any]:
        """This page's content, straight from the document that owns it.

        There is no setter. Every Page on a path holds the one dict the
        document has, so an assignment here gives this Page a copy nobody else
        sees. Mutate what this returns. To replace the whole content, refresh
        the document.
        """
        return self._document.data

    def rebind_document(self, document: "page_document.PageDocument") -> None:
        """Read through document from now on.

        For a page rename, the one operation that changes which file a live
        Page belongs to. The rename re-points json_path in place and calls
        this, so a Page minted under the old name during the rename shares the
        content of every other Page of the renamed page.
        """
        self._document = document

    def get_name(self) -> str:
        return os.path.splitext(os.path.basename(self.json_path))[0]

    def update_dict(self) -> None:
        """Update the dict and leave the action objects alone.

        Do not call this after a change to the action objects. The refresh
        lands in the document, so every deck on this page gets it, and it
        overwrites an unsaved edit made through any of them. A mutator
        therefore saves in the call that mutates.
        """
        self._document.refresh_from_disk()

    def load(self, load_from_file: bool = False):
        start = time.time()
        if load_from_file:
            self.update_dict()
        self.load_action_objects()

        end = time.time()
        log.debug(f"Loaded page {self.get_name()} in {end - start:.2f} seconds")

    def save(self) -> None:
        # Record the edit and arm the write. The flush seam owns the per-path
        # lock, the write and its time. A trailing timer makes a burst of
        # edits cost one write. Every reader sees the in-memory change already, so
        # only the disk waits. A page switch, a deck close, a quit and a read
        # of the page file each ask the seam to write now.
        page_flush.get().mark_dirty(self)

    @contextmanager
    def edit(self) -> Iterator[_Dict[str, Any]]:
        """Change this page's content as one edit, and send it to the file.

        The mutation seam for a caller that holds a Page. Nothing inside the
        block may touch a page file or the GTK main loop.
        """
        # Changes that only make sense together, such as a swap of two keys,
        # go in one block. It holds the file's lock, so a write in flight
        # cannot snapshot the page halfway, and it marks the page once.
        #
        # That lock is the file's only lock and it is a leaf. A read of a page
        # (the read barrier takes the lock), a reload onto a deck, and a
        # marshal to the main thread each deadlock from in here. Mutate the
        # content, and reload after the block.
        with self._document.edit() as data:
            yield data

    def flush(self) -> str:
        """Write this page's file now, and return the path written.

        It is the counterpart to save(). A mutator marks and a boundary
        flushes. A boundary is the last chance the edits get: a deck that
        leaves this page, a deck that goes away, the app that quits, or a read
        of the file. It returns the path, because such a boundary names the
        file it made current, as the page switch does for plugins and DBus.
        """
        page_flush.get().flush_path(self.json_path)
        return self.json_path

    def make_backup(self, json_path: str | None = None):
        # The flush passes the path it holds the save lock for. That is this
        # page's own path except after a page move, which re-points json_path
        # while a write for the old path is still pending. A backup of any file
        # but the one about to be overwritten copies the wrong page.
        page_document.back_up_page_file(json_path if json_path is not None else self.json_path)

    def move_key_to_end(self, dictionary, key):
        # It operates on the passed dict, which is the flush's snapshot. A pop
        # and reinsert on the live self.dict mutates the page mid-save and
        # reorders a dict that no write reads.
        page_document.move_key_to_end(dictionary, key)

    def set_background(self, file_path):
        self.dict.setdefault("background", {})
        self.dict["background"]["path"] = file_path
        self.save()

    def load_action_objects(self):
        # The import is function-scoped, because deck_controller/controller.py
        # imports this module and a module-level import here closes a cycle.
        from src.backend.DeckManagement.deck_controller.controller import CONTROLLER_CLASSES

        new_action_objects = {}

        for input_type in Input.All:
            input_class = CONTROLLER_CLASSES[input_type]
            input_type_name = input_type.input_type
            for key in input_class.Available_Identifiers(self.deck_controller.deck):
                input_ident = Input.FromTypeIdentifier(input_type_name, key)
                for state in input_ident.get_states(self):
                    try:
                        # action_objects keys by int state and the page json
                        # keys by str. A state key that is not a number belongs
                        # in neither.
                        state = int(state)
                    except ValueError:
                        continue
                    for i, action in enumerate(input_ident.get_actions(self, state)):
                        if action.get("id") is None:
                            continue

                        action_object = self.get_new_action_object(
                            # loaded_action_objects=self.action_objects,
                            loaded_action_objects=self.action_objects,
                            action_id=action["id"],
                            state=state,
                            i=i,
                            input_ident=input_ident,
                        )
                        # input_action_objects[state][i] = action_object
                        new_action_objects.setdefault(input_type_name, {})
                        new_action_objects[input_type_name].setdefault(key, {})
                        new_action_objects[input_type_name][key].setdefault(state, {})
                        # new_action_objects[input_type][key][state].setdefault(i, {})
                        new_action_objects[input_type_name][key][state][i] = action_object

        old_actions = self.get_all_actions(self.action_objects)
        new_actions = self.get_all_actions(new_action_objects)

        for old_action in old_actions:
            if old_action not in new_actions:
                # The framework owns this teardown. It notifies and then
                # always calls clean_up(), so a plugin that overrides the hook
                # without super() cannot leak the dropped action.
                ActionCore.teardown(old_action)

        self.action_objects = new_action_objects

        if self.deck_controller.active_page == self:
            # The page is loaded already, so this covers new actions only.
            self.initialize_actions()

    # def load_action_object_sector(self, loaded_action_objects, dict_key: str, state)

    def get_new_action_object(self, loaded_action_objects: _Dict, action_id: str, state: int, i: int, input_ident):
        
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            # Only before create_global_objects(), where no holder resolves.
            return NoActionHolderFound(id=action_id, identifier=input_ident, state=state)

        action_holder = plugin_manager.get_action_holder_from_id(action_id)

        ## No action holder found
        if action_holder is None:
            plugin_id = plugin_manager.get_plugin_id_from_action_id(action_id)
            if plugin_manager.get_is_plugin_out_of_date(plugin_id):
                return ActionOutdated(id=action_id, identifier=input_ident, state=state)
            return NoActionHolderFound(id=action_id, identifier=input_ident, state=state)

        ## Keep old object if it exists
        old_action = loaded_action_objects.get(input_ident.input_type, {}).get(input_ident.json_identifier, {}).get(state, {}).get(i)
        if old_action is not None:
            # action_core holds the action class, but ActionHolder annotates
            # that attribute as an instance, so bind it locally before this
            # code uses it as a class.
            action_core_class: Any = action_holder.action_core
            if isinstance(old_action, action_core_class):
                return old_action #FIXME: never used
            
        ## Create new action object            
        action_object = action_holder.init_and_get_action(
            deck_controller=self.deck_controller,
            page=self,
            state=state,
            input_ident=input_ident,
        )
        return action_object

    def _load_action_objects(self):
        return
        # Store loaded action objects
        loaded_action_objects = copy(self.action_objects)

        add_threads: list[threading.Thread] = []

        # Load action objects
        self.action_objects = {}
        for input_type in Input.KeyTypes:
            for input_identifier in self.dict.get(input_type, {}):
                input_ident = Input.FromTypeIdentifier(input_type, input_identifier)
                for state in input_ident.get_states(self):
                    state = int(state)
                    if "actions" not in input_ident.get_state_dict(self, state):
                        continue
                    for i, action in enumerate(input_ident.get_actions(self, state)):
                        if action.get("id") is None:
                            continue

                        input_action_objects = input_ident.get_dict(self.action_objects)
                        input_action_objects.setdefault(state, {})

                        action_holder = gl.plugin_manager.get_action_holder_from_id(action["id"])
                        if action_holder is None:
                            plugin_id = gl.plugin_manager.get_plugin_id_from_action_id(action["id"])
                            if gl.plugin_manager.get_is_plugin_out_of_date(plugin_id):
                                input_action_objects[state][i] = ActionOutdated(id=action["id"], identifier=input_ident, state=state)
                            else:
                                input_action_objects[state][i] = NoActionHolderFound(id=action["id"], identifier=input_ident, state=state)
                            continue
                        action_class = action_holder.action_core
                        
                        if action_class is None:
                            input_action_objects[state][i] = NoActionHolderFound(id=action["id"], identifier=input_ident, state=state)
                            continue

                        old_action_object = input_ident.get_dict(loaded_action_objects)
                        old_object = old_action_object.get(state, {}).get(i)
                        
                        if i in old_action_object.get(state, {}):
                            # if isinstance(loaded_action_objects.get(key, {}).get(i), action_class):
                            if old_object is not None:
                                if isinstance(old_object, action_class):
                                    input_action_objects[state][i] = old_action_object[state][i]
                                    continue

                        # action_object = action_holder.init_and_get_action(deck_controller=self.deck_controller, page=self, coords=key)
                        # self.action_objects[key][i] = action_object
                        if type == "keys" and self.deck_controller.coords_to_index(key.split("x")) > self.deck_controller.deck.key_count():
                            continue
                        thread = threading.Thread(target=self.add_action_object_from_holder, args=(action_holder, input_ident, state, i), name=f"add_action_object_from_holder_{input_ident.json_identifier}_{state}_{i}")
                        thread.start()
                        add_threads.append(thread)

        for thread in add_threads:
            thread.join()

        all_old_objects: list[ActionCore] = []
        for type in loaded_action_objects:
            for key in loaded_action_objects[type]:
                for i in loaded_action_objects[type][key]:
                    all_old_objects.append(loaded_action_objects[type][key][i])

        all_action_objects: list[ActionCore] = []
        for type in self.action_objects:
            for key in self.action_objects[type]:
                for i in self.action_objects[type][key]:
                    all_action_objects.append(self.action_objects[type][key][i])

        for action in all_old_objects:
            if action not in all_action_objects:
                if isinstance(action, ActionCore):
                    action.on_removed_from_cache()
                    action.page = None
                del action

    def move_actions(self, type: str, from_key: str, to_key: str):
        from_actions = self.action_objects.get(type, {}).get(from_key, {})

        for raw_action in from_actions.values():
            action: "ActionCore" = raw_action
            if type == "keys":
                action.key_index = self.deck_controller.coords_to_index(to_key.split("x"))
            action.identifier = to_key

    def switch_actions_of_inputs(self, input_1: InputIdentifier, input_2: InputIdentifier):
        input_1_dict = self.action_objects.get(input_1.input_type, {}).get(input_1.json_identifier, {})
        input_2_dict = self.action_objects.get(input_2.input_type, {}).get(input_2.json_identifier, {})

        for state in input_1_dict:
            for action in input_1_dict[state].values():
                action.input_ident = input_2

        for state in input_2_dict:
            for action in input_2_dict[state].values():
                action.input_ident = input_1

        # Change in action_objects
        self.action_objects.setdefault(input_1.input_type, {})
        self.action_objects.setdefault(input_2.input_type, {})
        self.action_objects[input_1.input_type][input_1.json_identifier] = input_2_dict
        self.action_objects[input_2.input_type][input_2.json_identifier] = input_1_dict


    @log.catch
    def add_action_object_from_holder(self, action_holder: "ActionHolder", input_ident: "InputIdentifier", state: int, i: int):
        action_object = action_holder.init_and_get_action(deck_controller=self.deck_controller, page=self, input_ident=input_ident, state=state)
        if action_object is None:
            return
        self.action_objects.setdefault(input_ident.input_type, {})
        self.action_objects[input_ident.input_type].setdefault(input_ident.json_identifier, {})
        self.action_objects[input_ident.input_type][input_ident.json_identifier].setdefault(int(state), {})
        self.action_objects[input_ident.input_type][input_ident.json_identifier][int(state)][i] = action_object

    def remove_plugin_action_objects(self, plugin_id: str) -> bool:
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            return False

        plugin_obj = plugin_manager.get_plugin_by_id(plugin_id)
        if plugin_obj is None:
            return False

        # Collect first, then delete and tear down. A del of the local variable
        # does nothing to the object, so a plugin uninstall must call
        # ActionCore.teardown to reach clean_up().
        to_remove: list[tuple] = []
        for type in list(self.action_objects.keys()):
            for key in list(self.action_objects[type].keys()):
                for state in list(self.action_objects[type][key].keys()):
                    for index in list(self.action_objects[type][key][state].keys()):
                        action = self.action_objects[type][key][state][index]
                        if not isinstance(action, ActionCore):
                            continue
                        if action.plugin_base == plugin_obj:
                            to_remove.append((type, key, state, index, action))

        for type, key, state, index, action in to_remove:
            del self.action_objects[type][key][state][index]
            ActionCore.teardown(action)

        return True
    
    def update_inputs_with_actions_from_plugin(self, plugin_id: str):
        # plugin_obj = gl.plugin_manager.get_plugin_by_id(plugin_id)
        plugin_manager = gl.plugin_manager
        if plugin_manager is None:
            return

        for input_type in list(self.action_objects.keys()):
            for json_identifier in list(self.action_objects[input_type].keys()):
                for state in list(self.action_objects[input_type][json_identifier].keys()):
                    for index in list(self.action_objects[input_type][json_identifier][state].keys()):
                        action_core = self.action_objects[input_type][json_identifier][state][index]
                        action_id = action_core.action_id

                        if plugin_manager.get_plugin_id_from_action_id(action_id) == plugin_id:
                            identifier = Input.FromTypeIdentifier(input_type, json_identifier)

                            c_input = self.deck_controller.get_input(identifier)
                            if c_input.state == int(state):
                                c_input.update()
    
#    def get_keys_with_plugin(self, plugin_id: str):
#        plugin_obj = gl.plugin_manager.get_plugin_by_id(plugin_id)
#        if plugin_obj is None:
#            return []
#        
#        keys = []
#        for type in self.action_objects.values():
#            for key in self.action_objects[type]:
#                for state in self.action_objects[type][state]:
#                    for action in self.action_objects[type][state][key].values():
#                        if not isinstance(action, ActionCore):
#                            continue
#                        if action.plugin_base == plugin_obj:
#                            keys.append(key)
#
#        return keys

    def remove_plugin_actions_from_json(self, plugin_id: str):
        for type in Input.KeyTypes:
            # A page json can lack an input type. A non-Plus deck has no
            # touchscreens section.
            for key in self.dict.get(type, {}):
                for state in self.dict[type][key].get("states", {}):
                    actions = self.dict[type][key]["states"][state].get("actions", [])
                    # Collect the indices first. A delete from actions during
                    # the enumerate() walk skips the next entry.
                    to_remove = [
                        i for i, action in enumerate(actions)
                        # An action here is a plain dict of raw json, so it has
                        # no id attribute. The or "" catches an explicit
                        # "id": null, which passes a .get default.
                        if (action.get("id") or "").split("::")[0] == plugin_id
                    ]
                    for i in reversed(to_remove):
                        del actions[i]

        self.save()

    def get_without_action_objects(self):
        # The document owns the content and its file shape. The flush writes
        # pages that no deck shows, and those have no Page to ask.
        return self._document.get_without_action_objects()

    def get_all_actions(self, action_dict: _Dict = None):
        if action_dict is None:
            action_dict = self.action_objects
        actions = []
        for input_type in action_dict:
            for key in action_dict[input_type]:
                for state in action_dict[input_type][key]:
                    for action in action_dict[input_type][key][state].values():
                        if action is None:
                            continue
                        if not isinstance(action, ActionCore):
                            continue
                        actions.append(action)
        return actions
    
    def get_all_actions_for_type(self, ident, only_action_cores: bool = False):
        actions = []
        input_type = ident.input_type
        input_identifier = ident.json_identifier
        if input_identifier in self.action_objects.get(input_type, {}):
            for state in self.action_objects[input_type].get(input_identifier, {}):
                for action in self.action_objects[input_type][input_identifier].get(state, {}).values():
                    if action is None or not action:
                        continue
                    if only_action_cores and not isinstance(action, ActionCore):
                        continue
                    actions.append(action)
        return actions
    
    def get_all_actions_for_input(self, ident, state, only_action_cores: bool = False):
        actions = []
        input_type = ident.input_type
        json_identifier = ident.json_identifier
        if json_identifier in self.action_objects.get(input_type, {}):
            if state in self.action_objects[input_type].get(json_identifier, {}):
                for action in self.action_objects[input_type][json_identifier].get(state, {}).values():
                    if action is None or not action:
                        continue
                    if only_action_cores and not isinstance(action, ActionCore):
                        continue
                    actions.append(action)
        return actions
    
    def get_action(self, identifier: InputIdentifier = None, state: int = None, index: int = None):
        if identifier is None:
            return None
        return self.action_objects.get(identifier.input_type, {}).get(identifier.json_identifier, {}).get(state, {}).get(index)
    
    def get_action_dict(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None):
        # Arg validation
        if action_object is None:
            if None in (identifier, state, index):
                raise ValueError("Please pass an identifier, state and index or an action object")
            
        if action_object is None:
            action_object = self.get_action(identifier, state, index)

        if action_object is None:
            raise ValueError("Could not find action object")
        
        ident = action_object.input_ident
        for state in ident.get_states(self):
            for i, action_dict in enumerate(ident.get_actions(self, state)):
                if self.get_action(ident, int(state), i) is action_object:
                    return action_dict

        return {}
                
    def set_action_dict(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None, action_dict: _Dict = None):
        # Arg validation
        if action_object is None:
            if None in (identifier, state, index):
                raise ValueError("Please pass an identifier, state and index or an action object")
            
        if action_object is None:
            action_object = self.get_action(identifier, state, index)

        if action_object is None:
            raise ValueError("Could not find action object")
        
        # Do not name the loop variable action_dict. It shadows the parameter
        # and makes the assignment below a self-assignment.
        ident = action_object.input_ident
        for state in ident.get_states(self):
            actions = ident.get_actions(self, state)
            for i, _existing_dict in enumerate(actions):
                if self.get_action(ident, int(state), i) is action_object:
                    actions[i] = action_dict
                    break

        self.save()
    
    def get_action_settings(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None):
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        return action_dict.get("settings", {})


    def set_action_settings(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None, settings: _Dict = None):
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        action_dict["settings"] = settings
        self.set_action_dict(action_object, identifier, state, index, action_dict)

    def get_action_event_assignments(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None):
        action_dict = self.get_action_dict(action_object, identifier, state, index)

        # backwards compat
        assignments = action_dict.get("event-assignments", {})
        for key, value in assignments.items():
            if value == "None":
                assignments[key] = None

        return assignments
    
    
    def set_action_event_assigment(self, event_assigner: EventAssigner | None, input_event: InputEvent | None, action_object: ActionCore = None, identifier: InputIdentifier = None, state: int = None, index: int = None):
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        action_dict.setdefault("event-assignments", {})
        action_dict["event-assignments"][str(input_event)] = event_assigner.id if event_assigner else None
        self.set_action_dict(action_object, identifier, state, index, action_dict)


    def has_key_an_image_controlling_action(self, identifier, state: int):
        input_type = identifier.input_type
        json_identifier = identifier.json_identifier
        if input_type not in self.action_objects or json_identifier not in self.action_objects[input_type]:
            return False
        for action in self.action_objects[input_type][json_identifier][state].values():
            if hasattr(action, "CONTROLS_KEY_IMAGE"):
                if action.CONTROLS_KEY_IMAGE:
                    return True
        return False

    @log.catch
    def initialize_actions(self):
        for action in self.get_all_actions():
            # Atomic claim. A bare check-then-set lets two concurrent
            # load_page calls for one page both read False and both submit
            # ready callbacks, which runs a second on_ready and duplicates the
            # backend processes. Only the claim is under the lock.
            with self._ready_claim_lock:
                if action.on_ready_called:
                    continue
                action.on_ready_called = True
            action.load_event_overrides()
            action.load_initial_generative_ui()
            # A plugin callback can block without end, so it runs on the deck's
            # action pool and never on the caller's thread, which is often GTK.
            self._submit_ready_callbacks(action)

    def _submit_ready_callbacks(self, action: ActionCore):
        executor = getattr(self.deck_controller, "action_executor", None)
        if executor is None:
            # The deck is in teardown, so drop the call.
            return
        try:
            executor.submit(self._run_ready_callbacks, action)
        except RuntimeError:
            # The executor shut down, because the deck disconnected mid-call.
            pass

    @log.catch
    def _run_ready_callbacks(self, action: ActionCore):
        try:
            action.on_ready()
        except Exception:
            log.opt(exception=True).error(
                f"on_ready failed for action {getattr(action, 'action_id', action)}"
            )
        finally:
            # A raising on_ready must still open the tick and update gates, or
            # the action stays dead for the life of the page.
            action.on_ready_finished = True
        action.on_update()

    def clear_action_objects(self):
        for input_type in self.action_objects:
            for input_identifier in self.action_objects[input_type]:
                for state in self.action_objects[input_type][input_identifier]:
                    state_dict = self.action_objects[input_type][input_identifier][state]
                    for action in list(state_dict.values()):
                        # Notify before the detach, because plugin cleanup
                        # code can still need action.page. teardown() always calls
                        # clean_up(), and it is a no-op for a placeholder.
                        ActionCore.teardown(action)
                        if hasattr(action, "page"):
                            action.page = None
                    state_dict.clear()
            self.action_objects[input_type] = {}

    def get_pages_with_same_json(self, get_self: bool = False) -> list:
        pages: list[Page]= []
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # Snapshot active_page once. Another thread sets it to None while a
            # controller connects, disconnects or closes, so a re-read per check
            # races a non-None guard against a None deref of .json_path.
            active_page = controller.active_page
            if active_page is None:
                continue
            if active_page == self and not get_self:
                continue
            if active_page.json_path == self.json_path:
                pages.append(active_page)
        return pages
    
    def reload_similar_pages(self, identifier: InputIdentifier = None, reload_self: bool = False,
                             load_brightness: bool = True, load_screensaver: bool = True, load_background: bool = True, load_inputs: bool = True,
                             load_dials: bool = True, load_touchscreens: bool = True):

        # A caller that edits the dict and comes straight here, such as a
        # pasted key or a pasted dial, has no other save in the call, so this
        # save must stay. It marks the page, the read barrier inside the reload
        # writes the mark out, and the re-read below then returns the edit
        # instead of erasing it.
        self.save()
        for page in self.get_pages_with_same_json(get_self=reload_self):
            page.load(load_from_file=True)
            # page.deck_controller.update_input(identifier)
            if identifier is not None:
                page.deck_controller.load_input_from_identifier(identifier, page)
            else:
                # Each controller gets its own Page object. A self here loads
                # this controller's Page onto the other decks.
                page.deck_controller.load_page(page)

    def get_action_comment(self, index: int, state: int, identifier: InputIdentifier) -> str:
        try:
            return self.dict[identifier.input_type][identifier.json_identifier]["states"][str(state)]["actions"][index].get("comment")
        except KeyError:
            return ""

    def set_action_comment(self, index: int, comment: str, state: int, identifier: InputIdentifier):
        if identifier.json_identifier in self.action_objects[identifier.input_type] and index in self.action_objects[identifier.input_type][identifier.json_identifier][state]:
            self.dict[identifier.input_type][identifier.json_identifier]["states"][str(state)]["actions"][index]["comment"] = comment
            self.save()

    def fix_action_objects_order(self, identifier: InputIdentifier) -> None:
        """
        #TODO: Switch to list instead of dict to avoid this
        """
        if identifier.json_identifier not in self.action_objects.get(identifier.input_type, {}):
            return
        
        actions = list(self.action_objects[identifier.input_type][identifier.json_identifier].values())

        self.action_objects[identifier.input_type][identifier.json_identifier] = {}
        for i, action in enumerate(actions):
            self.action_objects[identifier.input_type][identifier.json_identifier][i] = action
    
    # Configuration
    def _get_dict_value(self, keys: list[str]):
        # Any, because the walk descends out of the page's mapping into
        # whatever the leaf holds. The except below catches that case.
        value: Any = self.dict
        for i, key in enumerate(keys):
            fallback: dict | None = {}
            if i == len(keys) - 1:
                fallback = None

            try:
                value = value.get(key, fallback)
            except AttributeError:
                # A path shorter than keys ended on a non-dict.
                return
        return value
    
    def _set_dict_value(self, keys: list[str], value):
        d = self.dict
        for i, key in enumerate(keys):
            if i == len(keys) - 1:
                d[key] = value
            else:
                d = d.setdefault(key, {})

        self.save()

    def update_key_image(self, coords: str | tuple[int, int], state: int) -> None:
        #TODO: Move to DeckController
        #TODO: Make input specific
        coords = self.get_tuple_coords(coords)
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            # active_page is None while a controller connects, disconnects or
            # closes. Skip it instead of raising AttributeError.
            active_page = controller.active_page
            if active_page is None or active_page.json_path != self.json_path:
                continue
            key_index = controller.coords_to_index(coords)
            if key_index is None:
                continue
            if key_index > len(controller.inputs[Input.Key]) - 1:
                continue
            key = controller.inputs[Input.Key][key_index]
            if key.state == state:
                key.update()

    def update_input(self, identifier: InputIdentifier, state: int, wake: bool = True) -> None:
        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            if wake:
                if controller.screen_saver.showing:
                    controller.screen_saver.hide()

            # active_page is None while a controller connects, disconnects or
            # closes. Skip it instead of raising AttributeError.
            active_page = controller.active_page
            if active_page is None or active_page.json_path != self.json_path:
                continue
            c_input = controller.get_input(identifier)
            if c_input is None:
                continue
            if c_input.state != state:
                continue
            c_input.update()

    def get_controller_inputs(self, identifier: InputIdentifier) -> list["ControllerInput"]:
        inputs: list["ControllerInput"] = []

        for controller in (gl.deck_manager.deck_controller if gl.deck_manager is not None else []):
            for c_input in controller.get_inputs(identifier):
                if c_input.identifier == identifier:
                    inputs.append(c_input)

        return inputs

    # ControllerInputState, because ControllerInput declares states as the
    # base, and every use below in the label and layout managers is a
    # base-class member.
    def get_controller_input_states(self, identifier: InputIdentifier, state: int) -> list["ControllerInputState"]:
        matching_states: list["ControllerInputState"] = []

        for controller_input in self.get_controller_inputs(identifier):
            for input_state in controller_input.states.values():
                if input_state.state == state:
                    matching_states.append(input_state)

        return matching_states

    def get_page_coords(self, coords: str | tuple[int, int]) -> str:
        if isinstance(coords, tuple):
            return f"{coords[0]}x{coords[1]}"
        return coords
    
    def get_tuple_coords(self, coords: str | tuple[int, int]) -> tuple[int, int]:
        if isinstance(coords, str):
            x, y = coords.split("x")
            return int(x), int(y)
        return coords
    
    # Get/set methods

    def get_label_manager(self, identifier: InputIdentifier, state: int) -> "LabelManager | None":
        c_input = self.deck_controller.get_input(identifier)
        if c_input is None:
            return None
        input_state = c_input.states.get(state)
        if input_state is None:
            return None

        return input_state.label_manager
        

    def get_label_text(self, identifier: InputIdentifier, state: int, label_position: str) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "text"])

    def set_label_text(self, identifier: InputIdentifier, state: int, label_position: str, text: str, update: bool = True) -> None:
        for input_state in self.get_controller_input_states(identifier, state):
            input_state.label_manager.page_labels[label_position].text = text
            # An in-place mutation skips set_page_label and its invalidation,
            # so drop the scroll caches here. A shortened label otherwise keeps
            # scrolling, and a lengthened one never starts.
            input_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "text"], text)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].text = text
            label_manager.invalidate_scroll_caches()

        if update:
            self.update_input(identifier, state)

    def get_label_font_family(self, identifier: InputIdentifier, state: int, label_position: str) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-family"])

    def set_label_font_family(self, identifier: InputIdentifier, state: int, label_position: str, font_family: str, update: bool = True) -> None:
        for input_state in self.get_controller_input_states(identifier, state):
            input_state.label_manager.page_labels[label_position].font_name = font_family
            input_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-family"], font_family)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_name = font_family
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def get_label_font_size(self, identifier: InputIdentifier, state: int, label_position: str) -> int:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-size"])
    
    def get_label_font_style(self, identifier: InputIdentifier, state: int, label_position: str) -> int:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-style"])
    
    def get_label_font_weight(self, identifier: InputIdentifier, state: int, label_position: str) -> int:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-weight"])

    def set_label_font_size(self, identifier: InputIdentifier, state: int, label_position: str, font_size: int, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].font_size = font_size
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-size"], font_size)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_size = font_size
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_weight(self, identifier: InputIdentifier, state: int, label_position: str, font_weight: int, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].font_weight = font_weight
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-weight"], font_weight)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_weight = font_weight
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_color(self, identifier: InputIdentifier, state: int, label_position: str, font_color: list[int], update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].color = font_color
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "color"], font_color)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].color = font_color
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    # outline_width is a scalar stroke width, and None means the font default.
    # KeyLabel.outline_width is int | None. The callers are the spin button and
    # the reset button of LabelEditor, which pass int and None.
    def set_label_outline_width(self, identifier: InputIdentifier, state: int, label_position: str, outline_width: int | None, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].outline_width = outline_width
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "outline_width"], outline_width)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].outline_width = outline_width
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_outline_color(self, identifier: InputIdentifier, state: int, label_position: str, outline_color: list[int], update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].outline_color = outline_color
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "outline_color"], outline_color)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].outline_color = outline_color
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_style(self, identifier: InputIdentifier, state: int, label_position: str, font_style: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].style = font_style
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "style"], font_style)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].style = font_style
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_alignment(self, identifier: InputIdentifier, state: int, label_position: str, alignment: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].alignment = alignment
            key_state.label_manager.invalidate_scroll_caches()

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "alignment"], alignment)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].alignment = alignment
            label_manager.invalidate_scroll_caches()
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def get_media_size(self, identifier: InputIdentifier, state: int) -> float:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "size"])

    def set_media_size(self, identifier: InputIdentifier, state: int, size: float, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.size = size

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "size"], size)

        if update:
            self.update_input(identifier, state)

    def get_media_valign(self, identifier: InputIdentifier, state: int) -> float:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "valign"])

    def set_media_valign(self, identifier: InputIdentifier, state: int, valign: float, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.valign = valign

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "valign"], valign)

        if update:
            self.update_input(identifier, state)

    def get_media_halign(self, identifier: InputIdentifier, state: int) -> float:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "halign"])

    def set_media_halign(self, identifier: InputIdentifier, state: int, halign: float, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.halign = halign

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "halign"], halign)

        if update:
            self.update_input(identifier, state)

    def get_media_path(self, identifier: InputIdentifier, state: int) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "path"])

    def set_media_path(self, identifier: InputIdentifier, state: int, path: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.path = path  # type: ignore[attr-defined]  # ImageLayout (Subclasses/KeyLayout.py) declares no `path` field; nothing reads this write

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "path"], path)

        if update:
            self.update_input(identifier, state)

    def get_media_fps(self, identifier: InputIdentifier, state: int) -> int:
        value = self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "fps"])
        return 30 if value is None else int(value)

    def set_media_fps(self, identifier: InputIdentifier, state: int, fps: int, update: bool = True) -> None:
        # Apply to a playing video at once, so the change waits for no page
        # reload. GIF media (KeyGIF) keeps its own timeline and has no
        # set_playback, so only InputVideo media takes the cap.
        for input_state in self.get_controller_input_states(identifier, state):
            # Only where this page shows, which is the filter update_input
            # uses. Without it, an edit of one page's FPS row rebases the video
            # timeline on a deck that shows another page.
            controller = getattr(input_state, "deck_controller", None)
            active_page = getattr(controller, "active_page", None)
            if active_page is None or active_page.json_path != self.json_path:
                continue
            video = getattr(input_state, "key_video", None) or getattr(input_state, "video", None)
            if video is not None and hasattr(video, "set_playback"):
                video.set_playback(fps=fps, loop=video.loop)

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "fps"], fps)

        if update:
            self.update_input(identifier, state)

    def get_background_color(self, identifier: InputIdentifier, state: int) -> list[int]:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "color"])

    def set_background_color(self, identifier: InputIdentifier, state: int, color: list[int], update: bool = True, update_ui: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.background_manager.set_page_color(color, update=update, update_ui=update_ui)

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "color"], color)

    def get_background_image(self, identifier: InputIdentifier, state: int) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "image"])

    def set_background_image(self, identifier: InputIdentifier, state: int, path: str, update: bool = True) -> None:
        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "image"], path)
        if update:
            self.update_input(identifier, state)

    def get_background_loop(self, identifier: InputIdentifier, state: int) -> bool:
        value = self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "loop"])
        return True if value is None else bool(value)

    def set_background_loop(self, identifier: InputIdentifier, state: int, loop: bool, update: bool = True) -> None:
        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "loop"], loop)
        if update:
            self.update_input(identifier, state)

    def get_background_fps(self, identifier: InputIdentifier, state: int) -> int:
        value = self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "fps"])
        return 30 if value is None else int(value)

    def set_background_fps(self, identifier: InputIdentifier, state: int, fps: int, update: bool = True) -> None:
        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "background", "fps"], fps)
        if update:
            self.update_input(identifier, state)


class NoActionHolderFound:
    def __init__(self, id: str, state: int, identifier: InputIdentifier = None):
        self.id = id
        self.action_id = id
        self.type = type
        self.identifier = identifier
        self.state = state


class ActionOutdated:
    def __init__(self, id: str, state: int, identifier: InputIdentifier = None):
        self.id = id
        self.action_id = id
        self.type = type
        self.identifier = identifier
        self.state = state