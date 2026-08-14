"""Regression checks for the grouped low-severity app-shell findings.

Every check drives a real method unbound on plain stub objects, so no GTK
widget is built and no display is needed.
"""
import fixtures  # noqa: F401  (must be first: isolates the data dir)

import inspect

import globals as gl


class Obj:
    """Attribute bag."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class ExplodingModel:
    """Sentinel for pages_model. Any indexing fails the test."""
    def __getitem__(self, item):
        raise AssertionError(
            f"pages_model must not be indexed when get_active() == -1 "
            f"(got index {item!r})"
        )


def check_on_activate_defers_show_donate() -> None:
    import src.app as app_mod

    source = inspect.getsource(app_mod.App.on_activate)
    assert "on_finished.append" not in source, (
        "on_activate must not append to MainWindow.on_finished: that list is "
        "drained synchronously inside the MainWindow constructor, so the "
        "append is dead code (and the old form appended show_donate()'s "
        "None result while calling it as a side effect)"
    )
    assert "self.show_donate()" in source, (
        "on_activate must still invoke show_donate directly"
    )
    print("  PASS: on_activate calls show_donate directly (no dead append)")


def check_hide_error_targets_stack_child() -> None:
    from src.windows.mainWindow.elements.Sidebar.Sidebar import Sidebar

    error_page = object()
    key_editor = object()
    configurator_stack = object()
    set_calls = []
    main_stack = Obj(
        get_visible_child=lambda: error_page,
        set_visible_child=lambda child: set_calls.append(child),
        set_transition_duration=lambda ms: None,
    )
    stub = Obj(
        main_stack=main_stack,
        error_page=error_page,
        key_editor=key_editor,
        configurator_stack=configurator_stack,
    )

    Sidebar.hide_error(stub)

    assert set_calls == [configurator_stack], (
        f"hide_error must switch main_stack to configurator_stack (its real "
        f"child), got {set_calls!r} (key_editor is a child of "
        f"configurator_stack, not of main_stack)"
    )
    print("  PASS: Sidebar.hide_error targets configurator_stack")


def check_deck_name_dedup() -> None:
    from src.windows.mainWindow.elements.DeckStack import DeckStack

    stub = Obj(deck_attributes={}, deck_names=[], deck_numbers=[])

    def make_controller(serial):
        deck = Obj(
            deck_type=lambda: "Stream Deck MK.2",
            get_serial_number=lambda: serial,
        )
        # get_page_attributes reads the cached serial accessor of the
        # controller, not the device.
        return Obj(deck=deck, serial_number=lambda: serial)

    _, first = DeckStack.get_page_attributes(stub, make_controller("SN1"))
    _, second = DeckStack.get_page_attributes(stub, make_controller("SN2"))
    _, third = DeckStack.get_page_attributes(stub, make_controller("SN3"))

    assert first == "Stream Deck MK.2", first
    assert second == "Stream Deck MK.2 (2)", (
        f"a second identical deck must get a '(2)' suffix, not a mutated "
        f"model name -- got {second!r}"
    )
    assert third == "Stream Deck MK.2 (3)", third
    print("  PASS: deck-name dedup suffixes '(n)' instead of renaming the model")


def check_page_selector_negative_index_guard() -> None:
    from src.windows.mainWindow.elements.PageSelector import PageSelector

    load_page = Recorder()
    controller = Obj(active_page=None, load_page=load_page)
    deck_stack = Obj(get_visible_child=lambda: Obj(deck_controller=controller))
    stub = Obj(
        main_window=Obj(leftArea=Obj(deck_stack=deck_stack)),
        pages_model=ExplodingModel(),
    )
    drop_down = Obj(get_active=lambda: -1)

    PageSelector.on_change_page(stub, drop_down)
    assert load_page.calls == [], (
        "get_active() == -1 must be a no-op, not a load of the last page"
    )

    # Same guard on the page-settings button path.
    stub2 = Obj(
        drop_down=Obj(get_active=lambda: -1),
        pages_model=ExplodingModel(),
        on_click_open_page_manager=Recorder(),
    )
    PageSelector.on_click_open_page_settings(stub2, button=None)
    print("  PASS: PageSelector guards get_active() == -1 on both paths")


def check_deck_manager_usb_callback_guards() -> None:
    import src.backend.DeckManagement.DeckManager as dm_mod

    saved_app = gl.app

    # A disconnect event before the UI exists must not raise.
    gl.app = None
    stub = Obj(deck_controller=[])
    try:
        dm_mod.DeckManager.on_disconnect(
            stub, "dev", {"ID_VENDOR_ID": dm_mod.ELGATO_VENDOR_ID}
        )
    except AttributeError as e:
        raise AssertionError(
            f"on_disconnect must be guarded against a missing gl.app / "
            f"main_win: {e}"
        )
    finally:
        gl.app = saved_app

    # add_newly_connected_deck must reach the call that re-checks whether any
    # deck is available. A trailing dot in recursive_hasattr(gl, "app.main_win.")
    # always reads False and skips it. The call goes through a port, not a direct
    # main_win poke, so the recorder is a port.
    from src.backend import ui_port

    class _RecordingPort(ui_port.UIPort):
        def __init__(self):
            self.added = []
            self.availability_refreshes = 0
            self.page_list_changes = 0

        def on_deck_added(self, controller):
            self.added.append(controller)

        def refresh_deck_availability(self):
            self.availability_refreshes += 1

        def on_page_list_changed(self):
            self.page_list_changes += 1

    port = _RecordingPort()
    ui_port.install(port)
    saved_ctor = dm_mod.DeckController
    dm_mod.DeckController = lambda manager, deck: Obj(deck=deck)
    try:
        # DeckManager wraps the controller construction in
        # _init_deck_controller_with_retry(). Stub it, so the method reaches the
        # availability refresh this test verifies.
        stub = Obj(
            deck_controller=[],
            fake_deck_controller=[],
            _init_deck_controller_with_retry=lambda deck: Obj(deck=deck),
        )
        dm_mod.DeckManager.add_newly_connected_deck(stub, deck=Obj())
    finally:
        dm_mod.DeckController = saved_ctor
        ui_port.install(None)
        gl.app = saved_app

    assert port.availability_refreshes == 1, (
        "add_newly_connected_deck must refresh deck availability when the UI "
        "exists -- the trailing-dot recursive_hasattr typo made this dead code"
    )
    assert len(port.added) == 1, "the new deck was never announced to the UI"
    assert port.page_list_changes == 1, "the page selector was never refreshed"
    print("  PASS: DeckManager USB callbacks guarded; typo'd guard is live again")


def check_deck_group_active_page_guards() -> None:
    from src.windows.mainWindow.elements.DeckSettings.DeckGroup import (
        Brightness,
        Screensaver,
    )

    saved_sm = getattr(gl, "settings_manager", None)
    gl.settings_manager = fixtures.StubSettingsManager()
    try:
        set_brightness = Recorder()
        controller = Obj(active_page=None, set_brightness=set_brightness)
        stub = Obj(
            deck_serial_number="SN1",
            settings_page=Obj(deck_controller=controller),
        )
        Brightness.on_value_changed_idle(stub, Obj(get_value=lambda: 42))
        assert set_brightness.calls == [((42,), {})], (
            f"with no active page nothing can overwrite brightness -- the "
            f"slider value must be applied, got {set_brightness.calls!r}"
        )

        stub2 = Obj(settings_page=Obj(deck_controller=Obj(active_page=None)))
        assert Screensaver.page_overwrites_screensaver(stub2) is False, (
            "no active page means 'not overwritten'"
        )

        # update_image is the screensaver asset picker callback. It must not
        # reload the screensaver against a None active_page, because
        # load_screensaver dereferences page.dict and would raise.
        load_screensaver = Recorder()
        controller3 = Obj(active_page=None, load_screensaver=load_screensaver)
        stub3 = Obj(
            deck_serial_number="SN1",
            set_thumbnail=Recorder(),
            settings_page=Obj(deck_controller=controller3),
        )
        Screensaver.update_image(stub3, "/some/screensaver.png")
        assert load_screensaver.calls == [], (
            "with no active page there is nothing to reload the screensaver "
            f"against -- load_screensaver must be skipped, got "
            f"{load_screensaver.calls!r}"
        )
    finally:
        gl.settings_manager = saved_sm
    print("  PASS: Brightness/Screensaver rows tolerate active_page=None")


def check_udev_probe_spawn_form() -> None:
    import src.windows.Onboarding.OnboardingWindow as ow_mod

    recorded = []

    def fake_check_output(command):
        recorded.append(command)
        return b"252 (252.19-1~deb12u1)\n"

    saved_subprocess = ow_mod.subprocess
    saved_is_flatpak = ow_mod.is_flatpak
    ow_mod.subprocess = Obj(
        check_output=fake_check_output,
        CalledProcessError=saved_subprocess.CalledProcessError,
    )
    try:
        ow_mod.is_flatpak = lambda: True
        version = ow_mod.OnboardingWindow.get_udev_version(Obj())
        assert recorded[-1] == ["flatpak-spawn", "--host", "udevadm", "--version"], (
            f"inside flatpak the probe must run udevadm on the HOST via "
            f"flatpak-spawn, got {recorded[-1]!r}"
        )
        assert version == "252", (
            f"only the leading version token may be returned (version.parse "
            f"chokes on build suffixes), got {version!r}"
        )

        ow_mod.is_flatpak = lambda: False
        ow_mod.OnboardingWindow.get_udev_version(Obj())
        assert recorded[-1] == ["udevadm", "--version"], recorded[-1]
    finally:
        ow_mod.subprocess = saved_subprocess
        ow_mod.is_flatpak = saved_is_flatpak
    print("  PASS: udev probe uses flatpak-spawn --host and a parseable token")


def check_asset_manager_drops_callback_refs() -> None:
    from src.windows.AssetManager.AssetManager import AssetManager

    action = Obj(name="opener-action")  # stands in for the pinned action/page
    callback = Recorder()
    stub = Obj(
        callback_func=callback,
        callback_args=(action,),
        callback_kwargs={"k": action},
        hide=Recorder(),
    )

    AssetManager.deliver_selection(stub, "/some/asset.png")

    assert callback.calls == [(("/some/asset.png", action), {"k": action})], (
        f"the selection callback must still be delivered, got {callback.calls!r}"
    )
    assert stub.callback_func is None, (
        "the hidden singleton must not pin the selection callback after "
        "delivery"
    )
    assert stub.callback_args == () and stub.callback_kwargs == {}, (
        f"callback args/kwargs must be dropped too, got "
        f"{stub.callback_args!r} / {stub.callback_kwargs!r}"
    )
    assert len(stub.hide.calls) == 1
    print("  PASS: AssetManager drops callback refs at delivery")


def check_flowbox_drops_callback_refs() -> None:
    # The second delivery path is CustomAssets/FlowBox.on_child_activated. It
    # captures the callback, then nulls the manager refs before it spawns the
    # delivery thread. Stub threading.Thread, so the capture is synchronous and
    # no real thread runs.
    import src.windows.AssetManager.CustomAssets.FlowBox as fb_mod

    action = Obj(name="opener-action")  # stands in for the pinned action/page
    callback = Recorder()
    asset_manager = Obj(
        callback_func=callback,
        callback_args=(action,),
        callback_kwargs={"k": action},
        hide=Recorder(),
    )
    stub = Obj(
        asset_chooser=Obj(asset_manager=asset_manager),
        callback_thread=Recorder(),  # thread target; captured, never run here
    )
    child = Obj(asset={"internal-path": "/some/custom.png"})

    captured = {}

    class FakeThread:
        def __init__(self, target=None, args=(), name=None):
            captured["target"] = target
            captured["args"] = args

        def start(self):
            captured["started"] = True

    saved_thread = fb_mod.threading.Thread
    fb_mod.threading.Thread = FakeThread
    try:
        fb_mod.CustomAssetChooserFlowBox.on_child_activated(stub, None, child)
    finally:
        fb_mod.threading.Thread = saved_thread

    # The manager must drop the refs the moment delivery is dispatched.
    assert asset_manager.callback_func is None, (
        "the hidden singleton must not pin the selection callback after the "
        "custom-asset FlowBox delivers"
    )
    assert asset_manager.callback_args == () and asset_manager.callback_kwargs == {}, (
        f"callback args/kwargs must be dropped too, got "
        f"{asset_manager.callback_args!r} / {asset_manager.callback_kwargs!r}"
    )
    # The captured thread must still carry the real callback, path and args.
    assert captured.get("started") is True, "delivery thread must be started"
    assert captured["args"] == (
        "/some/custom.png", callback, (action,), {"k": action}
    ), (
        f"the captured callback/path/args must survive the null-out, got "
        f"{captured.get('args')!r}"
    )
    assert len(asset_manager.hide.calls) == 1
    print("  PASS: CustomAssets FlowBox drops callback refs at delivery")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_appshell_lows")
    check_on_activate_defers_show_donate()
    check_hide_error_targets_stack_child()
    check_deck_name_dedup()
    check_page_selector_negative_index_guard()
    check_deck_manager_usb_callback_guards()
    check_deck_group_active_page_guards()
    check_udev_probe_spawn_form()
    check_asset_manager_drops_callback_refs()
    check_flowbox_drops_callback_refs()
    print("PASS: scenario_appshell_lows")


if __name__ == "__main__":
    main()
