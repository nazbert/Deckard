from functools import lru_cache
import os
import json
import time

from src.backend.DeckManagement.HelperMethods import recursive_hasattr
from src.windows.PageManager.Importer.StreamDeckUI.helper import font_family_from_path, hex_to_rgba255
from src.windows.PageManager.Importer.StreamDeckUI.code_conv import parse_keys_as_keycodes

from src.Signals import Signals
from loguru import logger as log

import globals as gl

import gi
from gi.repository import GLib

class StreamDeckUIImporter:
    def __init__(self, json_export_path: str):
        self.json_export_path = json_export_path

    @lru_cache(maxsize=None)
    def index_to_page_coords(self, index: int, deck_serial: int) -> str:
        # Find deck
        rows, cols = 3, 5
        for deck_controller in gl.app.deck_manager.deck_controller:
            if deck_controller.serial_number() == deck_serial:
                rows, cols = deck_controller.deck.key_layout()
                break
        y = index // cols
        x = index % cols
        return f"{x}x{y}"
    
    def save_json(self, json_path: str, data: dict, _retries: int = 3):
        with open(json_path, "w") as f:
            json.dump(data, f, indent=4)

        loaded = None
        try:
            with open(json_path) as f:
                loaded = json.load(f)
        except Exception as e:
            pass

        if loaded != data:
            if _retries > 0:
                log.error(f"Failed to save {json_path}, trying again ({_retries} retries left)")
                self.save_json(json_path, data, _retries=_retries - 1)
            else:
                log.error(f"Failed to save {json_path} after all retries, giving up")
            
    def allocate_page_paths(self, deck: str, page_names) -> dict[str, str]:
        """Maps each export page name to a collision-free target path.
        Computed up front for the whole deck so ChangePage cross-references
        can point at the FINAL filenames -- and so an existing user page
        named ui_<deck>_<n>.json is never overwritten (a numeric suffix is
        appended instead)."""
        pages_dir = os.path.join(gl.DATA_PATH, "pages")
        os.makedirs(pages_dir, exist_ok=True)

        page_paths: dict[str, str] = {}
        allocated: set[str] = set()
        for page_name in page_names:
            base = f"ui_{deck}_{int(page_name) + 1}"
            candidate = os.path.join(pages_dir, f"{base}.json")
            suffix = 2
            while os.path.exists(candidate) or candidate in allocated:
                candidate = os.path.join(pages_dir, f"{base}_{suffix}.json")
                suffix += 1
            allocated.add(candidate)
            page_paths[page_name] = candidate

        return page_paths

    def get_state_map(self, available_states: list[str]):
        available_states = [int(state) for state in available_states]
        available_states.sort()

        state_map = {}
        for i, original_number in enumerate(available_states):
            state_map[str(i)] = str(original_number)

        return state_map

    def perform_import(self):
        with open(self.json_export_path) as f:
            self.export = json.load(f)


        for deck in self.export.get("state", {}):
            ## Deck preferences -- merge into whatever deck settings already
            ## exist; replacing the file wholesale erased every unrelated
            ## section (rotation, key layout, ...) on import (issue #55).
            preferences_path = os.path.join(gl.DATA_PATH, "settings", "decks", f"{deck}.json")
            preferences = {}
            try:
                with open(preferences_path) as f:
                    preferences = json.load(f)
            except (OSError, json.JSONDecodeError):
                preferences = {}
            preferences.setdefault("brightness", {})["value"] = self.export["state"][deck].get("brightness", 75)
            screensaver = preferences.setdefault("screensaver", {})
            screensaver["enable"] = True
            screensaver["time-delay"] = self.export["state"][deck].get("display_timeout", 5*60)//60
            screensaver["brightness"] = self.export["state"][deck].get("brightness_dimmed", 0)

            os.makedirs(os.path.dirname(preferences_path), exist_ok=True)
            self.save_json(preferences_path, preferences)

            # Final page filenames for this deck, collision-suffixed, so
            # same-named user pages survive and intra-import ChangePage
            # references stay consistent.
            page_paths = self.allocate_page_paths(deck, self.export["state"][deck].get("buttons", {}).keys())

            for page_name in self.export["state"][deck].get("buttons", {}):
                ## Keys
                page = {}
                page["keys"] = {}

                for button in self.export["state"][deck]["buttons"][page_name]:
                    coords = self.index_to_page_coords(int(button), deck)
                    page["keys"][coords] = {}

                    button_data = self.export["state"][deck]["buttons"][page_name][button]

                    # Support both formats: with explicit "states" dict, or
                    # flat format where properties are directly on the button
                    if "states" in button_data and button_data["states"]:
                        states = button_data["states"]
                    else:
                        states = {"0": button_data}

                    state_map = self.get_state_map(available_states=list(states.keys()))
                    for page_state, export_state in state_map.items():
                        state_data = states[export_state]

                        page["keys"][coords].setdefault("states", {})
                        page["keys"][coords]["states"].setdefault(page_state, {})

                        ## Text
                        font_color_hex = state_data.get("font_color")
                        if font_color_hex in [None, ""]:
                            font_color_hex = "#FFFFFFFF"
                        page["keys"][coords]["states"][page_state]["labels"] = {}
                        page["keys"][coords]["states"][page_state]["labels"]["bottom"] = {
                            "text": state_data.get("text", None),
                            "color": hex_to_rgba255(font_color_hex),
                            # Hyphenated: the keys Page/LabelManager read.
                            # The old underscore spellings were dead keys
                            # the loader never looked at (issue #55).
                            "font-size": None,
                            "font-family": font_family_from_path(state_data.get("font"))
                        }

                        page["keys"][coords]["states"][page_state]["background"] = {}
                        color_hex = state_data.get("background_color")
                        if color_hex not in [None, ""]:
                            page["keys"][coords]["states"][page_state]["background"]["color"] = hex_to_rgba255(color_hex)

                        ## Icon
                        page["keys"][coords]["states"][page_state]["media"] = {}
                        export_icon = state_data.get("icon")
                        if export_icon not in [None, ""]:
                            if os.path.exists(export_icon):
                                asset_id = gl.asset_manager_backend.add(asset_path=export_icon)
                                asset = gl.asset_manager_backend.get_by_id(asset_id)
                                page["keys"][coords]["states"][page_state]["media"]["path"] = asset["internal-path"]
                            else:
                                log.warning(f"Icon {export_icon} not found, skipping")

                        ## Actions
                        page["keys"][coords]["states"][page_state]["actions"] = []

                        # Switch page
                        export_switch_page = state_data.get("switch_page")
                        if str(export_switch_page) != str(int(page_name)+1) and export_switch_page not in [0, "0", None, ""]:
                            if export_switch_page not in [None, ""]:
                                # switch_page is 1-based over the export's
                                # 0-based page names; resolve through the
                                # allocation map so the reference tracks any
                                # collision suffix the target received.
                                page_path = None
                                try:
                                    page_path = page_paths.get(str(int(export_switch_page) - 1))
                                except (TypeError, ValueError):
                                    page_path = None
                                if page_path is None:
                                    # Target page not part of this export:
                                    # keep the historical naming.
                                    page_path = os.path.join(gl.DATA_PATH, "pages", f"ui_{deck}_{export_switch_page}.json")
                                action = {
                                    "id": "com_core447_DeckPlugin::ChangePage",
                                    "settings": {
                                        "selected_page": page_path,
                                        "deck_number": None
                                    }
                                }
                                page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Hotkey
                        if state_data.get("keys") not in [None, ""]:
                            parsed = ""
                            try:
                                parsed = parse_keys_as_keycodes(state_data["keys"])[0]
                            except Exception as e:
                                log.error(f"Failed to parse keys: {state_data['keys']}. Error: {e}")

                            if parsed not in [None, ""]:
                                action = {
                                    "id": "com_core447_OSPlugin::Hotkey",
                                    "settings": {
                                        "keys": []
                                    }
                                }
                                for key in parsed:
                                    action["settings"]["keys"].append([key, 1]) # Press

                                for key in parsed:
                                    action["settings"]["keys"].append([key, 0]) # Release

                                page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Write text
                        export_write = state_data.get("write")
                        if export_write not in [None, ""]:
                            action = {
                                "id": "com_core447_OSPlugin::WriteText",
                                "settings": {
                                    "text": export_write
                                }
                            }
                            page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Command
                        export_command = state_data.get("command")
                        if export_command not in [None, ""]:
                            action = {
                                "id": "com_core447_OSPlugin::RunCommand",
                                "settings": {
                                    "command": export_command
                                }
                            }
                            page["keys"][coords]["states"][page_state]["actions"].append(action)

                        # Brightness
                        export_brightness_change = state_data.get("brightness_change")
                        if export_brightness_change not in [None, "", 0]:
                            action = None
                            if export_brightness_change > 0:
                                action = {
                                    "id": "com_core447_DeckPlugin::IncreaseBrightness",
                                    "settings": {}
                                }
                            else:
                                action = {
                                    "id": "com_core447_DeckPlugin::DecreaseBrightness",
                                    "settings": {}
                                }
                            page["keys"][coords]["states"][page_state]["actions"].append(action)


                page_path = page_paths[page_name]
                self.save_json(page_path, page)
                # gl.signal_manager.trigger_signal(Signals.PageAdd, page_path) # We don't trigger the action to save ressources
                # time.sleep(0.005) # Otherwise the app can't hold up - The problem is the signal call, but is is necessary to

                gl.page_manager.update_dict_of_pages_with_path(page_path)
                gl.page_manager.reload_pages_with_path(page_path)
                log.success(f"Imported page {page_name} as page {os.path.basename(page_path)} on deck {deck}")

            log.success(f"Imported all pages of deck {deck}")

        log.success("Imported all pages from StreamDeck UI")

        if recursive_hasattr(gl, "app.main_win.sidebar.page_selector"):
            GLib.idle_add(gl.app.main_win.sidebar.page_selector.update)
        if recursive_hasattr(gl, "page_manager_window.page_selector"):
            GLib.idle_add(gl.page_manager_window.page_selector.load_pages)
        log.success("Updated ui")