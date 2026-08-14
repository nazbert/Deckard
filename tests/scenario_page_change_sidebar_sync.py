"""
The page-change UI sync refreshes the sidebar.

GtkUIAdapter.on_page_changed holds the logic and the engine calls the port. The
fake DeckStackChild carries no settings_page, which is the real
PageSettingsPage shape.
"""

# No widget is constructed here, and the harness pumps the default MainContext
# by hand so the coalescer stays observable.
import time
from types import SimpleNamespace

import fixtures

from gi.repository import GLib

from src.backend import ui_port
from src.windows.ui_adapter import GtkUIAdapter


def _pump(duration: float = 0.1) -> None:
    context = GLib.MainContext.default()
    deadline = time.time() + duration
    while time.time() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


class _Recorder:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def _fake_window(visible_child, sidebar_update, subview_active=False):
    deck_stack = SimpleNamespace(get_visible_child=lambda: visible_child)
    configurator_stack = object()
    subview = object()  # stands in for ActionChooser / ActionConfigurator
    main_stack = SimpleNamespace(
        get_visible_child=lambda: subview if subview_active else configurator_stack
    )
    return SimpleNamespace(
        leftArea=SimpleNamespace(deck_stack=deck_stack),
        sidebar=SimpleNamespace(
            update=sidebar_update,
            main_stack=main_stack,
            configurator_stack=configurator_stack,
        ),
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_page_change_sidebar_sync")
    controller = fixtures.make_headless_controller(serial="sidebar-sync-1")
    adapter = GtkUIAdapter()
    ui_port.install(adapter)
    try:
        # The real PageSettingsPage shape has page_settings and no
        # settings_page.
        # The media thread's FPS-warning path pokes the bound child, so
        # low_fps_banner needs a sink to keep background ticks quiet.
        own_child = SimpleNamespace(
            deck_controller=controller,
            page_settings=SimpleNamespace(deck_config=SimpleNamespace(grid=object())),
            low_fps_banner=SimpleNamespace(set_revealed=lambda *_: None),
        )
        adapter.bind(controller, own_child)

        # The fixture's initial page load queued one sync through the port.
        # Drain it while no window is attached, so the counts below start at
        # zero.
        assert adapter._window is None
        _pump()

        # 1. The visible deck changed page, so the sidebar refreshes.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=own_child, sidebar_update=update)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 1, (
            f"sidebar.update ran {update.calls} times, expected 1 -- the "
            "page-change UI sync is dead again"
        )

        # 2. Another deck is visible. The sidebar mirrors the visible deck's
        # selection, so it must not reload.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=SimpleNamespace(),
                                       sidebar_update=update)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 0, (
            "sidebar.update ran for a background deck's page change"
        )

        # 3. A sidebar sub-view is up. The refresh must not pull the user
        # out of it.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=own_child, sidebar_update=update,
                                       subview_active=True)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 0, (
            "sidebar.update fired while a sub-view was active -- a page "
            "change would snap the user out of mid-edit"
        )

        # 4. Many coalesced triggers arm one idle and give one refresh.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=own_child, sidebar_update=update)
        ui_port.get().on_page_changed(controller)
        ui_port.get().on_page_changed(controller)
        ui_port.get().on_page_changed(controller)
        assert adapter._page_sync_queued.get(controller) is True, "queue flag not set"
        _pump()
        assert update.calls == 1, (
            f"3 queued triggers produced {update.calls} refreshes, expected "
            "1 -- coalescing broken"
        )
        # The flag is popped, not written back as False. Re-inserting the key
        # after an interleaved unbind() pins the whole DeckController graph of
        # an unplugged deck.
        assert not adapter._page_sync_queued.get(controller), "queue flag not cleared"

        # 5. An unbound controller gets no refresh and no crash. A None child
        # must not match a None visible child.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=None, sidebar_update=update)
        adapter.unbind(controller)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 0, "refresh ran for a controller with no bound child"

        # 6. With no UI the call is a no-op, and the engine's call path still
        # runs through the null port.
        ui_port.install(None)
        ui_port.get().on_page_changed(controller)
        _pump()

        print("PASS: sidebar refreshes for the visible deck only, skips sub-views, coalesces")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)

    print("PASS: scenario_page_change_sidebar_sync")


if __name__ == "__main__":
    main()
