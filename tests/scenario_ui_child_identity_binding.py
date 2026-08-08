"""
Regression test (issue #156): a controller's UI child must be resolved by
OBJECT IDENTITY, never by matching a fresh device serial read against the
stack-child name.

Field incident 2026-07-16/17: the child was registered under one serial
string while later lookups re-read the serial from the device -- when the two
disagreed (dual-instance USB contention at boot; or the whole window replaced,
issue #158), the lookup missed forever, the key grid resolved to None forever,
and the preview push silently dirty-marked instead of painting the visible
grid: the app window only repainted on re-open.

Since #141 the binding lives in `GtkUIAdapter` (bound by object at
DeckStack.add_page, unbound at remove_page) instead of in two cached fields on
the controller -- so this pins the adapter. The guarded regression class is
unchanged: the fakes deliberately carry NO name information at all, so any
name-matching resurrection fails here.

Headless tier: children are plain namespaces and no widget is constructed, so
importing the adapter (and hence Gtk) without a display is fine.
"""
from types import SimpleNamespace

import fixtures

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input
from src.windows.ui_adapter import GtkUIAdapter


class _FakeStack:
    def __init__(self, children):
        # A None in `children` models GTK's ListModel race: iteration
        # snapshots len once, so pages removed mid-scan yield None for
        # trailing indices.
        self._pages = [
            None if c is None else SimpleNamespace(get_child=lambda c=c: c)
            for c in children
        ]

    def get_pages(self):
        return list(self._pages)

    def get_child_by_name(self, name):
        # Name lookups must cleanly MISS on these name-free fakes: a
        # name-based reimplementation of the lookup should fail the
        # meaningful assertions below, not crash on a missing API.
        return None


class _FakeButton:
    def __init__(self):
        self.images = []

    def set_image(self, image):
        self.images.append(image)


def _fake_grid(rows=1, cols=1):
    return SimpleNamespace(buttons=[[_FakeButton() for _ in range(rows)] for _ in range(cols)])


def _fake_child(controller, grid):
    return SimpleNamespace(
        deck_controller=controller,
        page_settings=SimpleNamespace(deck_config=SimpleNamespace(grid=grid)),
        # The controller's FPS-warning path pokes this on whatever child it
        # resolves -- give it a harmless sink so a background tick during the
        # test can't log noise.
        low_fps_banner=SimpleNamespace(set_revealed=lambda *_: None),
    )


def _fake_window(deck_stack):
    return SimpleNamespace(leftArea=SimpleNamespace(deck_stack=deck_stack))


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_ui_child_identity_binding")
    controller = fixtures.make_headless_controller(serial="identity-1")
    adapter = GtkUIAdapter()
    ui_port.install(adapter)
    try:
        grid = _fake_grid()
        child = _fake_child(controller, grid)

        # 1. add_page's bind (by object) is what every lookup uses.
        adapter.bind(controller, child)
        assert adapter.query_deck_widget(controller, "deck_stack_child") is child, (
            "the bound DeckStackChild was not honored"
        )
        assert adapter.query_deck_widget(controller, "key_grid") is grid, (
            "the key grid did not resolve through the bound child"
        )

        # 2. Cold resolution: attach_window's rescan binds by IDENTITY. The
        # fakes have no serial/name anywhere, so any name-based matching would
        # come up empty here (the pre-fix failure mode: previews then only
        # dirty-mark, forever).
        adapter.unbind(controller)
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None
        adapter._window = _fake_window(_FakeStack([child]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is child, (
            "the identity rescan failed to find the controller's own child "
            "-- previews would silently stop reaching the visible grid"
        )

        # 3. No false positive: a stack of other controllers' children.
        adapter.unbind(controller)
        stranger = _fake_child(object(), _fake_grid())
        adapter._window = _fake_window(_FakeStack([stranger]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None, (
            "the rescan matched a child belonging to another controller"
        )

        # 3b. Mid-scan stack mutation: trailing None pages (ListModel len
        # snapshot after a main-thread removal) must terminate the scan
        # cleanly, not raise.
        adapter._window = _fake_window(_FakeStack([stranger, None]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None, (
            "a scan over a mutating stack did not terminate cleanly"
        )

        # 4. Widget-tree replacement heals via re-binding (what add_page does
        # on a rebuilt window, issue #158): the adapter must serve the NEW
        # grid, never the orphaned old one.
        new_grid = _fake_grid()
        new_child = _fake_child(controller, new_grid)
        adapter.bind(controller, new_child)
        assert adapter.query_deck_widget(controller, "key_grid") is new_grid, (
            "a stale binding survived the re-bind -- pushes would land in the "
            "orphaned old widget tree"
        )

        # 5. The whole point of the binding: a mirror push reaches the bound
        # grid's button, and reaches NOTHING once unbound (-> the engine
        # dirty-marks instead, which is what load_from_changes replays).
        identifier = Input.Key("0x0")
        adapter._window_mapped = True
        assert adapter.push_input_image(controller, identifier, object()) is True, (
            "push was refused despite a bound, mapped grid"
        )
        assert len(new_grid.buttons[0][0].images) == 1, "the push did not reach the button"

        adapter.unbind(controller)
        assert adapter.push_input_image(controller, identifier, object()) is False, (
            "push was accepted for an unbound controller -- the frame would "
            "be silently lost instead of dirty-marked"
        )
        assert len(new_grid.buttons[0][0].images) == 1, "an unbound push still painted"

        print("PASS: identity binding resolves, rejects strangers, heals re-binds, gates pushes")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)

    print("PASS: scenario_ui_child_identity_binding")


if __name__ == "__main__":
    main()
