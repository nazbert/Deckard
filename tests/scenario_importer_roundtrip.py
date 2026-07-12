"""
Scenario: StreamDeck-UI importer round-trip (issue #55).

Three defects under test:
  1. It wrote `font_size`/`font_family` (underscores) into label dicts; the
     loader reads `font-size`/`font-family` -- imported fonts were dead keys.
  2. It REPLACED settings/decks/<serial>.json wholesale, erasing whatever
     deck settings already existed (rotation, unrelated sections).
  3. It overwrote same-named pages (`ui_<deck>_<n>.json`) -- and any suffix
     scheme must keep intra-import ChangePage references pointing at the
     final (suffixed) filenames.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
import types

import globals as gl
from fixtures import start_watchdog


SERIAL = "SDUI01"


def main() -> int:
    start_watchdog(60, "importer_roundtrip")
    fixtures._install_integration_globals()
    # index_to_page_coords iterates gl.app.deck_manager.deck_controller.
    gl.app = types.SimpleNamespace(deck_manager=gl.deck_manager)

    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    decks_dir = os.path.join(gl.DATA_PATH, "settings", "decks")
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(decks_dir, exist_ok=True)

    # Pre-existing pages the import must NOT clobber (both target names).
    sentinel = {"sentinel": True}
    for n in (1, 2):
        with open(os.path.join(pages_dir, f"ui_{SERIAL}_{n}.json"), "w") as f:
            json.dump(sentinel, f)

    # Pre-existing deck settings the import must merge into, not replace.
    deck_settings_path = os.path.join(decks_dir, f"{SERIAL}.json")
    with open(deck_settings_path, "w") as f:
        json.dump({"rotation": 90, "brightness": {"value": 20}, "custom": {"keep": 1}}, f)

    # Minimal streamdeck-ui export: two pages; page 0's button switches to
    # page 1 (export switch_page is 1-based: value 2 -> export page "1").
    export = {
        "streamdeck_ui_version": 2,
        "state": {
            SERIAL: {
                "brightness": 40,
                "brightness_dimmed": 10,
                "display_timeout": 300,
                "buttons": {
                    "0": {
                        "0": {
                            "text": "hello",
                            "font_color": "#FF0000",
                            "switch_page": 2,
                        },
                    },
                    "1": {
                        "0": {"text": "second"},
                    },
                },
            }
        },
    }
    export_path = os.path.join(gl.DATA_PATH, "sdui_export.json")
    with open(export_path, "w") as f:
        json.dump(export, f)

    from src.windows.PageManager.Importer.StreamDeckUI.StreamDeckUI import StreamDeckUIImporter
    StreamDeckUIImporter(export_path).perform_import()

    failures = []

    # --- 3a: sentinels untouched -----------------------------------------
    for n in (1, 2):
        with open(os.path.join(pages_dir, f"ui_{SERIAL}_{n}.json")) as f:
            if json.load(f) != sentinel:
                failures.append(f"pre-existing page ui_{SERIAL}_{n}.json was clobbered")

    # --- 3b: imported pages landed under suffixed names -------------------
    imported = sorted(
        p for p in os.listdir(pages_dir)
        if p.startswith(f"ui_{SERIAL}_") and p.endswith(".json")
        and p not in (f"ui_{SERIAL}_1.json", f"ui_{SERIAL}_2.json")
    )
    page0_dict = None
    page1_path = None
    if len(imported) != 2:
        failures.append(f"expected 2 collision-suffixed imported pages, found {imported}")
    else:
        # Identify by content, not filename: page 0 carries the "hello"
        # label, page 1 carries "second".
        for name in imported:
            full = os.path.join(pages_dir, name)
            with open(full) as f:
                data = json.load(f)
            texts = [
                label.get("text")
                for key in data.get("keys", {}).values()
                for state in key.get("states", {}).values()
                for label in state.get("labels", {}).values()
            ]
            if "hello" in texts:
                page0_dict = data
            elif "second" in texts:
                page1_path = full
        if page0_dict is None or page1_path is None:
            failures.append(f"could not identify imported pages by content in {imported}")
            page0_dict = None

    if page0_dict is not None:
        state0 = page0_dict.get("keys", {}).get("0x0", {}).get("states", {}).get("0", {})

        # --- 1: hyphenated label keys the loader reads ---------------------
        label = state0.get("labels", {}).get("bottom", {})
        if "font-family" not in label or "font-size" not in label:
            failures.append(f"label written with keys {sorted(label)} -- loader reads font-family/font-size")
        if "font_family" in label or "font_size" in label:
            failures.append("label still contains underscore font keys the loader never reads")
        if label.get("text") != "hello":
            failures.append(f"label text lost: {label.get('text')!r}")

        # --- 3c: ChangePage points at page 1's FINAL (suffixed) path -------
        actions = state0.get("actions", [])
        change = [a for a in actions if a.get("id") == "com_core447_DeckPlugin::ChangePage"]
        if not change:
            failures.append(f"switch_page did not produce a ChangePage action: {actions}")
        else:
            target = change[0].get("settings", {}).get("selected_page")
            if target != page1_path:
                failures.append(
                    f"ChangePage points at {target!r}; the import wrote page 1 to {page1_path!r}")

    # --- 2: deck settings merged, not replaced ----------------------------
    with open(deck_settings_path) as f:
        deck_settings = json.load(f)
    if deck_settings.get("rotation") != 90 or deck_settings.get("custom") != {"keep": 1}:
        failures.append(f"pre-existing deck settings erased: {deck_settings}")
    if deck_settings.get("brightness", {}).get("value") != 40:
        failures.append(f"imported brightness not applied: {deck_settings.get('brightness')}")
    screensaver = deck_settings.get("screensaver", {})
    if screensaver.get("time-delay") != 5 or screensaver.get("brightness") != 10 \
            or screensaver.get("enable") is not True:
        failures.append(f"imported screensaver settings wrong: {screensaver}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("PASS: importer writes loader keys, merges deck settings, and "
          "suffixes colliding pages with consistent ChangePage references")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
