import enum
import json
from collections.abc import Callable
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from .Observer import Observer
from .Asset import Asset

class ManagerEvent(enum.Enum):
    ADD = "add",
    REMOVE = "remove",
    OVERRIDE_ADD = "override_add",
    OVERRIDE_REMOVE = "override_remove",

# The manager keys to the concrete Asset subclass it was built with, so
# Manager(Icon, "icons") is a Manager[Icon] and get_asset() returns an Icon.
AssetT = TypeVar("AssetT", bound=Asset)

class Manager(Generic[AssetT]):
    def __init__(self, asset_type: type[AssetT], json_key: str):
        self._asset_type: type[AssetT] = asset_type
        self._assets: dict[str, AssetT] = {}
        self._asset_overrides: dict[str, AssetT] = {}
        # The json_key, "colors" or "icons", names this manager's dispatch
        # lane in a wedge warning. An unlabeled lane reads "default" there.
        self._observer = Observer(label=f"plugin-settings:{json_key}")
        self._json_key = json_key

    # Assets

    def add_asset(self, key: str, asset: AssetT, override: bool = False):
        if not self._assets.__contains__(key) or override:
            self._assets[key] = asset
            self._observer.notify(ManagerEvent.ADD, key, asset)

    def remove_asset(self, key: str):
        """Remove an asset from the manager, and its override with it."""
        if self._assets.__contains__(key):
            del self._assets[key]
            self._observer.notify(ManagerEvent.REMOVE, key, None)

        if self._asset_overrides.__contains__(key):
            del self._asset_overrides[key]

    # Overrides

    def add_override(self, key: str, asset: AssetT, skip_asset_check: bool = False, override: bool = False):
        if not self._assets.__contains__(key) and not skip_asset_check:
            return

        if not self._asset_overrides.__contains__(key) or override:
            self._asset_overrides[key] = asset
            self._observer.notify(ManagerEvent.OVERRIDE_ADD, key, asset)

    def remove_override(self, key: str):
        if self._asset_overrides.__contains__(key):
            del self._asset_overrides[key]
            self._observer.notify(ManagerEvent.OVERRIDE_REMOVE, key, self.get_asset(key))

    # Getter

    def get_asset(self, key: str, skip_override: bool = False) -> AssetT | None:
        """Return the override of an asset, or the asset itself.

        :param key: The key of the asset
        :param skip_override: Return the asset and read no override
        :return: An Asset or None
        """
        if skip_override:
            return self._assets.get(key, None)
        return self._asset_overrides.get(key, self._assets.get(key, None))

    def get_asset_values(self, key: str, skip_override: bool = False):
        asset = self.get_asset(key, skip_override)
        if asset:
            return asset.get_values()
        return None

    def get_assets(self) -> MappingProxyType[str, AssetT]:
        return MappingProxyType(self._assets)

    def get_overrides(self) -> MappingProxyType[str, AssetT]:
        return MappingProxyType(self._asset_overrides)

    def get_assets_merged(self) -> MappingProxyType[str, AssetT]:
        combined = {**self._assets, **self._asset_overrides}
        return MappingProxyType(combined)

    # Observer

    def add_listener(self, callback: Callable[..., Any]):
        self._observer.subscribe(callback)

    def remove_listener(self, callback: Callable[..., Any]):
        self._observer.unsubscribe(callback)

    # Save/Load

    def get_asset_json(self):
        return json.dumps({key: asset.to_json() for key, asset in self._assets.items()}, indent=4)

    def get_override_json(self):
        out = {}

        for key, asset in self._asset_overrides.items():
            out[key] = asset.to_json()
        return out

    def load_json(self, json_data: dict):
        json = json_data.get(self._json_key, None)

        if not json:
            return

        for key, value in json.items():
            self.add_override(key, self._asset_type.from_json(value), skip_asset_check=True)

    def get_save_key(self):
        return self._json_key