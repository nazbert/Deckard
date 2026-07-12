"""
Regression scenario for gl#31: version.parse("1.5.0-beta.5") <
version.parse("1.5.0"), so MigrationManager runs Migrator_1_5_0_beta_5
FIRST for a pre-beta.5 upgrader, nesting every key's labels/media under
states.0 -- and Migrator_1_5_0.migrate_pages then walked the old FLAT key
shape, so its per-key rewrites (Core447::* asset-id renames, label
normalizations) silently no-op'd: dangling icon-pack paths, keys render
blank. The staggered upgrade (pre-beta.5 -> some beta -> final 1.5.0) hits
the same hole, since the page is already nested by the time Migrator_1_5_0
finally arms.

Covers:
  (a) full chain: a pre-beta.5-shaped page run through MigrationManager
      (real version ordering, gl.app_version = final "1.5.0") ends with the
      asset-id renames and label normalizations applied INSIDE the
      states.0-nested shape;
  (b) legacy flat shape: Migrator_1_5_0.migrate_pages alone still applies
      its rewrites to a page whose keys were never nested;
  (c) scope: the rename/normalize pass touches ONLY labels/media -- the other
      state fields (actions, image-control-action, ...) survive verbatim; and
      a states-shaped key's stray top-level media is renamed too (Fix 4).
"""
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
    # migrations.json flags -- so every check sees "not yet migrated"
    migrations_json = os.path.join(gl.DATA_PATH, "settings", "migrations.json")
    if os.path.exists(migrations_json):
        os.remove(migrations_json)


def _pre_beta5_key() -> dict:
    """A key exactly as a pre-beta.5 install stored it: labels/media flat on
    the key dict, with the legacy default values Migrator_1_5_0 normalizes."""
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
        "renders blank (gl#31)"
    )
    label = state["labels"]["bottom"]
    assert label["text"] is None, f"{where}: empty label text not normalized to None"
    assert label["font-family"] is None, f"{where}: default font-family not normalized"
    assert label["font-size"] is None, f"{where}: default font-size not normalized"
    assert label["color"] is None, f"{where}: default label color not normalized"


def check_full_chain_rewrites_nested_shape() -> None:
    """(a) The real upgrade path: both migrators pending, ordered by parsed
    version (beta.5 first), against a pre-beta.5 page."""
    _reset()
    page_path = _write_page("PreBeta5", {
        "keys": {"0x0": _pre_beta5_key()},
        "background": {"path": OLD_ICON_PATH},
    })

    # A pre-beta.5 upgrader landing on final 1.5.0: both migrators arm.
    # (On a beta app_version Migrator_1_5_0 would not arm at all yet --
    # every current beta user hits this chain at final release.)
    original_app_version = gl.app_version
    gl.app_version = "1.5.0"
    try:
        manager = MigrationManager()
        # Same registration order as main.py (1_5_0 added first) -- the
        # manager must still run beta_5 first by version sort.
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
    # Page-level rename (outside the key walker) must keep working too.
    assert page["background"]["path"] == NEW_ICON_PATH, (
        "page background path was not renamed"
    )
    print("PASS: full migrator chain applies 1.5.0 rewrites inside states.0")


def check_flat_shape_still_rewritten() -> None:
    """(b) Migrator_1_5_0.migrate_pages alone against a never-nested page:
    the legacy flat walker behavior must be preserved."""
    _reset()
    page_path = _write_page("FlatShape", {"keys": {"1x1": _pre_beta5_key()}})

    Migrator_1_5_0().migrate_pages()

    key = _read_page(page_path)["keys"]["1x1"]
    assert "states" not in key, "migrate_pages must not reshape keys itself"
    _assert_rewrites_applied(key, "flat shape")
    print("PASS: 1.5.0 migrator still rewrites the legacy flat key shape")


def check_non_label_media_state_fields_untouched() -> None:
    """(c) The rename/normalize pass must touch ONLY labels/media -- the other
    state fields (actions, image-control-action, label-control-actions) must
    survive verbatim. Also pins Fix 4 (MR !11 review): a key that is already
    states-shaped AND carries a stray top-level media has that top-level media
    renamed too, instead of dangling."""
    _reset()
    page_path = _write_page("StatesShaped", {"keys": {"2x2": {
        "states": {"0": {
            "labels": {"bottom": {"text": "", "font-size": 15}},
            "media": {"path": OLD_ICON_PATH},
            "actions": [{"id": "com_core447_OSPlugin::Launch", "settings": {"x": 1}}],
            "image-control-action": 2,
            "label-control-actions": [0, 1, 0],
        }},
        # stray top-level media next to states -- beta_5 skips (won't nest) a
        # key that already has states, so without Fix 4 this would dangle.
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
    # Fix 4: stray top-level media on a states-shaped key is also renamed
    assert key["media"]["path"] == "com_core447_MaterialIcons/stray.png", (
        f"stray top-level media on a states-shaped key was not renamed -- "
        f"still {key['media']['path']!r}; it would dangle (MR !11 review Fix 4)"
    )
    print("PASS: rename pass leaves non-label/media state fields intact; "
          "stray top-level media also renamed")


def main() -> None:
    check_full_chain_rewrites_nested_shape()
    check_flat_shape_still_rewritten()
    check_non_label_media_state_fields_untouched()
    print("PASS: scenario_migration_ordering")


if __name__ == "__main__":
    main()
