from typing import TYPE_CHECKING, TypedDict
from enum import Enum

if TYPE_CHECKING:
    from src.backend.PageManagement.Page import Page
    from src.backend.DeckManagement.deck_controller.controller import DeckController
    from src.backend.DeckManagement.deck_controller.inputs import ControllerInput


# Shape of one entry under an input's "states" map in a page json.
# Annotation only -- the accessors below hand back the live dicts. Written
# with the functional syntax because the control keys are hyphenated.
StateDict = TypedDict("StateDict", {
    "actions": list[dict],
    "media": dict,
    "labels": dict,
    "background": dict,
    "image-control-action": "int | None",
    "label-control-actions": list,
    "background-control-action": "int | None",
}, total=False)


class InputIdentifier:
    # Every concrete input below (Input.Key/Dial/Touchscreen) defines its own
    # nested Events enum, so code holding the base type can reach it.
    # Annotation only -- like InputEvent.string_name, this declares the
    # attribute without creating one, so `hasattr(InputIdentifier, "Events")`
    # stays False exactly as before.
    Events: "type[InputEvent]"

    def __init__(self, input_type: str, json_identifier: str, controller_class_name: str):
        self.input_type = input_type
        self.json_identifier: str = json_identifier
        self.controller_class_name = controller_class_name

    def get_config(self, page: "Page") -> dict:
        return page.dict.get(self.input_type, {}).get(self.json_identifier, {})

    def get_dict(self, d):
        return d.get(self.input_type, {}).get(self.json_identifier)

    # -- page state accessors ------------------------------------------
    #
    # State keys in a page json are strings (Page.save writes self.dict
    # verbatim); int keys are legitimate only in the in-memory
    # action_objects registry. str(state) below is the single place that
    # coercion happens, so callers can pass either. Every one of these
    # returns the LIVE nested dict/list -- callers mutate in place and
    # page.save() writes self.dict wholesale.

    def get_states(self, page: "Page") -> dict:
        return self.get_config(page).get("states", {})

    def get_state_dict(self, page: "Page", state) -> dict:
        return self.get_states(page).get(str(state), {})

    def get_actions(self, page: "Page", state) -> list:
        return self.get_state_dict(page, state).get("actions", [])

    def get_action_entry(self, page: "Page", state, index: int) -> dict | None:
        actions = self.get_actions(page, state)
        if 0 <= index < len(actions):
            return actions[index]
        return None

    def ensure_state_dict(self, page: "Page", state) -> dict:
        """Like get_state_dict, but creates the input/states/state chain so
        the returned dict is actually part of the page."""
        input_dict = page.dict.setdefault(self.input_type, {}).setdefault(self.json_identifier, {})
        return input_dict.setdefault("states", {}).setdefault(str(state), {})

    # DeckController.get_input answers None when this identifier is not among
    # the controller's inputs (wrong deck model, stale identifier), so the
    # optional is honest rather than a lie the callers have to unlearn.
    def get_controller_input(self, controller: "DeckController") -> "ControllerInput | None":
        return controller.get_input(self)
    
    def __eq__(self, o):
        if o is None:
            return False
        if not isinstance(o, InputIdentifier):
            raise ValueError(f"Invalid type {type(o)} for InputIdentifier")
        return self.input_type == o.input_type and self.json_identifier == o.json_identifier

    def __str__(self):
        return f"Input({self.input_type}, {self.json_identifier})"
    
    def __hash__(self) -> int:
        return hash((self.input_type, self.json_identifier))

class InputEvent(Enum):
    # Annotation only -- an Enum body turns *assignments* into members, so
    # this declares the per-member attribute __new__ sets below without
    # creating a member of its own.
    string_name: str

    def __new__(cls, string_name):
        obj = object.__new__(cls)
        obj.string_name = string_name
        return obj
    
    def __str__(self) -> str:
        return self.string_name
    
    
class Input:
    class Key(InputIdentifier):
        input_type = "keys"
        controller_class_name = "ControllerKey"

        class Events(InputEvent):
            DOWN = "Key Down"
            UP = "Key Up"
            SHORT_UP = "Key Short Up"
            HOLD_START = "Key Hold Start"
            HOLD_STOP = "Key Hold Stop"

        def __init__(self, json_identifier: str):
            self.coords = Input.Key.Coords_From_PageCoords(json_identifier)
            self.json_identifier = Input.Key.Coords_To_PageCoords(self.coords)
            super().__init__(self.input_type, self.json_identifier, self.controller_class_name)

        @staticmethod
        def Coords_From_PageCoords(page_coords: str):
            split = page_coords.split("x")
            return (int(split[0]), int(split[1]))
        
        @staticmethod
        def Coords_To_PageCoords(coords: tuple[int, int]):
            return f"{coords[0]}x{coords[1]}"
        
        @staticmethod
        def Index_To_Coords(deck_controller: "DeckController", index):
            rows, cols = deck_controller.deck.key_layout()
            x = index % cols
            y = index // cols
            return (x, y)
        
        @staticmethod
        def Coords_To_Index(deck_controller: "DeckController", coords):# -> Any:
            if type(coords) == str:
                coords = coords.split("x")
            x, y = map(int, coords)
            rows, cols = deck_controller.deck.key_layout()
            return y * cols + x
        
        def get_page_coords(self):
            return self.Coords_To_PageCoords(self.coords)
        
        def get_index(self, deck_controller: "DeckController"):
            return self.Coords_To_Index(deck_controller, self.coords)
        
    class Dial(InputIdentifier):
        input_type = "dials"
        controller_class_name = "ControllerDial"

        class Events(InputEvent):
            DOWN = "Dial Down"
            UP = "Dial Up"
            SHORT_UP = "Dial Short Up"
            HOLD_START = "Dial Hold Start"
            HOLD_STOP = "Dial Hold Stop"
            TURN_CW = "Dial Turn CW"
            TURN_CCW = "Dial Turn CCW"
            SHORT_TOUCH_PRESS = "Dial Touchscreen Short Press"
            LONG_TOUCH_PRESS = "Dial Touchscreen Long Press"
  
        def __init__(self, json_identifier: str):
            self.index = int(json_identifier)
            super().__init__(self.input_type, json_identifier, self.controller_class_name)


    class Touchscreen(InputIdentifier):
        input_type = "touchscreens"
        controller_class_name = "ControllerTouchScreen"

        class Events(InputEvent):
            DRAG_LEFT = "Touchscreen Drag Left"
            DRAG_RIGHT = "Touchscreen Drag Right"

        def __init__(self, json_identifier: str):
            self.index = str(json_identifier)
            super().__init__(self.input_type, json_identifier, self.controller_class_name)

    All = (Key, Dial, Touchscreen)
    KeyTypes = [key_type.input_type for key_type in All]
    
    @staticmethod
    def FromTypeIdentifier(input_type: str, json_identifier: str):
        input_map = {
            "keys": Input.Key,
            "dials": Input.Dial,
            "touchscreens": Input.Touchscreen
        }
        if input_type in input_map:
            return input_map[input_type](json_identifier)
        raise ValueError(f"Unknown input type {input_type}")
    
    @staticmethod
    def AllEvents() -> list[InputEvent]:
        events: list[InputEvent] = []

        for t in Input.All:
            events.extend(list(t.Events))



        return events
        for attr in dir(Input):
            nested_class = getattr(Input, attr, None)
            if isinstance(nested_class, type):
                for sub_attr in dir(nested_class):
                    sub_nested_class = getattr(nested_class, sub_attr, None)
                    if isinstance(sub_nested_class, type) and issubclass(sub_nested_class, Enum):
                        events.extend(list(sub_nested_class))
        return events
    
    @staticmethod
    def EventFromStringName(string_name: str | None) -> InputEvent | None:
        if string_name in [None, str(None)]:
            return None
        for event in Input.AllEvents():
            if event.string_name == string_name:
                return event
        raise ValueError(f"Unknown string name {string_name}")