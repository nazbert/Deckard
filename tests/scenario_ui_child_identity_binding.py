"""
A controller's UI child must be resolved by object identity.

A lookup that re-reads the device serial and matches it against the
stack-child name misses forever once the two disagree, and the preview push
then dirty-marks instead of painting. GtkUIAdapter binds by object at
add_page and unbinds at remove_page. The fakes carry no name information.
"""
import time
from types import SimpleNamespace

import fixtures

import globals as gl
from gi.repository import GLib

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input
from src.windows.ui_adapter import GtkUIAdapter


def _pump(duration: float = 0.1) -> None:
    """Run the default MainContext's pending idles -- the adapter queues its
    stack mutations with GLib.idle_add and this harness runs no main loop
    (precedent. Scenario_offmain_ui_construction.py)."""
    context = GLib.MainContext.default()
    deadline = time.time() + duration
    while time.time() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


class _FakeStack:
    def __init__(self, children):
        # A None in `children` models GTK's ListModel race. Iteration
        # snapshots len once, so pages removed mid-scan yield None for
        # trailing indices.
        self._pages = [
            None if c is None else SimpleNamespace(get_child=lambda c=c: c)
            for c in children
        ]
        self.added = []
        self.removed = []

    def get_pages(self):
        return list(self._pages)

    def get_child_by_name(self, name):
        # Name lookups must cleanly MISS on these name-free fakes. A
        # name-based reimplementation of the lookup should fail the
        # meaningful assertions below, not crash on a missing API.
        return None

    def add_page(self, controller):
        self.added.append(controller)

    def remove_page(self, controller):
        self.removed.append(controller)


class _FakeButton:
    """The two halves the adapter drives. Conversion on the producer, paint on
    the main loop."""

    def __init__(self):
        self.prepared = []
        self.painted = []

    def prepare_mirror_frame(self, image):
        self.prepared.append(image)
        return image

    def paint_mirror_frame(self, payload):
        self.painted.append(payload)
        return False


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
    # get_mapped/connect are what attach_window() needs; without them it
    # would take its except branch and skip nothing else, but the real path
    # is worth exercising.
    return SimpleNamespace(
        leftArea=SimpleNamespace(deck_stack=deck_stack),
        get_mapped=lambda: False,
        connect=lambda *args: None,
    )


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

        # 2. Cold resolution. Attach_window's rescan binds by IDENTITY. The
        # fakes have no serial/name anywhere, so any name-based matching would
        # come up empty here (the pre-fix failure mode. Previews then only
        # dirty-mark, forever).
        adapter.unbind(controller)
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None
        adapter._window = _fake_window(_FakeStack([child]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is child, (
            "the identity rescan failed to find the controller's own child "
            "-- previews would silently stop reaching the visible grid"
        )

        # 3. No false positive. A stack of other controllers' children.
        adapter.unbind(controller)
        stranger = _fake_child(object(), _fake_grid())
        adapter._window = _fake_window(_FakeStack([stranger]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None, (
            "the rescan matched a child belonging to another controller"
        )

        # 3b. Mid-scan stack mutation. Trailing None pages (ListModel len
        # snapshot after a main-thread removal) must terminate the scan
        # cleanly, not raise.
        adapter._window = _fake_window(_FakeStack([stranger, None]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None, (
            "a scan over a mutating stack did not terminate cleanly"
        )

        # 4. Widget-tree replacement heals via re-binding (what add_page does
        # on a rebuilt window). The adapter must serve the NEW
        # grid, never the orphaned old one.
        new_grid = _fake_grid()
        new_child = _fake_child(controller, new_grid)
        adapter.bind(controller, new_child)
        assert adapter.query_deck_widget(controller, "key_grid") is new_grid, (
            "a stale binding survived the re-bind -- pushes would land in the "
            "orphaned old widget tree"
        )

        # 5. The whole point of the binding. A mirror push reaches the bound
        # grid's button, and reaches NOTHING once unbound (-> the engine
        # dirty-marks instead, which is what load_from_changes replays).
        identifier = Input.Key("0x0")
        adapter._window_mapped = True
        assert adapter.push_input_image(controller, identifier, object()) is True, (
            "push was refused despite a bound, mapped grid"
        )
        assert len(new_grid.buttons[0][0].prepared) == 1, "the push did not reach the button"
        # The paint resolves the widget again, so it has to land on the bound
        # grid too -- not on the orphan the push happened to see.
        assert adapter._drain_mirror(controller, identifier) is False
        assert len(new_grid.buttons[0][0].painted) == 1, (
            "the drain did not paint into the bound grid's button"
        )

        adapter.unbind(controller)
        assert adapter.push_input_image(controller, identifier, object()) is False, (
            "push was accepted for an unbound controller -- the frame would "
            "be silently lost instead of dirty-marked"
        )
        assert len(new_grid.buttons[0][0].prepared) == 1, "an unbound push still reached the button"

        # 6. Hotplug DURING MainWindow construction. The adapter
        # is installed before the constructor but `_window` is only set by
        # attach_window() after it, so on_deck_added/on_deck_removed are
        # no-ops for the whole build. A deck the USB monitor registered in
        # that window would get no stack child at all (rescan only re-binds
        # children that already exist), and one it dropped would leave a
        # stale child behind. attach_window must reconcile BOTH directions.
        assert controller in gl.deck_manager.deck_controller, "fixture invariant"

        # 6a. Missed add. The deck is live but the stack was built without it.
        # detach_window() first, to drop the strangers sections 3/3b bound.
        adapter.detach_window()
        stack = _FakeStack([])
        adapter.attach_window(_fake_window(stack))
        _pump()
        assert stack.added == [controller], (
            f"attach_window queued add_page for {stack.added}, expected the "
            "one live-but-unbound controller -- a deck plugged in during "
            "window construction would never get a stack child"
        )
        assert stack.removed == [], "attach_window removed a live deck's page"

        # 6b. Stale child. Bound to a controller the deck manager no longer
        # knows (unplugged during construction) -> remove + unbind.
        ghost = object()
        ghost_child = _fake_child(ghost, _fake_grid())
        stack = _FakeStack([ghost_child])
        adapter.attach_window(_fake_window(stack))
        _pump()
        assert stack.removed == [ghost], (
            f"attach_window queued remove_page for {stack.removed}, expected "
            "the stale child of an unplugged deck"
        )
        assert adapter.query_deck_widget(ghost, "deck_stack_child") is None, (
            "the stale binding survived the reconcile"
        )

        # 6b'. No deck manager at all. Reconciling against an unknown world
        # must be a no-op, NOT "no decks exist, remove everything".
        settled_child = _fake_child(controller, _fake_grid())
        stack = _FakeStack([settled_child])
        real_deck_manager = gl.deck_manager
        gl.deck_manager = None
        try:
            adapter.attach_window(_fake_window(stack))
            _pump()
        finally:
            gl.deck_manager = real_deck_manager
        assert stack.removed == [], (
            "reconcile tore down bound children when it could not see a deck "
            f"manager: {stack.removed}"
        )

        # 6c. Steady state. A stack that already matches the deck manager
        # must produce no churn at all (attach_window runs on every window
        # rebuild).
        settled_child = _fake_child(controller, _fake_grid())
        stack = _FakeStack([settled_child])
        adapter.attach_window(_fake_window(stack))
        _pump()
        assert stack.added == [] and stack.removed == [], (
            f"a settled stack was churned: added={stack.added} "
            f"removed={stack.removed}"
        )

        print("PASS: identity binding resolves, rejects strangers, heals re-binds, gates pushes")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)

    print("PASS: scenario_ui_child_identity_binding")


if __name__ == "__main__":
    main()
