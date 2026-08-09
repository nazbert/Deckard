from src.backend.DeckManagement.InputIdentifier import Input, InputEvent
from src.backend.PluginManager.EventAssigner import EventAssigner


class EventManager:
    def __init__(self):
        self._event_assigners: list[EventAssigner] = []

        self._overrides: dict[str, str] = {} # {"key_down": "event_1"}

    def set_overrides(self, overrides: dict[str, str]):
        self._overrides = overrides

    def add_event_assigner(self, event_assigner: EventAssigner):
        if self.get_event_assigner_by_id(event_assigner.id):
            raise ValueError(f"Event assigner with id '{event_assigner.id}' already exists on this action")
        self._event_assigners.append(event_assigner)

    def clear_event_assigners(self):
        self._event_assigners.clear()

    def get_all_event_assigners(self) -> list[EventAssigner]:
        return self._event_assigners

    def get_event_assigner_by_id(self, id: str) -> EventAssigner | None:
        for event_assigner in self._event_assigners:
            if event_assigner.id == id:
                return event_assigner
        return None

    def get_event_map(self, ignore_overrides: bool = False) -> dict[InputEvent, EventAssigner | None]:
        # Every known event is a key; the value is None for events no assigner
        # claims (and for an override that maps an event to nothing).
        event_map: dict[InputEvent, EventAssigner | None] = {}

        all_events = Input.AllEvents()
        for event in all_events:
            event_map[event] = None

        # Assign default events
        for event_assigner in self._event_assigners:
            for default_event in event_assigner.default_events:
                if default_event is None:
                    # EventAssigner falls back to [default_event] when neither
                    # default_events nor default_event was given, so an
                    # assigner declared with no events at all yields [None].
                    # Such an entry could never be looked up (the map is read
                    # by real InputEvent) -- keep it out of the map.
                    continue
                event_map[default_event] = event_assigner

        if not ignore_overrides:
            # Apply the overrides
            for input_event_str, event_id in self._overrides.items():
                input_event = Input.EventFromStringName(input_event_str)
                override_assigner = self.get_event_assigner_by_id(event_id) if event_id else None
                event_map[input_event] = override_assigner

        return event_map

    def get_event_assigner_for_event(self, event: InputEvent) -> EventAssigner | None:
        return self.get_event_map().get(event)