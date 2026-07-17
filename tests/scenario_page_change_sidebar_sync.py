"""
Regression test (issue #157): update_ui_on_page_change must actually refresh
the sidebar.

The old body resolved `...page_settings.settings_page` -- an attribute that
never existed after the settings restructure -- so it raised AttributeError
on EVERY page change, a blanket except mislabeled it as first-deck noise,
and the sidebar update (the only still-meaningful piece) never ran.

Headless tier, same fake-namespace approach as
scenario_ui_child_identity_binding: the fake DeckStackChild deliberately has
NO settings_page anywhere (the real PageSettingsPage shape), so any revival
of the dead accessor chain fails this test. Also pins the review-round
guards: no refresh while a sidebar sub-view (ActionChooser/Configurator) is
up, and the _queue_ui_page_sync coalescer.
"""
from types import SimpleNamespace

import fixtures
import globals as gl


class _Recorder:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def _fake_app(visible_child, sidebar_update, subview_active=False):
    deck_stack = SimpleNamespace(get_visible_child=lambda: visible_child)
    configurator_stack = object()
    subview = object()  # stands in for ActionChooser / ActionConfigurator
    main_stack = SimpleNamespace(
        get_visible_child=lambda: subview if subview_active else configurator_stack
    )
    return SimpleNamespace(
        main_win=SimpleNamespace(
            leftArea=SimpleNamespace(deck_stack=deck_stack),
            sidebar=SimpleNamespace(
                update=sidebar_update,
                main_stack=main_stack,
                configurator_stack=configurator_stack,
            ),
        )
    )


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_page_change_sidebar_sync")
    controller = fixtures.make_headless_controller(serial="sidebar-sync-1")
    saved_app = getattr(gl, "app", None)
    try:
        # Real PageSettingsPage shape: page_settings WITHOUT settings_page.
        # low_fps_banner: the media thread's FPS-warning path pokes whatever
        # child is bound -- give it a sink so background ticks stay quiet.
        own_child = SimpleNamespace(
            deck_controller=controller,
            page_settings=SimpleNamespace(deck_config=SimpleNamespace(grid=object())),
            low_fps_banner=SimpleNamespace(set_revealed=lambda *_: None),
        )
        controller.own_deck_stack_child = own_child

        # 1. Visible deck's page changed -> sidebar refreshes. This is the
        # regression: the pre-fix body died on the dead accessor chain first.
        update = _Recorder()
        gl.app = _fake_app(visible_child=own_child, sidebar_update=update)
        controller.update_ui_on_page_change()
        assert update.calls == 1, (
            f"sidebar.update ran {update.calls} times, expected 1 -- the "
            "page-change UI sync is dead again"
        )

        # 2. Another deck is visible -> the sidebar (which mirrors the
        # visible deck's selection) must NOT be reloaded.
        update = _Recorder()
        gl.app = _fake_app(visible_child=SimpleNamespace(), sidebar_update=update)
        controller.update_ui_on_page_change()
        assert update.calls == 0, (
            "sidebar.update ran for a background deck's page change"
        )

        # 3. A sidebar sub-view (ActionChooser/ActionConfigurator) is up ->
        # the refresh must not yank the user out of it.
        update = _Recorder()
        gl.app = _fake_app(visible_child=own_child, sidebar_update=update,
                           subview_active=True)
        controller.update_ui_on_page_change()
        assert update.calls == 0, (
            "sidebar.update fired while a sub-view was active -- a page "
            "change would snap the user out of mid-edit"
        )

        # 4. Coalescing: N completion triggers -> one pending callback ->
        # one refresh when it runs.
        update = _Recorder()
        gl.app = _fake_app(visible_child=own_child, sidebar_update=update)
        controller._ui_page_sync_queued = False
        controller._queue_ui_page_sync()
        controller._queue_ui_page_sync()
        controller._queue_ui_page_sync()
        assert controller._ui_page_sync_queued is True, "queue flag not set"
        controller._run_ui_page_sync()  # what the single queued idle would do
        assert update.calls == 1, (
            f"3 queued triggers produced {update.calls} refreshes, expected "
            "1 -- coalescing broken"
        )
        assert controller._ui_page_sync_queued is False, "queue flag not cleared"

        # 5. No UI at all (headless / early boot) -> silent no-op.
        gl.app = None
        controller.update_ui_on_page_change()

        print("PASS: sidebar refreshes for the visible deck only, skips sub-views, coalesces")
    finally:
        gl.app = saved_app
        controller.own_deck_stack_child = None
        fixtures.teardown(controller)

    print("PASS: scenario_page_change_sidebar_sync")


if __name__ == "__main__":
    main()
