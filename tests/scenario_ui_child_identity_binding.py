"""
Regression test (issue #156): the controller must find its DeckStackChild by
OBJECT IDENTITY, never by matching a fresh device serial read against the
stack-child name.

Field incident 2026-07-16/17: the child was registered under one serial
string while later lookups re-read the serial from the device -- when the two
disagreed (dual-instance USB contention at boot; or the whole window replaced,
issue #158), get_own_deck_stack_child() missed forever, get_own_key_grid()
returned None forever, and set_ui_key_image silently dirty-marked instead of
pushing to the visible grid: the app window only repainted on re-open.

Headless tier: gl.app is faked with plain namespaces -- the lookup only needs
`gl.app.main_win.leftArea.deck_stack.get_pages()` and children exposing
`.deck_controller` / `.page_settings.deck_config.grid`, so no GTK is
involved. The fakes deliberately carry NO name information at all, proving
the resolution path no longer depends on it.
"""
from types import SimpleNamespace

import fixtures
import globals as gl


class _FakeStack:
    def __init__(self, children):
        self._pages = [SimpleNamespace(get_child=lambda c=c: c) for c in children]

    def get_pages(self):
        return list(self._pages)


def _fake_child(controller, grid):
    return SimpleNamespace(
        deck_controller=controller,
        page_settings=SimpleNamespace(deck_config=SimpleNamespace(grid=grid)),
        # The controller's FPS-warning path (DeckController.set_fps_warning)
        # pokes this on whatever child it resolves -- give it a harmless sink
        # so a background tick during the test can't log noise.
        low_fps_banner=SimpleNamespace(set_revealed=lambda *_: None),
    )


def _install_fake_app(deck_stack):
    gl.app = SimpleNamespace(
        main_win=SimpleNamespace(leftArea=SimpleNamespace(deck_stack=deck_stack))
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_ui_child_identity_binding")
    controller = fixtures.make_headless_controller(serial="identity-1")
    saved_app = getattr(gl, "app", None)
    try:
        grid = object()
        child = _fake_child(controller, grid)
        _install_fake_app(_FakeStack([child]))

        # 1. Pre-bound ref (what add_page now does) short-circuits the scan.
        controller.own_deck_stack_child = child
        controller.own_key_grid = None
        assert controller.get_own_deck_stack_child() is child, (
            "pre-bound own_deck_stack_child ref was not honored"
        )

        # 2. Cold lookup resolves by identity -- the fakes have no serial/name
        # anywhere, so any name-based matching would return None here (the
        # pre-fix failure mode: previews then only dirty-mark, forever).
        controller.own_deck_stack_child = None
        controller.own_key_grid = None
        assert controller.get_own_deck_stack_child() is child, (
            "identity scan failed to find the controller's own DeckStackChild "
            "-- previews would silently stop reaching the visible grid"
        )
        assert controller.own_deck_stack_child is child, "lookup result was not cached"

        # 3. The full grid-resolution chain previews use.
        assert controller.get_own_key_grid() is grid, (
            "get_own_key_grid did not resolve through the identity-bound child"
        )

        # 4. No false positive: a stack of other controllers' children.
        controller.own_deck_stack_child = None
        controller.own_key_grid = None
        stranger = _fake_child(object(), object())
        _install_fake_app(_FakeStack([stranger]))
        assert controller.get_own_deck_stack_child() is None, (
            "identity scan matched a child belonging to another controller"
        )

        # 5. Widget-tree replacement heals via re-binding (what add_page does
        # on a rebuilt window): the controller must serve the NEW grid, not
        # the cached old one.
        new_grid = object()
        new_child = _fake_child(controller, new_grid)
        _install_fake_app(_FakeStack([new_child]))
        controller.own_deck_stack_child = new_child
        controller.own_key_grid = None
        assert controller.get_own_key_grid() is new_grid, (
            "grid cache survived a re-bind -- pushes would land in the "
            "orphaned old widget tree"
        )

        print("PASS: identity binding resolves, caches, rejects strangers, and heals re-binds")
    finally:
        gl.app = saved_app
        fixtures.teardown(controller)

    print("PASS: scenario_ui_child_identity_binding")


if __name__ == "__main__":
    main()
