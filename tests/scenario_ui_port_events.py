"""
Scenario: the engine->UI port contract.

Before the port there was no way to observe what the engine wanted to tell a UI
without building a GTK widget tree -- the only headless-visible trace was the
dirty-marker dict, i.e. the "nothing was shown" path. With the port, a test
can attach a RECORDING implementation and assert the positive direction:
which inputs got mirrored, when the sidebar was asked to re-render, and that
an accepting UI means the engine does NOT dirty-mark.

Covers:
  (a) with an accepting port attached, a page load pushes an image for every
      key identifier AND the touchscreen, and ui_image_changes_while_hidden
      stays EMPTY (the exact inverse of scenario_hidden_window_markers).
  (b) on_page_changed fires with the page's actions already initialized --
      the ordering load_page deliberately arranges (the sidebar's
      ActionManager can only render actions that exist).
  (c) install(None) restores the null port and pushes dirty-mark again.
  (d) every port method is callable from a headless process without raising,
      on the null port AND on a GtkUIAdapter with no window attached. This is
      the class of bug that used to be real: update_state_switcher reached
      gl.app.main_win.sidebar unguarded, and FlatpakDeckDisconnectThread
      called check_for_errors() with no window -- both AttributeError crashes
      before the window existed.
  (e) the GtkUIAdapter's mirror slots coalesce: N pushes against a blocked
      main loop cost one paint, carrying the newest frame, with the
      conversion still on the producer.
"""
import os
import time
from types import SimpleNamespace

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)
import globals as gl

from loguru import logger as log

from src.backend import ui_port
from src.backend.DeckManagement.InputIdentifier import Input

WATCHDOG_SECONDS = 60


class RecordingPort(ui_port.UIPort):
    """Accepts every frame and records everything the engine reports."""

    def __init__(self):
        self.pushes: list = []
        self.page_changes: list = []
        self.visuals: list = []
        self.states_changed: list = []
        self.state_selected: list = []
        self.low_fps: list = []
        self.layout_changes: list = []
        self.decks_added: list = []
        self.decks_removed: list = []
        self.availability_refreshes = 0
        self.page_list_changes = 0
        self.plugin_problems: list = []
        # Set by the test: snapshotted whenever on_page_changed fires.
        self.page_change_probe = lambda: None

    def push_input_image(self, controller, identifier, image) -> bool:
        self.pushes.append(identifier)
        return True

    def on_page_changed(self, controller) -> None:
        self.page_changes.append(self.page_change_probe())

    def on_input_visuals_changed(self, controller, identifier, state, aspect) -> None:
        self.visuals.append((identifier, state, aspect))

    def on_input_states_changed(self, controller, identifier, n_states) -> None:
        self.states_changed.append((identifier, n_states))

    def on_input_state_selected(self, controller, identifier, state) -> None:
        self.state_selected.append((identifier, state))

    def set_low_fps_warning(self, controller, shown) -> None:
        self.low_fps.append(shown)

    def on_deck_layout_changed(self, controller) -> None:
        self.layout_changes.append(controller)

    def on_deck_added(self, controller) -> None:
        self.decks_added.append(controller)

    def on_deck_removed(self, controller) -> None:
        self.decks_removed.append(controller)

    def refresh_deck_availability(self) -> None:
        self.availability_refreshes += 1

    def on_page_list_changed(self) -> None:
        self.page_list_changes += 1

    def notify_plugin_problem(self, plugin_id, kind) -> None:
        self.plugin_problems.append((plugin_id, kind))


def check_accepted_pushes_suppress_markers(port) -> None:
    controller = fixtures.make_headless_controller(serial="ui-port-1")
    try:
        keys = controller.inputs[Input.Key]
        touchscreens = controller.inputs[Input.Touchscreen]
        assert keys, "fixture sanity: expected at least one key input"
        assert touchscreens, "fixture sanity: expected a touchscreen input"

        wanted = {i.identifier for i in keys} | {touchscreens[0].identifier}
        assert fixtures.wait_until(
            lambda: wanted <= set(port.pushes), timeout=10), (
            f"the page load did not mirror every input: missing "
            f"{sorted(str(i) for i in wanted - set(port.pushes))}"
        )

        assert controller.ui_image_changes_while_hidden == {}, (
            "the engine dirty-marked even though every push was ACCEPTED -- "
            f"markers: {controller.ui_image_changes_while_hidden!r}"
        )
        print("PASS: an accepting port receives every input and suppresses dirty markers")

        # (c) Detach: pushes must dirty-mark again, exactly as headless does.
        ui_port.install(None)
        keys[0].update(force=True)
        assert fixtures.wait_until(
            lambda: keys[0].identifier in controller.ui_image_changes_while_hidden,
            timeout=5), (
            "with the null port installed the engine did not fall back to "
            "dirty-marking -- the map-time replay would have nothing to show"
        )
        print("PASS: install(None) restores the null port and the dirty-mark fallback")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)


def check_page_change_follows_action_init() -> None:
    latch_cls = fixtures.make_latch_action_class()
    icon_path = fixtures.make_test_png(
        os.path.join(gl.DATA_PATH, "media", "ui_port_icon.png"), color=(0, 180, 0))
    fixtures.install_stub_plugin_manager(latch_cls, icon_path)

    port = RecordingPort()
    ui_port.install(port)
    controller = fixtures.make_headless_controller(serial="ui-port-2")
    try:
        key_ident = controller.inputs[Input.Key][0].identifier.json_identifier
        action_page = gl.page_manager.get_page(
            fixtures.seed_action_page("UiPortActions", key_ident), controller)

        def actions_ready() -> bool:
            for by_ident in action_page.action_objects.values():
                for by_state in by_ident.values():
                    for by_index in by_state.values():
                        for action in by_index.values():
                            if getattr(action, "on_ready_called", False):
                                return True
            return False

        port.page_change_probe = actions_ready
        port.page_changes.clear()
        controller.load_page(action_page, allow_reload=True)

        assert fixtures.wait_until(lambda: any(port.page_changes), timeout=10), (
            f"on_page_changed never fired with the page's actions initialized "
            f"(observed probes: {port.page_changes!r}) -- the sidebar would "
            f"render a page whose action objects don't exist yet"
        )
        print("PASS: on_page_changed reaches the UI with the new page's actions initialized")
    finally:
        ui_port.install(None)
        fixtures.teardown(controller)


class _RaisingButton:
    """A widget whose conversion blows up the way a torn-down/rebuilt one does
    (image2pixbuf on a freed surface, a disposed GtkPicture, ...)."""

    def prepare_mirror_frame(self, image):
        raise RuntimeError("widget tree is being torn down")

    def paint_mirror_frame(self, payload):
        raise RuntimeError("widget tree is being torn down")


class _RecordingMirror:
    """Stands in for a KeyButton / ScreenBarImage: conversion on the producer,
    paint on the main loop, each half recorded separately."""

    def __init__(self):
        self.prepared: list = []
        self.painted: list = []

    def prepare_mirror_frame(self, image):
        self.prepared.append(image)
        return image

    def paint_mirror_frame(self, payload) -> bool:
        self.painted.append(payload)
        return False


class _FakeController:
    """Hashable stand-in that owns the one piece of engine state the adapter
    writes to."""

    def __init__(self):
        self.ui_image_changes_while_hidden: dict = {}


def _fake_key_child(button):
    return SimpleNamespace(
        page_settings=SimpleNamespace(
            deck_config=SimpleNamespace(grid=SimpleNamespace(buttons=[[button]]))
        )
    )


def _fake_mirror_child(button, strip):
    return SimpleNamespace(
        page_settings=SimpleNamespace(
            deck_config=SimpleNamespace(
                grid=SimpleNamespace(buttons=[[button]]),
                screenbar=SimpleNamespace(image=strip),
            )
        )
    )


def check_mirror_pushes_coalesce() -> None:
    """N producer pushes against a blocked main loop must cost ONE paint, and
    that paint must carry the LAST frame.

    The key mirror used to convert and GLib.idle_add per frame: a stalled loop
    accumulated one queued callback and one retained pixbuf per frame per key,
    then painted every superseded frame in turn once it caught up. Both
    mirrors now hand frames to a per-input latest-wins slot, so the backlog is
    one frame per input whatever the producer does.
    """
    from gi.repository import GLib

    from src.windows.ui_adapter import TOUCHSCREEN_UI_INTERVAL_S, GtkUIAdapter

    controller = _FakeController()
    button, strip = _RecordingMirror(), _RecordingMirror()
    adapter = GtkUIAdapter()
    adapter.bind(controller, _fake_mirror_child(button, strip))
    adapter._window_mapped = True

    ctx = GLib.MainContext.default()

    def pump() -> None:
        while ctx.pending():
            ctx.iteration(False)

    # Earlier checks left idles of their own on the default context.
    pump()

    key_ident = Input.Key("0x0")
    frames = [f"key-frame-{i}" for i in range(25)]
    for frame in frames:
        assert adapter.push_input_image(controller, key_ident, frame) is True, (
            "a mirror push was refused with a mapped window and a bound widget"
        )
    assert button.painted == [], (
        "a frame painted while the main loop was blocked -- the paint must be "
        "marshalled onto the loop, never run on the producer"
    )
    assert button.prepared == frames, (
        "the pixbuf conversion did not run once per frame ON THE PRODUCER "
        f"({len(button.prepared)} of {len(frames)}) -- coalescing happens on "
        "the way to the loop, it must not push conversion onto the loop"
    )

    pump()
    assert button.painted == [frames[-1]], (
        f"{len(frames)} pushes against a blocked main loop produced "
        f"{len(button.painted)} paints ({button.painted!r}) -- the slot must "
        "collapse them into one paint of the newest frame"
    )
    assert controller.ui_image_changes_while_hidden == {}, (
        "a coalesced frame was dirty-marked even though the newest one "
        f"painted: {controller.ui_image_changes_while_hidden!r}"
    )

    # The touchscreen mirror runs on the same slot, plus an interval: its
    # preview is as wide as the deck, so it is rate-limited on purpose -- and
    # a frame the interval holds back must still land once it expires, or the
    # last frame of a burst (a scroll that stops) is lost.
    ts_ident = Input.Touchscreen("sd-plus")
    assert adapter.push_input_image(controller, ts_ident, "strip-first") is True
    pump()
    assert strip.painted == ["strip-first"], (
        f"the first strip frame did not paint: {strip.painted!r}"
    )

    held = [f"strip-{i}" for i in range(10)]
    pushed_at = time.monotonic()
    for frame in held:
        assert adapter.push_input_image(controller, ts_ident, frame) is True
    pump()
    if time.monotonic() - pushed_at < TOUCHSCREEN_UI_INTERVAL_S:
        assert strip.painted == ["strip-first"], (
            "the touchscreen mirror painted inside its interval: "
            f"{strip.painted!r}"
        )

    def flushed() -> bool:
        pump()
        return strip.painted == ["strip-first", held[-1]]

    assert fixtures.wait_until(flushed, timeout=5), (
        "the frame the interval held back never flushed, or flushed a "
        f"superseded one: {strip.painted!r}"
    )

    adapter.unbind(controller)
    assert adapter._mirror_slots == {}, (
        "unbind left the unplugged deck's mirror slots behind -- each one "
        "pins the controller and its last frame"
    )
    print("PASS: mirror pushes coalesce to one paint of the newest frame per input")


def check_every_port_method_is_headless_safe() -> None:
    """No window, no gl.app: every method must be a quiet no-op."""
    from src.windows.ui_adapter import GtkUIAdapter

    identifier = Input.Key("0x0")
    controller = object()

    # Anything the adapter logs at WARNING+ is a failure for the quiet-no-op
    # half below: push_input_image's broad except REPORTS through the log, so
    # without this sink a guard that silently degraded into the except path
    # would look identical to a guard that worked.
    warnings: list = []
    sink_id = log.add(
        lambda msg: warnings.append(msg.record["message"]),
        level="WARNING",
        filter=lambda record: record["name"] == "src.windows.ui_adapter",
    )

    def exercise(port, label: str) -> None:
        try:
            assert port.push_input_image(controller, identifier, object()) is False, (
                f"{label}: push must be refused with no UI attached"
            )
            port.on_page_changed(controller)
            port.on_input_visuals_changed(controller, identifier, 0, "labels")
            port.on_input_visuals_changed(controller, identifier, 0, "layout")
            port.on_input_visuals_changed(controller, identifier, 0, "background")
            port.on_input_states_changed(controller, identifier, 3)
            port.on_input_state_selected(controller, identifier, 1)
            port.set_low_fps_warning(controller, True)
            port.on_deck_layout_changed(controller)
            assert port.query_input_widget(controller, identifier) is None
            assert port.query_deck_widget(controller, "key_grid") is None
            assert port.query_deck_widget(controller, "deck_stack_child") is None
            port.on_deck_added(controller)
            port.on_deck_removed(controller)
            port.refresh_deck_availability()
            port.on_page_list_changed()
            port.notify_plugin_problem("com_example_Plugin", "outdated")
            port.notify_plugin_problem("com_example_Plugin", "missing")
        except Exception as e:
            raise AssertionError(f"{label}: port method raised headless: {e!r}")

    exercise(ui_port.UIPort(), "null port")
    adapter = GtkUIAdapter()
    exercise(adapter, "unattached GtkUIAdapter")

    assert not warnings, (
        "the unattached adapter logged warnings while being exercised -- a "
        f"guard degraded into push_input_image's containment path: {warnings}"
    )

    # The adapter's public sync methods are thin GLib.idle_add wrappers, and
    # pygobject SWALLOWS exceptions raised inside idle callbacks -- so the
    # exercise() pass above proves almost nothing about them: with no main
    # loop the _run_* bodies (which hold every real guard) never execute at
    # all. Call those bodies directly.
    #
    # Each must also return False: a GLib idle/timeout callback that returns
    # anything truthy re-arms itself forever.
    run_bodies = [
        ("_run_page_changed", (controller,)),
        ("_run_input_visuals_changed", (controller, identifier, 0, "labels")),
        ("_run_input_visuals_changed", (controller, identifier, 0, "layout")),
        ("_run_input_visuals_changed", (controller, identifier, 0, "background")),
        ("_run_input_states_changed", (controller, identifier, 3)),
        ("_run_input_state_selected", (controller, identifier, 1)),
        ("_run_set_low_fps_warning", (controller, True)),
        ("_run_deck_layout_changed", (controller,)),
        ("_drain_mirror", (controller, Input.Touchscreen("sd-plus"))),
    ]
    for name, args in run_bodies:
        try:
            result = getattr(adapter, name)(*args)
        except Exception as e:
            raise AssertionError(
                f"GtkUIAdapter.{name} raised with no window attached: {e!r} -- "
                "headless this is invisible (pygobject swallows idle-callback "
                "exceptions), in the app it is a silently dead UI update"
            )
        assert result is False, (
            f"GtkUIAdapter.{name} returned {result!r}, expected False -- a "
            "GLib idle/timeout callback that returns truthy re-arms forever"
        )

    # An unknown aspect is a programming error, not a crash: it logs and
    # returns, so it is checked separately from the no-warning assertion.
    assert adapter._run_input_visuals_changed(controller, identifier, 0, "bogus") is False

    # push_input_image's except path (the containment that keeps a failing
    # preview from throttling the media writer). Exercised with a widget that
    # raises, since every OTHER refusal returns False through a guard and
    # never reaches the except at all.
    adapter.bind(controller, _fake_key_child(_RaisingButton()))
    adapter._window_mapped = True
    warnings.clear()
    assert adapter.push_input_image(controller, identifier, object()) is False, (
        "a raising widget must come back as False so the engine dirty-marks; "
        "anything else silently loses the frame"
    )
    assert warnings, (
        "the containment path swallowed a widget failure without logging"
    )
    adapter.unbind(controller)
    log.remove(sink_id)

    # on_deck_layout_changed ran inline on the main thread, which is the path
    # that imports KeyGrid lazily -- proving the ui_adapter <-> KeyGrid import
    # cycle is actually broken.
    import sys
    assert "src.windows.mainWindow.elements.KeyGrid" in sys.modules, (
        "the rotation path never reached its lazy KeyGrid import"
    )
    print("PASS: every port method is callable headless on both the null port and the adapter")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_ui_port_events")

    port = RecordingPort()
    ui_port.install(port)
    check_accepted_pushes_suppress_markers(port)
    check_page_change_follows_action_init()
    check_every_port_method_is_headless_safe()
    check_mirror_pushes_coalesce()

    print("PASS: scenario_ui_port_events")


if __name__ == "__main__":
    main()
