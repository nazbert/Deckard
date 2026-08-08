"""
Regression test (issue #157): the page-change UI sync must actually refresh
the sidebar.

The original body resolved `...page_settings.settings_page` -- an attribute
that never existed after the settings restructure -- so it raised
AttributeError on EVERY page change, a blanket except mislabeled it as
first-deck noise, and the sidebar update (the only still-meaningful piece)
never ran.

Since #141 the logic lives in `GtkUIAdapter.on_page_changed`, not in
DeckController: the engine just calls the port. The assertions are unchanged
in substance -- the visible-deck guard, the sub-view guard, the coalescer --
and the fake DeckStackChild still deliberately has NO settings_page anywhere
(the real PageSettingsPage shape), so any revival of the dead accessor chain
fails this test.

Headless tier: gl.app / the "window" are plain namespaces and no widget is
ever constructed, so importing the adapter (and hence Gtk) without a display
is fine. The adapter wraps its work in GLib idles and this harness runs no
main loop, so the default MainContext is pumped manually -- which keeps the
coalescer observable (the precedent is scenario_offmain_ui_construction.py).
"""
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
        # Real PageSettingsPage shape: page_settings WITHOUT settings_page.
        # low_fps_banner: the media thread's FPS-warning path pokes whatever
        # child is bound -- give it a sink so background ticks stay quiet.
        own_child = SimpleNamespace(
            deck_controller=controller,
            page_settings=SimpleNamespace(deck_config=SimpleNamespace(grid=object())),
            low_fps_banner=SimpleNamespace(set_revealed=lambda *_: None),
        )
        adapter.bind(controller, own_child)

        # The fixture's own initial page load already queued one sync through
        # the port. Drain it while no window is attached so the counts below
        # are ours alone.
        assert adapter._window is None
        _pump()

        # 1. Visible deck's page changed -> sidebar refreshes. This is the
        # regression: the pre-fix body died on the dead accessor chain first.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=own_child, sidebar_update=update)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 1, (
            f"sidebar.update ran {update.calls} times, expected 1 -- the "
            "page-change UI sync is dead again"
        )

        # 2. Another deck is visible -> the sidebar (which mirrors the
        # visible deck's selection) must NOT be reloaded.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=SimpleNamespace(),
                                       sidebar_update=update)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 0, (
            "sidebar.update ran for a background deck's page change"
        )

        # 3. A sidebar sub-view (ActionChooser/ActionConfigurator) is up ->
        # the refresh must not yank the user out of it.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=own_child, sidebar_update=update,
                                       subview_active=True)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 0, (
            "sidebar.update fired while a sub-view was active -- a page "
            "change would snap the user out of mid-edit"
        )

        # 4. Coalescing: N completion triggers -> one pending idle -> one
        # refresh when it runs.
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
        # Cleared means "not set": the flag is POPPED, not written back to
        # False -- re-inserting the key after an interleaved unbind() would
        # pin the whole DeckController graph of an unplugged deck.
        assert not adapter._page_sync_queued.get(controller), "queue flag not cleared"

        # 5. An unbound controller (window gone / deck detached) -> no refresh,
        # no crash. The None-child must not match a None visible child.
        update = _Recorder()
        adapter._window = _fake_window(visible_child=None, sidebar_update=update)
        adapter.unbind(controller)
        ui_port.get().on_page_changed(controller)
        _pump()
        assert update.calls == 0, "refresh ran for a controller with no bound child"

        # 6. No UI at all (headless / early boot) -> silent no-op, and the
        # engine's own call path stays exercised through the null port.
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
