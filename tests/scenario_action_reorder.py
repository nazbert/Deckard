"""
Regression scenario for the action-reorder buttons (issue #111, upstream
StreamController#577, 1.5.0-beta.14 regression).

Upstream dfcbd44a ("Chore: Respect adwaita accent color") turned
AddActionButtonRow from an Adw.PreferencesRow subclass into a plain wrapper
whose `.button` instance attribute is the real Adw.ButtonRow, and mechanically
rewrote ActionRow.on_click_up/on_click_down to reference
`AddActionButtonRow.button` (a class attribute that does not exist) and
`one_up_child.button` (ActionRow lost its overlay `.button` in 6055cb40).
Every click on the up/down buttons raised AttributeError inside the GTK
signal handler -- swallowed by PyGObject, so the buttons just appeared dead.

Un-breaking the buttons also re-activated reorder_actions, dead since
beta.14, which carried two latent defects of its own (review round 1):
"background-control-action" was never remapped after a move, and a page dict
without "label-control-actions" raised TypeError mid-write (after the page
dict and action_objects were reordered, before save()).

The handlers and reorder_actions are exercised here as plain functions on
duck-typed stand-ins (no GTK widget is instantiated; the module import brings
in Gtk/Adw class definitions only). That is a faithful discriminator: the
broken wiring raises AttributeError at the `AddActionButtonRow.button` class
attribute access no matter what the fakes look like.

Checks:
  (a) Handler wiring: on_click_up / on_click_down on a middle row run without
      raising, visually reorder via reorder_child_after(row, neighbour_row)
      (row widgets, not `.button` attrs), and call reorder_actions with the
      pre-move indices.
  (b) Guards: up on the top row and down on the bottom action row are no-ops
      (the neighbour is the add-action button row).
  (c) Data round-trip through the REAL ActionExpanderRow.reorder_actions:
      page dict "actions" order, action_objects order, and the remapping of
      ALL THREE control keys -- image-control-action,
      background-control-action, label-control-actions -- plus page.save()
      and controller.load_page(). Control values are chosen distinct from
      each other to catch cross-wiring between the remaps.
  (d) Stale-index hardening: two consecutive "up" clicks on the same row
      (dispatched before any sidebar rebuild) move it up twice -- requires
      update_indices() after each move, otherwise the second click undoes
      the first.
  (e) Missing control keys: a page dict carrying only "actions" (no
      image/background/label control keys -- hand-edited/imported/legacy
      pages) reorders without raising and the write COMPLETES (save +
      load_page), with the control keys defaulted the same way
      ActionPermissionManager reads them.
"""
import copy
import sys
from types import SimpleNamespace

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import globals as gl
from src.windows.mainWindow.elements.Sidebar.elements.ActionManager import (
    ActionExpanderRow,
    ActionRow,
)

# This scenario drives its checks at module top level (no main()); start the
# watchdog here so a hang in the code under test fails fast (#69).
fixtures.start_watchdog(60, label="scenario_action_reorder")

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def click(handler, row):
    """Invoke a click handler, returning the exception instead of letting it
    escape -- mirrors PyGObject swallowing handler exceptions, and keeps a
    run against broken code failing orderly instead of dying mid-scenario."""
    try:
        handler(row, None)
        return None
    except Exception as e:
        return e


# --------------------------------------------------------------------- #
# Duck-typed stand-ins. FakeExpander borrows the REAL reorder methods
# from ActionExpanderRow (they are plain functions in the class dict) so
# the data path under test is the production code, while the widget-tree
# plumbing (get_rows/reorder_child_after) is emulated with lists.
# --------------------------------------------------------------------- #

class FakeExpander:
    reorder_index_after = ActionExpanderRow.reorder_index_after
    reorder_action_objects = ActionExpanderRow.reorder_action_objects
    reorder_actions = ActionExpanderRow.reorder_actions

    def __init__(self, rows, add_action_button, identifier, state):
        self.rows = list(rows)  # add button last, like the real expander
        self.add_action_button = add_action_button
        self.active_identifier = identifier
        self.active_state = state
        self.reorder_actions_calls = []
        self.reorder_child_after_calls = []

    def get_rows(self):
        return list(self.rows)

    def reorder_child_after(self, child, after):
        # Emulates BetterExpander.reorder_child_after: child is re-inserted
        # at `after`'s pre-removal index (GtkHelper/GtkHelper.py:189-208).
        self.reorder_child_after_calls.append((child, after))
        after_index = self.rows.index(after)
        self.rows.remove(child)
        self.rows.insert(after_index, child)

    def update_indices(self):
        # Mirrors ActionExpanderRow.update_indices verbatim.
        for i, row in enumerate(self.get_rows()):
            row.index = i


class FakeRow:
    """Carries exactly what on_click_up/on_click_down dereference."""

    def __init__(self, name, index, expander):
        self.name = name
        self.index = index
        self.expander = expander

    def __repr__(self):
        return f"<FakeRow {self.name}@{self.index}>"


def make_world(action_ids, image_control=0, background_control=0,
               label_controls=(0, 0, 0), include_control_keys=True):
    """A fake controller/page pair wired into gl.app, plus a FakeExpander
    holding one FakeRow per action and the add button last."""
    state = {"actions": [{"id": a, "settings": {}} for a in action_ids]}
    if include_control_keys:
        state["image-control-action"] = image_control
        state["background-control-action"] = background_control
        state["label-control-actions"] = list(label_controls)

    page = SimpleNamespace(
        dict={"keys": {"0x0": {"states": {"0": copy.deepcopy(state)}}}},
        action_objects={
            "keys": {"0x0": {0: {i: f"obj_{a}" for i, a in enumerate(action_ids)}}}
        },
        save_calls=0,
    )
    page.save = lambda: setattr(page, "save_calls", page.save_calls + 1)

    controller = SimpleNamespace(active_page=page, load_page_calls=[])
    controller.load_page = lambda p: controller.load_page_calls.append(p)

    gl.app = SimpleNamespace(
        main_win=SimpleNamespace(get_active_controller=lambda: controller)
    )

    identifier = SimpleNamespace(input_type="keys", json_identifier="0x0")
    add_button = SimpleNamespace(name="add-button")  # stands in for the Adw.ButtonRow
    expander = FakeExpander([], add_button, identifier, state=0)
    rows = [FakeRow(a, i, expander) for i, a in enumerate(action_ids)]
    expander.rows = rows + [add_button]
    return controller, page, expander, rows


def state_dict(page):
    return page.dict["keys"]["0x0"]["states"]["0"]


def action_order(page):
    return [a["id"] for a in state_dict(page)["actions"]]


def object_order(page):
    return list(page.action_objects["keys"]["0x0"][0].values())


# --------------------------------------------------------------------- #
# (a) + (c): "up" on the middle row of [A, B, C]
# --------------------------------------------------------------------- #
print("(a)/(c) middle row up: wiring fires and data round-trips")
controller, page, expander, rows = make_world(["A", "B", "C"], image_control=1,
                                              background_control=0,
                                              label_controls=[0, 1, 2])
raised = click(ActionRow.on_click_up, rows[1])  # B moves up

check("on_click_up does not raise", raised is None, repr(raised))
if raised is None:
    check("visual reorder used the row widgets themselves",
          expander.reorder_child_after_calls == [(rows[1], rows[0])],
          str(expander.reorder_child_after_calls))
    check("visual order is B,A,C,add",
          [getattr(r, "name", None) for r in expander.rows] == ["B", "A", "C", "add-button"],
          str(expander.rows))
    check("page dict actions reordered", action_order(page) == ["B", "A", "C"],
          str(action_order(page)))
    check("action_objects reordered", object_order(page) == ["obj_B", "obj_A", "obj_C"],
          str(object_order(page)))
    check("image-control-action follows its action (1 -> 0)",
          state_dict(page)["image-control-action"] == 0,
          str(state_dict(page)["image-control-action"]))
    check("background-control-action follows its action (0 -> 1)",
          state_dict(page).get("background-control-action") == 1,
          str(state_dict(page).get("background-control-action")))
    check("label-control-actions remapped ([0,1,2] -> [1,0,2])",
          state_dict(page)["label-control-actions"] == [1, 0, 2],
          str(state_dict(page)["label-control-actions"]))
    check("page saved", page.save_calls == 1, str(page.save_calls))
    check("page reloaded on the controller",
          controller.load_page_calls == [page], str(controller.load_page_calls))
    check("row indices refreshed", [r.index for r in rows] == [1, 0, 2],
          str([r.index for r in rows]))

# --------------------------------------------------------------------- #
# (a): "down" on the middle row of [A, B, C]
# --------------------------------------------------------------------- #
print("(a) middle row down")
controller, page, expander, rows = make_world(["A", "B", "C"], image_control=1,
                                              background_control=2,
                                              label_controls=[2, 2, 2])
raised = click(ActionRow.on_click_down, rows[1])  # B moves down

check("on_click_down does not raise", raised is None, repr(raised))
if raised is None:
    check("visual order is A,C,B,add",
          [getattr(r, "name", None) for r in expander.rows] == ["A", "C", "B", "add-button"],
          str(expander.rows))
    check("page dict actions reordered", action_order(page) == ["A", "C", "B"],
          str(action_order(page)))
    check("image-control-action follows its action (1 -> 2)",
          state_dict(page)["image-control-action"] == 2,
          str(state_dict(page)["image-control-action"]))
    check("background-control-action follows its action (2 -> 1)",
          state_dict(page).get("background-control-action") == 1,
          str(state_dict(page).get("background-control-action")))
    check("label-control-actions remapped ([2,2,2] -> [1,1,1])",
          state_dict(page)["label-control-actions"] == [1, 1, 1],
          str(state_dict(page)["label-control-actions"]))

# --------------------------------------------------------------------- #
# (b) guards: top row up / bottom row down are no-ops
# --------------------------------------------------------------------- #
print("(b) edge guards")
controller, page, expander, rows = make_world(["A", "B"])
raised = click(ActionRow.on_click_up, rows[0])  # rows[-1] is the add button
check("top row up does not raise", raised is None, repr(raised))
check("top row up is a no-op (no visual reorder)",
      expander.reorder_child_after_calls == [] and action_order(page) == ["A", "B"],
      f"{expander.reorder_child_after_calls} / {action_order(page)}")
check("top row up saves nothing", page.save_calls == 0, str(page.save_calls))

raised = click(ActionRow.on_click_down, rows[1])  # rows[2] is the add button
check("bottom row down does not raise", raised is None, repr(raised))
check("bottom row down is a no-op",
      expander.reorder_child_after_calls == [] and action_order(page) == ["A", "B"],
      f"{expander.reorder_child_after_calls} / {action_order(page)}")

# --------------------------------------------------------------------- #
# (d) double-click before any sidebar rebuild: indices must not go stale
# --------------------------------------------------------------------- #
print("(d) consecutive clicks between rebuilds")
controller, page, expander, rows = make_world(["A", "B", "C"], image_control=2,
                                              background_control=0,
                                              label_controls=[2, 0, 1])
c_row = rows[2]
raised = click(ActionRow.on_click_up, c_row)
raised = raised or click(ActionRow.on_click_up, c_row)
check("two ups do not raise", raised is None, repr(raised))
check("two ups move C to the top", action_order(page) == ["C", "A", "B"],
      str(action_order(page)))
check("objects follow", object_order(page) == ["obj_C", "obj_A", "obj_B"],
      str(object_order(page)))
check("image-control-action follows across both moves (2 -> 0)",
      state_dict(page)["image-control-action"] == 0,
      str(state_dict(page)["image-control-action"]))
check("background-control-action follows across both moves (0 -> 1)",
      state_dict(page).get("background-control-action") == 1,
      str(state_dict(page).get("background-control-action")))
raised = click(ActionRow.on_click_up, c_row)
check("third up is a no-op (C now at top)",
      raised is None and action_order(page) == ["C", "A", "B"],
      f"{raised!r} / {action_order(page)}")

# --------------------------------------------------------------------- #
# (e) page dict without any control keys (hand-edited/imported/legacy):
#     the reorder must complete its write instead of raising mid-write.
# --------------------------------------------------------------------- #
print("(e) missing control keys")
controller, page, expander, rows = make_world(["A", "B", "C"],
                                              include_control_keys=False)
raised = click(ActionRow.on_click_up, rows[1])  # B moves up
check("reorder without control keys does not raise", raised is None, repr(raised))
check("actions still reordered", action_order(page) == ["B", "A", "C"],
      str(action_order(page)))
check("objects still reordered", object_order(page) == ["obj_B", "obj_A", "obj_C"],
      str(object_order(page)))
check("write completed: page saved", page.save_calls == 1, str(page.save_calls))
check("write completed: page reloaded", controller.load_page_calls == [page],
      str(controller.load_page_calls))
check("absent image-control-action stays None",
      state_dict(page).get("image-control-action") is None,
      str(state_dict(page).get("image-control-action")))
check("absent background-control-action stays None",
      state_dict(page).get("background-control-action") is None,
      str(state_dict(page).get("background-control-action")))
check("absent label-control-actions defaults like ActionPermissionManager",
      state_dict(page).get("label-control-actions") == [None, None, None],
      str(state_dict(page).get("label-control-actions")))

# --------------------------------------------------------------------- #
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("all checks passed")
sys.exit(0)
