"""
Pins the page state-key type contract (gl#174).

A page json's ``states`` map is keyed by STRINGS -- Page.save() writes
self.dict verbatim through atomic_write_json, and JSON has no other option.
The in-memory ``action_objects`` registry is keyed by INTS. Before the
InputIdentifier accessors landed, every walk re-derived that boundary by
hand, and several did it inconsistently in one expression (Page.get_action_dict
iterated ``str`` state keys but looked the same states up in action_objects
with ``int(state)``), so a page could round-trip with an int key wedged into
the json tree.

Checks:
  (a) The accessors coerce -- get_state_dict/get_actions/get_action_entry
      return the same live objects for state 0 and "0", and never for a
      state that doesn't exist.
  (b) They hand back LIVE dicts (mutating the result is visible in
      page.dict), and ensure_state_dict creates the chain.
  (c) get_action_dict / set_action_dict / set_action_settings round-trip
      against a real Page carrying a real action object.
  (d) No non-str state key ever appears in page.dict -- or in the json
      actually written to disk -- after any of those writes.
"""
import json
import os

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl
from fixtures import start_watchdog, teardown

from src.backend.DeckManagement.InputIdentifier import Input

WATCHDOG_SECONDS = 60

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def bad_state_keys(tree: dict) -> list:
    """Every non-str state key anywhere under a page tree."""
    bad = []
    for input_type in Input.KeyTypes:
        for ident, input_dict in tree.get(input_type, {}).items():
            for state in input_dict.get("states", {}):
                if not isinstance(state, str):
                    bad.append((input_type, ident, state, type(state).__name__))
    return bad


def check_accessor_coercion_and_liveness(page, ident) -> None:
    states = ident.get_states(page)
    check("get_states returns the live states map",
          states is page.dict[ident.input_type][ident.json_identifier]["states"])
    check("state keys in the loaded page are all str",
          all(isinstance(k, str) for k in states), str(list(states)))

    by_int = ident.get_state_dict(page, 0)
    by_str = ident.get_state_dict(page, "0")
    check("get_state_dict coerces int and str to the same live dict",
          by_int is by_str is states["0"])

    check("get_state_dict on a missing state gives an empty dict",
          ident.get_state_dict(page, 7) == {})
    check("get_actions coerces too",
          ident.get_actions(page, 0) is ident.get_actions(page, "0")
          is states["0"]["actions"])
    check("get_actions on a missing state gives an empty list",
          ident.get_actions(page, 7) == [])

    entry = ident.get_action_entry(page, 0, 0)
    check("get_action_entry returns the live action dict",
          entry is states["0"]["actions"][0])
    check("get_action_entry out of range gives None",
          ident.get_action_entry(page, 0, 5) is None
          and ident.get_action_entry(page, 0, -1) is None)

    # Live, not a copy: a mutation through the accessor is in page.dict.
    ident.get_state_dict(page, 0)["harness-probe"] = 1
    check("get_state_dict hands back a live dict",
          page.dict[ident.input_type][ident.json_identifier]["states"]["0"].get("harness-probe") == 1)
    del ident.get_state_dict(page, 0)["harness-probe"]

    # ensure_state_dict builds the chain, with a str key, for a state that
    # is not there yet -- and is idempotent.
    created = ident.ensure_state_dict(page, 3)
    created["media"] = {"path": None}
    check("ensure_state_dict creates the state under a str key",
          page.dict[ident.input_type][ident.json_identifier]["states"].get("3") is created)
    check("ensure_state_dict is idempotent",
          ident.ensure_state_dict(page, "3") is created)

    fresh = Input.Key("4x4")
    made = fresh.ensure_state_dict(page, 0)
    check("ensure_state_dict creates a missing input too",
          page.dict["keys"]["4x4"]["states"]["0"] is made)

    check("no non-str state key after the accessor writes",
          not bad_state_keys(page.dict), str(bad_state_keys(page.dict)))

    # Leave the page as we found it.
    del page.dict[ident.input_type][ident.json_identifier]["states"]["3"]
    del page.dict["keys"]["4x4"]


def check_action_dict_round_trip(page, ident) -> None:
    action = page.get_action(identifier=ident, state=0, index=0)
    check("the seeded action object loaded", action is not None)
    if action is None:
        return

    check("action_objects is keyed by int state",
          0 in page.action_objects[ident.input_type][ident.json_identifier],
          str(list(page.action_objects[ident.input_type][ident.json_identifier])))

    action_dict = page.get_action_dict(action)
    check("get_action_dict finds the entry by object identity",
          action_dict is ident.get_action_entry(page, 0, 0), repr(action_dict))

    page.set_action_settings(action_object=action, settings={"probe": 42})
    check("set_action_settings round-trips through the accessors",
          page.get_action_settings(action) == {"probe": 42},
          repr(page.get_action_settings(action)))

    replacement = dict(action_dict)
    replacement["settings"] = {"probe": 99}
    page.set_action_dict(action_object=action, action_dict=replacement)
    check("set_action_dict replaced the entry in the page dict",
          ident.get_action_entry(page, 0, 0) is replacement)
    check("set_action_dict is visible through get_action_settings",
          page.get_action_settings(action) == {"probe": 99})

    check("no non-str state key after the write path",
          not bad_state_keys(page.dict), str(bad_state_keys(page.dict)))


def check_disk_round_trip(page) -> None:
    page.save()
    with open(page.json_path) as f:
        on_disk = json.load(f)
    check("no non-str state key survives to disk",
          not bad_state_keys(on_disk), str(bad_state_keys(on_disk)))

    # An int key smuggled straight into the live dict must be the ONLY way
    # to get one there -- i.e. the accessors are the coercion point, and a
    # raw write bypassing them is what this guard would catch.
    page.dict["keys"]["5x5"] = {"states": {0: {"actions": []}}}
    check("the guard actually detects a non-str state key",
          bool(bad_state_keys(page.dict)))
    del page.dict["keys"]["5x5"]


def main() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "state_keys_icon.png"), color=(0, 120, 200))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)
    start_watchdog(WATCHDOG_SECONDS, label="scenario_state_key_types")

    controller = fixtures.make_headless_controller(serial="state-keys-1")
    try:
        key_ident = controller.inputs[Input.Key][0].identifier.json_identifier
        page = gl.page_manager.get_page(
            fixtures.seed_action_page("StateKeys", key_ident), controller)
        controller.load_page(page, allow_reload=True)

        ident = Input.Key(key_ident)

        print("accessor coercion + liveness")
        check_accessor_coercion_and_liveness(page, ident)
        print("action dict round trip")
        check_action_dict_round_trip(page, ident)
        print("disk round trip")
        check_disk_round_trip(page)
    finally:
        teardown(controller)

    if FAILURES:
        raise SystemExit(f"FAIL: {len(FAILURES)} check(s) failed: {FAILURES}")
    print("\nALL PASS: scenario_state_key_types")


if __name__ == "__main__":
    main()
