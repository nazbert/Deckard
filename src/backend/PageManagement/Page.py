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
from argparse import Action
import gc
import os
import json
import sys
import threading
import time
import tempfile

# Import globals first to get IS_MAC
import globals as gl

if not gl.IS_MAC:
    from evdev import InputEvent

from loguru import logger as log
from copy import copy
import shutil

# Import globals
from src.backend.PluginManager.EventAssigner import EventAssigner
from src.backend.DeckManagement.ImageHelpers import crop_key_image_from_deck_sized_image
import globals as gl

from src.backend.PluginManager.ActionCore import ActionCore
from src.backend.DeckManagement.InputIdentifier import Input, InputIdentifier
# Import typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.backend.DeckManagement.DeckController import LabelManager
    from src.backend.PluginManager.ActionHolder import ActionHolder
    from src.backend.DeckManagement.DeckController import ControllerKeyState, ControllerKey


# One save lock per page json path, shared across every Page object for that
# path: each controller showing the same page holds its OWN Page instance, so
# a per-object lock/semaphore can never order two controllers' saves of the
# same file (issue #55).
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def _get_save_lock(json_path: str) -> threading.Lock:
    with _save_locks_guard:
        return _save_locks.setdefault(json_path, threading.Lock())


def _snapshot_json_tree(value):
    """Structural copy of a json-shaped tree: dicts/lists are re-created,
    leaves are shared by reference. dict.copy() and list() run entirely in C
    under the GIL, so each container snapshots atomically even while another
    thread is mutating it -- unlike json.dump (or copy.deepcopy), this can
    never raise `RuntimeError: dict changed size during iteration`. Leaves
    are deliberately NOT deep-copied: action entries hold live ActionCore
    objects under "object" (the caller strips those from the copy), which
    must never be duplicated."""
    if isinstance(value, dict):
        return {key: _snapshot_json_tree(item) for key, item in value.copy().items()}
    if isinstance(value, list):
        return [_snapshot_json_tree(item) for item in list(value)]
    return value


class Page:
    def __init__(self, json_path, deck_controller, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.dict = {}

        self.json_path = json_path
        self.deck_controller = deck_controller

        # Dir that contains all actions this allows us to keep them at reload
        self.action_objects = {}

        self.ready_to_clear = True

        self.load(load_from_file=True) #TODO: Later we want to limit the load of action objects to the available inputs

    def get_name(self) -> str:
        return os.path.splitext(os.path.basename(self.json_path))[0]
    
    def update_dict(self) -> None:
        """
        Updates the dict without any updates on the action objects.
        Do NOT use if you made changes to the action objects
        """
        self.dict = gl.page_manager.get_page_data(self.json_path)
    
    def load(self, load_from_file: bool = False):
        start = time.time()
        if load_from_file:
            self.update_dict()
        self.load_action_objects()

        # Call on_ready for all actions
        end = time.time()
        log.debug(f"Loaded page {self.get_name()} in {end - start:.2f} seconds")

    def save(self):
        # Keyed by json_path, not per-object: two controllers showing the
        # same page must not interleave their backup/write on one file.
        with _get_save_lock(self.json_path):
            # Make backup in case something goes wrong
            self.make_backup()

            without_objects = self.get_without_action_objects()
            # Make keys last element
            for type in Input.KeyTypes:
                self.move_key_to_end(without_objects, type)
            # Write to a temp file and atomically replace it, so an interrupted
            # write can't leave a truncated page.
            fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(self.json_path),
                                            prefix=".save-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(without_objects, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                # mkstemp creates 0600; keep the existing file's mode
                try:
                    mode = os.stat(self.json_path).st_mode & 0o777
                except FileNotFoundError:
                    mode = 0o644
                os.chmod(tmp_path, mode)
                os.replace(tmp_path, self.json_path)
                # fsync the directory so the rename itself is durable, not just data.
                try:
                    dir_fd = os.open(os.path.dirname(self.json_path), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            except BaseException:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise

    def make_backup(self):
        os.makedirs(os.path.join(gl.DATA_PATH, "pages","backups"), exist_ok=True)

        src_path = self.json_path
        dst_path = os.path.join(gl.DATA_PATH, "pages","backups", os.path.basename(src_path))

        # Check if json in src is valid
        with open(src_path) as f:
            try:
                json.load(f)
            except json.decoder.JSONDecodeError as e:
                log.error(f"Invalid json in {src_path}: {e}")
                return

        shutil.copy2(src_path, dst_path)

    def move_key_to_end(self, dictionary, key):
        # Operates on the passed dict (save()'s snapshot). This used to
        # pop/reinsert on live self.dict instead -- mutating the page
        # mid-save while never reordering the dict actually being written.
        if key in dictionary:
            dictionary[key] = dictionary.pop(key)

    def set_background(self, file_path):
        self.dict.setdefault("background", {})
        self.dict["background"]["path"] = file_path
        self.save()

    def load_action_objects(self):
        new_action_objects = {}

        for input_type in Input.All:
            input_class = getattr(sys.modules["src.backend.DeckManagement.DeckController"], input_type.controller_class_name)
            input_type_name = input_type.input_type
            for key in input_class.Available_Identifiers(self.deck_controller.deck):
                for state in self.dict.get(input_type_name, {}).get(key, {}).get("states", {}):
                    try:
                        state = int(state)
                    except ValueError:
                        continue
                    for i, action in enumerate(self.dict[input_type_name][key]["states"][str(state)].get("actions", [])):
                        if action.get("id") is None:
                            continue

                        input_ident = Input.FromTypeIdentifier(input_type_name, key)
                        # input_action_objects = input_ident.get_dict(new_action_objects)
                        # input_action_objects.setdefault(state, {})

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
                # Framework-owned teardown: notify then unconditionally
                # clean_up(), so a plugin overriding the hook without
                # super() can't leak the dropped action (D1).
                ActionCore.teardown(old_action)

        self.action_objects = new_action_objects

        if self.deck_controller.active_page == self:
            # if it's already loaded - this way it only triggers on newly added actions
            self.initialize_actions()

    # def load_action_object_sector(self, loaded_action_objects, dict_key: str, state)

    def get_new_action_object(self, loaded_action_objects: dict, action_id: str, state: int, i: int, input_ident):
        
        action_holder = gl.plugin_manager.get_action_holder_from_id(action_id)

        ## No action holder found
        if action_holder is None:
            plugin_id = gl.plugin_manager.get_plugin_id_from_action_id(action_id)
            if gl.plugin_manager.get_is_plugin_out_of_date(plugin_id):
                return ActionOutdated(id=action_id, identifier=input_ident, state=state)
            return NoActionHolderFound(id=action_id, identifier=input_ident, state=state)

        ## Keep old object if it exists
        old_action = loaded_action_objects.get(input_ident.input_type, {}).get(input_ident.json_identifier, {}).get(state, {}).get(i)
        if old_action is not None:
            if isinstance(old_action, action_holder.action_core):
                return old_action #FIXME: gets never used
            
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
                for state in self.dict[input_type][input_identifier].get("states", {}):
                    state = int(state)
                    input_ident = Input.FromTypeIdentifier(input_type, input_identifier)
                    if "actions" not in input_ident.get_config(self.dict)["states"][str(state)]:
                        continue
                    for i, action in enumerate(input_ident.get_config(self.dict)["states"][str(state)]["actions"]):
                        if action.get("id") is None:
                            continue

                        input_action_objects = input_ident.get_dict(self.action_objects)
                        input_action_objects.setdefault(state, {})

                        action_holder = gl.plugin_manager.get_action_holder_from_id(action["id"])
                        if action_holder is None:
                            plugin_id = gl.plugin_manager.get_plugin_id_from_action_id(action["id"])
                            if gl.plugin_manager.get_is_plugin_out_of_date(plugin_id):
                                input_action_objects[state][i] = ActionOutdated(id=action["id"])
                            else:
                                input_action_objects[state][i] = NoActionHolderFound(id=action["id"])
                            continue
                        action_class = action_holder.action_core
                        
                        if action_class is None:
                            input_action_objects[state][i] = NoActionHolderFound(id=action["id"])
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

        all_threads_finished = False
        while not all_threads_finished:
            all_threads_finished = True
            for thread in add_threads:
                if thread.is_alive():
                    all_threads_finished = False
                    break
            time.sleep(0.02)

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

        for action in from_actions.values():
            action: "ActionCore" = action
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
    def add_action_object_from_holder(self, action_holder: "ActionHolder", input_ident: "InputIdentifier", state: str, i: int):
        action_object = action_holder.init_and_get_action(deck_controller=self.deck_controller, page=self, input_ident=input_ident, state=state)
        if action_object is None:
            return
        self.action_objects.setdefault(input_ident.input_type, {})
        self.action_objects[input_ident.input_type].setdefault(input_ident.json_identifier, {})
        self.action_objects[input_ident.input_type][input_ident.json_identifier].setdefault(int(state), {})
        self.action_objects[input_ident.input_type][input_ident.json_identifier][int(state)][i] = action_object

    def remove_plugin_action_objects(self, plugin_id: str) -> bool:
        plugin_obj = gl.plugin_manager.get_plugin_by_id(plugin_id)
        if plugin_obj is None:
            return False

        # Collect first, then delete + tear down. `del action` on the local
        # variable used to be the only "cleanup" here -- it doesn't do
        # anything to the actual object, which is why plugin uninstall never
        # called clean_up() (design-doc bug 7).
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
        for input_type in list(self.action_objects.keys()):
            for json_identifier in list(self.action_objects[input_type].keys()):
                for state in list(self.action_objects[input_type][json_identifier].keys()):
                    for index in list(self.action_objects[input_type][json_identifier][state].keys()):
                        action_core = self.action_objects[input_type][json_identifier][state][index]
                        action_id = action_core.action_id

                        if gl.plugin_manager.get_plugin_id_from_action_id(action_id) == plugin_id:
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
            # A page json doesn't necessarily have every input type present
            # (e.g. no "touchscreens" section on a non-Plus deck) -- bug 38.
            for key in self.dict.get(type, {}):
                for state in self.dict[type][key].get("states", {}):
                    actions = self.dict[type][key]["states"][state].get("actions", [])
                    # Collect indices first: deleting from `actions` while
                    # enumerate() is still walking it skips the entry right
                    # after each deletion (bug 38).
                    to_remove = [
                        i for i, action in enumerate(actions)
                        # Actions are plain dicts here (raw json), not
                        # ActionCore objects -- `action.id` doesn't exist.
                        if action.get("id", "").split("::")[0] == plugin_id
                    ]
                    for i in reversed(to_remove):
                        del actions[i]

        self.save()

    def get_without_action_objects(self):
        # Serialize from a snapshot, never the live tree: json.dump over
        # self.dict raced concurrent mutations (a RuntimeError mid-dump
        # lost the whole save), and the old shallow copy() meant the
        # `del action["object"]` below mutated the ORIGINAL action dicts.
        dictionary = _snapshot_json_tree(self.dict)
        for type in Input.KeyTypes:
            for key in dictionary.get(type, {}):
                for state in dictionary[type][key].get("states", {}):
                    if "actions" not in dictionary[type][key]["states"][state]:
                        continue
                    for action in dictionary[type][key]["states"][state]["actions"]:
                        if "object" in action:
                            del action["object"]

        return dictionary

    def get_all_actions(self, action_dict: dict = None):
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
        
        for state in self.dict.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get("states", {}):
            for i, action_dict in enumerate(self.dict[action_object.input_ident.input_type][action_object.input_ident.json_identifier]["states"][state].get("actions", [])):
                if self.action_objects.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get(int(state), {}).get(i) is action_object:
                    return action_dict
                
        return {}
                
    def set_action_dict(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None, action_dict: dict = None):
        # Arg validation
        if action_object is None:
            if None in (identifier, state, index):
                raise ValueError("Please pass an identifier, state and index or an action object")
            
        if action_object is None:
            action_object = self.get_action(identifier, state, index)

        if action_object is None:
            raise ValueError("Could not find action object")
        
        # NB: the loop variable must not be named `action_dict` -- it used to
        # shadow the parameter, turning the assignment below into a no-op
        # self-assignment (issue #55).
        for state in self.dict.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get("states", {}):
            for i, _existing_dict in enumerate(self.dict[action_object.input_ident.input_type][action_object.input_ident.json_identifier]["states"][state].get("actions", [])):
                if self.action_objects.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get(int(state), {}).get(i) is action_object:
                    self.dict[action_object.input_ident.input_type][action_object.input_ident.json_identifier]["states"][state]["actions"][i] = action_dict
                    break

        self.save()
    
    def get_action_settings(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None):
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        return action_dict.get("settings", {})
        # Arg validation
        if action_object is None:
            if None in (identifier, state, index):
                raise ValueError("Please pass an identifier, state and index or an action object")
            
        if action_object is None:
            action_object = self.get_action(identifier, state, index)

        if action_object is None:
            raise ValueError("Could not find action object")

        for state in self.dict.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get("states", {}):
            for i, action_dict in enumerate(self.dict[action_object.input_ident.input_type][action_object.input_ident.json_identifier]["states"][state].get("actions", [])):
                if self.action_objects.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get(int(state), {})[i] is action_object:
                    return action_dict["settings"]
        return {}
    
    def set_action_settings(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None, settings: dict = None):
        action_dict = self.get_action_dict(action_object, identifier, state, index)
        action_dict["settings"] = settings
        self.set_action_dict(action_object, identifier, state, index, action_dict)
        return
        # Arg validation
        if action_object is None:
            if None in (identifier, state, index):
                raise ValueError("Please pass an identifier, state and index or an action object")
            
        if action_object is None:
            action_object = self.get_action(identifier, state, index)

        if action_object is None:
            raise ValueError("Could not find action object")

        for state in self.dict.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get("states", {}):
            for i, action_dict in enumerate(self.dict[action_object.input_ident.input_type][action_object.input_ident.json_identifier]["states"][state].get("actions", [])):
                if self.action_objects.get(action_object.input_ident.input_type, {}).get(action_object.input_ident.json_identifier, {}).get(int(state), {})[i] is action_object:
                    action_dict["settings"] = settings

        self.save()

    def get_action_event_assignments(self, action_object = None, identifier: InputIdentifier = None, state: int = None, index: int = None):
        action_dict = self.get_action_dict(action_object, identifier, state, index)

        # backwards compat
        assignments = action_dict.get("event-assignments", {})
        for key, value in assignments.items():
            if value == "None":
                assignments[key] = None

        return assignments
    
    
    def set_action_event_assigment(self, event_assigner: EventAssigner | None, input_event: "InputEvent | None", action_object: ActionCore = None, identifier: InputIdentifier = None, state: int = None, index: int = None):
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
            if not action.on_ready_called:
                action.on_ready_called = True
                action.load_event_overrides()
                action.load_initial_generative_ui()
                # Plugin callbacks can block indefinitely; run them on the
                # deck's action pool, never on the caller's (often GTK) thread.
                self._submit_ready_callbacks(action)

    def _submit_ready_callbacks(self, action: ActionCore):
        executor = getattr(self.deck_controller, "action_executor", None)
        if executor is None:
            # Deck is being torn down; drop the call.
            return
        try:
            executor.submit(self._run_ready_callbacks, action)
        except RuntimeError:
            # Executor already shut down (deck disconnected mid-call)
            pass

    @log.catch
    def _run_ready_callbacks(self, action: ActionCore):
        action.on_ready()
        action.on_update()

    def clear_action_objects(self):
        for input_type in self.action_objects:
            for input_identifier in self.action_objects[input_type]:
                for state in self.action_objects[input_type][input_identifier]:
                    state_dict = self.action_objects[input_type][input_identifier][state]
                    for action in list(state_dict.values()):
                        # Notify before detaching: plugin cleanup code may
                        # still need action.page. clean_up() is unconditional
                        # regardless of what the hook does (D1) -- teardown()
                        # is a no-op for non-ActionCore placeholders.
                        ActionCore.teardown(action)
                        if hasattr(action, "page"):
                            action.page = None
                    state_dict.clear()
            self.action_objects[input_type] = {}

    def get_name(self):
        return os.path.splitext(os.path.basename(self.json_path))[0]
    
    def get_pages_with_same_json(self, get_self: bool = False) -> list:
        pages: list[Page]= []
        for controller in gl.deck_manager.deck_controller:
            if controller.active_page is None:
                continue
            if controller.active_page == self and not get_self:
                continue
            if controller.active_page.json_path == self.json_path:
                pages.append(controller.active_page)
        return pages
    
    def reload_similar_pages(self, identifier: InputIdentifier = None, reload_self: bool = False,
                             load_brightness: bool = True, load_screensaver: bool = True, load_background: bool = True, load_inputs: bool = True,
                             load_dials: bool = True, load_touchscreens: bool = True):
        
        self.save()
        for page in self.get_pages_with_same_json(get_self=reload_self):
            page.load(load_from_file=True)
            # page.deck_controller.update_input(identifier)
            if identifier is not None:
                page.deck_controller.load_input_from_identifier(identifier, page)
            else:
                page.deck_controller.load_page(self)

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
        value = self.dict
        for i, key in enumerate(keys):
            fallback = {}
            if i == len(keys) - 1:
                fallback = None

            try:
                value = value.get(key, fallback)
            except:
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
        gl.page_manager.update_dict_of_pages_with_path(self.json_path)

    def update_key_image(self, coords: str | tuple[int, int], state: int) -> None:
        #TODO: Move to DeckController
        #TODO: Make input specific
        coords = self.get_tuple_coords(coords)
        for controller in gl.deck_manager.deck_controller:
            if controller.active_page.json_path != self.json_path:
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
        for controller in gl.deck_manager.deck_controller:
            if wake:
                if controller.screen_saver.showing:
                    controller.screen_saver.hide()

            if controller.active_page.json_path != self.json_path:
                continue
            c_input = controller.get_input(identifier)
            if c_input is None:
                continue
            if c_input.state != state:
                continue
            c_input.update()

    def get_controller_inputs(self, identifier: InputIdentifier) -> list["ControllerInput"]:
        inputs: list["ControllerInput"] = []

        for controller in gl.deck_manager.deck_controller:
            for c_input in controller.get_inputs(identifier):
                if c_input.identifier == identifier:
                    inputs.append(c_input)

        return inputs

    def get_controller_input_states(self, identifier: InputIdentifier, state: int) -> list["ControllerKeyState"]:
        matching_states: list["ControllerKeyState"] = []

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
            return tuple(map(int, coords.split("x")))
        return coords
    
    # Get/set methods

    def get_label_manager(self, identifier: InputIdentifier, state: int) -> "LabelManager":
        c_input = self.deck_controller.get_input(identifier)
        if c_input is None:
            return
        state = c_input.states.get(state)
        if state is None:
            return
        
        return state.label_manager
        

    def get_label_text(self, identifier: InputIdentifier, state: int, label_position: str) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "text"])

    def set_label_text(self, identifier: InputIdentifier, state: int, label_position: str, text: str, update: bool = True) -> None:
        for input_state in self.get_controller_input_states(identifier, state):
            input_state.label_manager.page_labels[label_position].text = text

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "text"], text)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].text = text

        if update:
            self.update_input(identifier, state)

    def get_label_font_family(self, identifier: InputIdentifier, state: int, label_position: str) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-family"])

    def set_label_font_family(self, identifier: InputIdentifier, state: int, label_position: str, font_family: str, update: bool = True) -> None:
        for input_state in self.get_controller_input_states(identifier, state):
            input_state.label_manager.page_labels[label_position].font_family = font_family

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-family"], font_family)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_name = font_family
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

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-size"], font_size)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_size = font_size
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_weight(self, identifier: InputIdentifier, state: int, label_position: str, font_weight: int, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].font_weight = font_weight

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "font-weight"], font_weight)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].font_weight = font_weight
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_color(self, identifier: InputIdentifier, state: int, label_position: str, font_color: list[int], update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].color = font_color

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "color"], font_color)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].color = font_color
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_outline_width(self, identifier: InputIdentifier, state: int, label_position: str, outline_width: list[int], update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].outline_width = outline_width

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "outline_width"], outline_width)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].outline_width = outline_width
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_outline_color(self, identifier: InputIdentifier, state: int, label_position: str, outline_color: list[int], update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].outline_color = outline_color

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "outline_color"], outline_color)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].outline_color = outline_color
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_font_style(self, identifier: InputIdentifier, state: int, label_position: str, font_style: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].style = font_style

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "style"], font_style)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].style = font_style
            label_manager.update_label_editor()

        if update:
            self.update_input(identifier, state)

    def set_label_alignment(self, identifier: InputIdentifier, state: int, label_position: str, alignment: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.label_manager.page_labels[label_position].alignment = alignment

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "labels", label_position, "alignment"], alignment)

        label_manager = self.get_label_manager(identifier, state)
        if label_manager is not None:
            label_manager.page_labels[label_position].alignment = alignment
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

    def get_media_valign(self, identifier: InputIdentifier, state: int) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "valign"])

    def set_media_valign(self, identifier: InputIdentifier, state: int, valign: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.valign = valign

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "valign"], valign)

        if update:
            self.update_input(identifier, state)

    def get_media_halign(self, identifier: InputIdentifier, state: int) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "halign"])

    def set_media_halign(self, identifier: InputIdentifier, state: int, halign: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.halign = halign

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "halign"], halign)

        if update:
            self.update_input(identifier, state)

    def get_media_path(self, identifier: InputIdentifier, state: int) -> str:
        return self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "path"])

    def set_media_path(self, identifier: InputIdentifier, state: int, path: str, update: bool = True) -> None:
        for key_state in self.get_controller_input_states(identifier, state):
            key_state.layout_manager.page_layout.path = path

        self._set_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "path"], path)

        if update:
            self.update_input(identifier, state)

    def get_media_fps(self, identifier: InputIdentifier, state: int) -> int:
        value = self._get_dict_value([identifier.input_type, identifier.json_identifier, "states", str(state), "media", "fps"])
        return 30 if value is None else int(value)

    def set_media_fps(self, identifier: InputIdentifier, state: int, fps: int, update: bool = True) -> None:
        # Live-apply to any playing video so the change doesn't wait for a
        # page reload. GIF media (KeyGIF) has its own timeline and no
        # set_playback -- only InputVideo-style media takes the cap.
        for input_state in self.get_controller_input_states(identifier, state):
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