"""
Regression scenario for migrator order against the nested key shape.

MigrationManager sorts Migrator_1_5_0_beta_5 before Migrator_1_5_0, so the keys
are nested under states.0 first.
"""

# Migrator_1_5_0 must apply its asset-id renames and label normalizations
# inside the nested shape and inside a flat page, and must leave the other
# state fields alone.
import json
import os
import shutil

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

import globals as gl

from src.backend.Migration.MigrationManager import MigrationManager
from src.backend.Migration.Migrators.Migrator_1_5_0 import Migrator_1_5_0
from src.backend.Migration.Migrators.Migrator_1_5_0_beta_5 import Migrator_1_5_0_beta_5

PAGES_DIR = os.path.join(gl.DATA_PATH, "pages")

OLD_ICON_PATH = "Core447::Material Icons/icons/some_icon.png"
NEW_ICON_PATH = "com_core447_MaterialIcons/icons/some_icon.png"


def _reset() -> None:
    shutil.rmtree(PAGES_DIR, ignore_errors=True)
    # Clear the migrations.json flags so every check starts unmigrated.
    migrations_json = os.path.join(gl.DATA_PATH, "settings", "migrations.json")
    if os.path.exists(migrations_json):
        os.remove(migrations_json)


def _pre_beta5_key() -> dict:
    """A key as a pre-beta.5 install stored it, with labels and media flat on
    the key dict and the legacy defaults Migrator_1_5_0 normalizes."""
    return {
        "labels": {
            "bottom": {
                "text": "",
                "font-family": "",
                "font-size": 15,
                "color": [255, 255, 255, 255],
            },
        },
        "media": {"path": OLD_ICON_PATH},
    }


def _write_page(name: str, page: dict) -> str:
    os.makedirs(PAGES_DIR, exist_ok=True)
    path = os.path.join(PAGES_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(page, f, indent=4)
    return path


def _read_page(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def _assert_rewrites_applied(state: dict, where: str) -> None:
    assert state["media"]["path"] == NEW_ICON_PATH, (
        f"{where}: media path was NOT renamed to the id system -- still "
        f"{state['media']['path']!r}; the icon-pack path dangles and the key "
        "renders blank"
    )
    label = state["labels"]["bottom"]
    assert label["text"] is None, f"{where}: empty label text not normalized to None"
    assert label["font-family"] is None, f"{where}: default font-family not normalized"
    assert label["font-size"] is None, f"{where}: default font-size not normalized"
    assert label["color"] is None, f"{where}: default label color not normalized"


def check_chain_rewrites_nested_shape() -> None:
    """Both migrators pending, ordered by parsed version, against a
    pre-beta.5 page."""
    _reset()
    page_path = _write_page("PreBeta5", {
        "keys": {"0x0": _pre_beta5_key()},
        "background": {"path": OLD_ICON_PATH},
    })

    # A pre-beta.5 install that lands on final 1.5.0 arms both migrators.
    original_app_version = gl.app_version
    gl.app_version = "1.5.0"
    try:
        manager = MigrationManager()
        # Registration order matches main.py. The manager must still run
        # beta_5 first, by version sort.
        manager.add_migrator(Migrator_1_5_0())
        manager.add_migrator(Migrator_1_5_0_beta_5())
        ordered = manager.get_ordered_migrators()
        assert isinstance(ordered[0], Migrator_1_5_0_beta_5), (
            "expected beta.5 to sort before 1.5.0 -- if this ever changes, "
            "this scenario's premise needs revisiting"
        )
        manager.run_migrators()
    finally:
        gl.app_version = original_app_version

    page = _read_page(page_path)
    key = page["keys"]["0x0"]
    assert "states" in key and "0" in key["states"], (
        "beta.5 migrator should have nested the key under states.0"
    )
    assert "media" not in key and "labels" not in key, (
        "key still has flat labels/media next to states -- shape is corrupt"
    )
    _assert_rewrites_applied(key["states"]["0"], "full chain, states.0")
    # The page-level rename runs outside the key walker.
    assert page["background"]["path"] == NEW_ICON_PATH, (
        "page background path was not renamed"
    )
    print("PASS: full migrator chain applies 1.5.0 rewrites inside states.0")


def check_flat_shape_rewritten() -> None:
    """Migrator_1_5_0.migrate_pages alone must still rewrite a never-nested
    page."""
    _reset()
    page_path = _write_page("FlatShape", {"keys": {"1x1": _pre_beta5_key()}})

    Migrator_1_5_0().migrate_pages()

    key = _read_page(page_path)["keys"]["1x1"]
    assert "states" not in key, "migrate_pages must not reshape keys itself"
    _assert_rewrites_applied(key, "flat shape")
    print("PASS: 1.5.0 migrator still rewrites the legacy flat key shape")


def check_other_state_fields_untouched() -> None:
    """The rename pass touches only labels and media. The other state fields
    survive verbatim. A states-shaped key with a stray top-level media has
    that media renamed too."""
    _reset()
    page_path = _write_page("StatesShaped", {"keys": {"2x2": {
        "states": {"0": {
            "labels": {"bottom": {"text": "", "font-size": 15}},
            "media": {"path": OLD_ICON_PATH},
            "actions": [{"id": "com_core447_OSPlugin::Launch", "settings": {"x": 1}}],
            "image-control-action": 2,
            "label-control-actions": [0, 1, 0],
        }},
        # Stray top-level media next to states. beta_5 skips a key that
        # already has states, so this media needs its own rename.
        "media": {"path": "Core447::Material Icons/stray.png"},
    }}})

    Migrator_1_5_0().migrate_pages()

    key = _read_page(page_path)["keys"]["2x2"]
    state = key["states"]["0"]
    # rewrites applied inside the state
    assert state["media"]["path"] == NEW_ICON_PATH, "nested media not renamed"
    assert state["labels"]["bottom"]["text"] is None
    assert state["labels"]["bottom"]["font-size"] is None
    # non-label/media fields untouched, byte-for-byte
    assert state["actions"] == [{"id": "com_core447_OSPlugin::Launch", "settings": {"x": 1}}], (
        f"actions were mutated by the rename pass: {state['actions']!r}"
    )
    assert state["image-control-action"] == 2, "image-control-action mutated"
    assert state["label-control-actions"] == [0, 1, 0], "label-control-actions mutated"
    # Stray top-level media on a states-shaped key is renamed too.
    assert key["media"]["path"] == "com_core447_MaterialIcons/stray.png", (
        f"stray top-level media on a states-shaped key was not renamed -- "
        f"still {key['media']['path']!r}; it would dangle"
    )
    print("PASS: rename pass leaves non-label/media state fields intact; "
          "stray top-level media also renamed")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_migration_ordering")
    check_chain_rewrites_nested_shape()
    check_flat_shape_rewritten()
    check_other_state_fields_untouched()
    print("PASS: scenario_migration_ordering")


if __name__ == "__main__":
    main()
