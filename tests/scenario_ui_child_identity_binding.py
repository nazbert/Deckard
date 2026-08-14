"""
A controller's UI child must be resolved by object identity.

A lookup that re-reads the device serial and matches it against the
stack-child name misses forever once the two disagree, and the preview push
then dirty-marks instead of painting. GtkUIAdapter binds by object.
"""

# The binding lands at DeckStack.add_page and lifts at remove_page. The fakes
# below carry no name information at all, so a name-matching lookup fails.
import time
from types import SimpleNamespace

import fixtures

import globals as gl
from gi.repository import GLib

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input
from src.windows.ui_adapter import GtkUIAdapter


def _pump(duration: float = 0.1) -> None:
    """Run the default MainContext's pending idles. The adapter queues its
    stack mutations with GLib.idle_add and this harness runs no main loop."""
    context = GLib.MainContext.default()
    deadline = time.time() + duration
    while time.time() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


class _FakeStack:
    def __init__(self, children):
        # A None among the children models GTK's ListModel race. Iteration
        # snapshots len once, so a page removed mid-scan yields None for a
        # trailing index.
        self._pages = [
            None if c is None else SimpleNamespace(get_child=lambda c=c: c)
            for c in children
        ]
        self.added = []
        self.removed = []

    def get_pages(self):
        return list(self._pages)

    def get_child_by_name(self, name):
        # A name lookup must miss cleanly on these name-free fakes, so a
        # name-based reimplementation fails the assertions below rather than
        # crashing on a missing API.
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
        # resolves, so a harmless sink keeps a background tick from logging
        # noise.
        low_fps_banner=SimpleNamespace(set_revealed=lambda *_: None),
    )


def _fake_window(deck_stack):
    # attach_window needs get_mapped and connect. Without them it takes its
    # except branch and skips nothing else, and the real path is worth
    # exercising.
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

        # 2. Cold resolution. attach_window's rescan binds by identity. The
        # fakes carry no serial and no name, so name-based matching comes up
        # empty here and every preview then only dirty-marks.
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

        # 3b. Mid-scan stack mutation. A trailing None page, left by a
        # ListModel len snapshot after a main-thread removal, must end the
        # scan cleanly rather than raise.
        adapter._window = _fake_window(_FakeStack([stranger, None]))
        adapter.rescan_children()
        assert adapter.query_deck_widget(controller, "deck_stack_child") is None, (
            "a scan over a mutating stack did not terminate cleanly"
        )

        # 4. A widget-tree replacement heals through the re-bind add_page
        # does on a rebuilt window. The adapter must serve the new grid,
        # never the orphaned old one.
        new_grid = _fake_grid()
        new_child = _fake_child(controller, new_grid)
        adapter.bind(controller, new_child)
        assert adapter.query_deck_widget(controller, "key_grid") is new_grid, (
            "a stale binding survived the re-bind -- pushes would land in the "
            "orphaned old widget tree"
        )

        # 5. A mirror push reaches the bound grid's button, and reaches
        # nothing once unbound, so the engine dirty-marks instead and
        # load_from_changes replays that marker.
        identifier = Input.Key("0x0")
        adapter._window_mapped = True
        assert adapter.push_input_image(controller, identifier, object()) is True, (
            "push was refused despite a bound, mapped grid"
        )
        assert len(new_grid.buttons[0][0].prepared) == 1, "the push did not reach the button"
        # The paint resolves the widget again, so it has to land on the
        # bound grid as well, rather than on the orphan the push saw.
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

        # 6. A hotplug during MainWindow construction. The adapter installs
        # before the constructor and attach_window sets _window after it, so
        # on_deck_added and on_deck_removed do nothing for the whole build. A
        # deck registered in that window gets no stack child, because the
        # rescan only re-binds a child that exists, and a dropped one leaves
        # a stale child. attach_window reconciles in both directions.
        assert controller in gl.deck_manager.deck_controller, "fixture invariant"

        # 6a. A missed add. The deck is live and the stack was built without
        # it. detach_window runs first, to drop the strangers bound above.
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

        # 6b. A stale child, bound to a controller the deck manager does not
        # know, after an unplug during construction. It is removed and
        # unbound.
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
        # must do nothing, rather than read as "no decks exist".
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
        # must produce no churn, because attach_window runs on every window
        # rebuild.
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
