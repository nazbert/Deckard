"""
The engine-to-UI port contract, observed through a recording implementation.

With an accepting port attached, a page load pushes an image for every key and
for the touchscreen, and nothing dirty-marks. on_page_changed fires with the
page's actions already initialized.
"""

# Every port method is callable headless, and the adapter's mirror slots
# coalesce to one paint.
import itertools
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
        # Set by the test. Snapshotted whenever on_page_changed fires.
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

        # (c) Detach. Pushes must dirty-mark again, exactly as headless does.
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
    """Stands in for a KeyButton / ScreenBarImage. Conversion on the producer,
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
    """Many producer pushes against a blocked main loop must cost one
    main-loop callback and one paint, carrying the last frame.

    Both mirrors hand a frame to a per-input latest-wins slot, so the backlog
    stays at one frame per input whatever the producer does.
    """
    # A convert-and-idle_add per frame accumulates one queued callback and one
    # retained pixbuf per frame per key on a stalled loop, then paints every
    # superseded frame in turn. The drains are counted as well as the paints,
    # because a slot that armed a callback per push still paints once, and
    # would look identical from the widget's side.
    from gi.repository import GLib

    from src.windows.ui_adapter import TOUCHSCREEN_UI_INTERVAL_S, GtkUIAdapter

    controller = _FakeController()
    button, strip = _RecordingMirror(), _RecordingMirror()
    adapter = GtkUIAdapter()
    adapter.bind(controller, _fake_mirror_child(button, strip))
    adapter._window_mapped = True

    # Every scheduled callback lands here first, because push_input_image
    # resolves self._drain_mirror at schedule time, so an instance attribute
    # wins.
    drains: list = []
    real_drain = adapter._drain_mirror

    def counting_drain(controller, identifier) -> bool:
        drains.append(identifier)
        return real_drain(controller, identifier)

    adapter._drain_mirror = counting_drain

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
    assert len(drains) == 1, (
        f"{len(frames)} pushes against a blocked main loop scheduled "
        f"{len(drains)} main-loop callbacks -- the slot must arm at most one "
        "while a drain is outstanding, however many frames arrive"
    )
    assert button.painted == [frames[-1]], (
        f"{len(frames)} pushes against a blocked main loop produced "
        f"{len(button.painted)} paints ({button.painted!r}) -- the slot must "
        "collapse them into one paint of the newest frame"
    )
    assert controller.ui_image_changes_while_hidden == {}, (
        "a coalesced frame was dirty-marked even though the newest one "
        f"painted: {controller.ui_image_changes_while_hidden!r}"
    )

    # The touchscreen mirror runs on the same slot, plus an interval. Its
    # preview is as wide as the deck, so the interval rate-limits it. A frame
    # the interval holds back must still land once it expires, or the last
    # frame of a burst, such as a scroll that stops, is lost.
    ts_ident = Input.Touchscreen("sd-plus")
    assert adapter.push_input_image(controller, ts_ident, "strip-first") is True
    pump()
    assert strip.painted == ["strip-first"], (
        f"the first strip frame did not paint: {strip.painted!r}"
    )

    held = [f"strip-{i}" for i in range(10)]
    drains.clear()
    for frame in held:
        assert adapter.push_input_image(controller, ts_ident, frame) is True
    pump()
    # The deadline is the slot's own. It starts at the drain that painted
    # "strip-first", not at the pushes above.
    slot = adapter._mirror_slots[(controller, ts_ident)]
    if time.monotonic() < slot._last_drain + TOUCHSCREEN_UI_INTERVAL_S:
        assert strip.painted == ["strip-first"], (
            "the touchscreen mirror painted inside its interval: "
            f"{strip.painted!r}"
        )
    assert len(drains) <= 1, (
        f"{len(held)} pushes inside the interval scheduled {len(drains)} "
        "callbacks -- the held frame needs exactly one delayed drain"
    )

    def flushed() -> bool:
        pump()
        return strip.painted == ["strip-first", held[-1]]

    assert fixtures.wait_until(flushed, timeout=5), (
        "the frame the interval held back never flushed, or flushed a "
        f"superseded one: {strip.painted!r}"
    )
    assert len(drains) == 1, (
        f"the held frame took {len(drains)} callbacks to reach the loop"
    )

    adapter.unbind(controller)
    assert adapter._mirror_slots == {}, (
        "unbind left the unplugged deck's mirror slots behind -- each one "
        "pins the controller and its last frame"
    )
    print("PASS: mirror pushes coalesce to one paint of the newest frame per input")


def check_unbind_tolerates_concurrent_drain() -> None:
    """unbind() must survive its slots being deleted underneath it.

    Deck removal arrives on the USB monitor, boot rescan and flatpak poll
    threads, while the GTK loop runs the drains still armed for that deck,
    and each drain pops its own slot the moment the child is gone.
    """
    # A KeyError out of unbind escapes on_deck_removed, so the caller never
    # reaches the controller's close() and the deck's media thread and USB
    # handle keep running with nothing holding them. The registry below runs
    # the adapter's real drains while unbind snapshots its keys, which is the
    # window the race lands in.
    from src.windows.ui_adapter import GtkUIAdapter, _MirrorSlot

    class _DrainDuringScan(dict):
        def __init__(self, adapter):
            super().__init__()
            self._adapter = adapter
            self._fired = False

        def __iter__(self):
            keys = list(dict.keys(self))
            if not self._fired:
                # The GTK loop, landing between unbind's snapshot and its
                # deletes.
                self._fired = True
                for controller, identifier in keys:
                    self._adapter._drain_mirror(controller, identifier)
            return iter(keys)

    controller = _FakeController()
    adapter = GtkUIAdapter()
    adapter.bind(controller, SimpleNamespace())
    adapter._window_mapped = True

    slots = _DrainDuringScan(adapter)
    identifiers = [Input.Key(f"{i}x0") for i in range(8)]
    for identifier in identifiers:
        slot = _MirrorSlot()
        slot.offer(f"frame for {identifier}")
        slots[(controller, identifier)] = slot
    adapter._mirror_slots = slots

    try:
        adapter.unbind(controller)
    except Exception as e:
        raise AssertionError(
            f"unbind raised {e!r} because a drain had already removed one of "
            "its slots -- on_deck_removed would abort before close(), leaving "
            "the deck's media thread and USB handle running"
        )
    assert slots._fired, "the drains never ran inside unbind's scan window"
    assert dict(slots) == {}, f"unbind left slots behind: {list(slots)!r}"
    print("PASS: unbind tolerates slots a concurrent drain already removed")


def check_dial_preview_rides_strip_payload() -> None:
    """The sidebar's dial preview is a crop of the strip frame, so it travels
    in that frame's payload.

    It is converted on the producer and painted by the same main-loop callback,
    with no callback of its own.
    """
    # The real prepare and paint halves run against a stand-in self.
    # Handing the crop to IconSelector.set_image instead puts one uncoalesced
    # idle on the loop per produced frame, at PRIORITY_HIGH, above GTK's own
    # redraw. That arrives at the producer's rate rather than the painted one,
    # and is the pressure the mirror slot removes.
    import src.windows.mainWindow.DeckPlus.ScreenBar as screenbar_mod
    from PIL import Image

    from src.windows.mainWindow.DeckPlus.ScreenBar import ScreenBarImage

    class _FakeIconSelector:
        def __init__(self):
            self.task_ids = itertools.count()
            self.latest_task_id = None
            self.painted: list = []

        def get_new_task_id(self):
            return next(self.task_ids)

        def set_pixbuf_and_del(self, pixbuf, task_id=None):
            self.painted.append((pixbuf, task_id))

        def set_image(self, image):
            raise AssertionError(
                "the dial preview went through IconSelector.set_image -- that "
                "schedules its own uncoalesced PRIORITY_HIGH idle per frame"
            )

    icon_selector = _FakeIconSelector()
    touchscreen = SimpleNamespace(get_dial_image_area=lambda ident: (0, 0, 40, 100))
    strip = SimpleNamespace(
        task_ids=itertools.count(),
        latest_task_id=None,
        screenbar=SimpleNamespace(
            deck_controller=SimpleNamespace(get_input=lambda ident: touchscreen)),
        painted=[],
    )
    strip.get_new_task_id = lambda: next(strip.task_ids)
    strip.set_pixbuf_and_del = lambda pixbuf, task_id=None: strip.painted.append(task_id)
    strip._prepare_dial_preview = lambda image: ScreenBarImage._prepare_dial_preview(
        strip, image)
    # set_image reaches back through self for both halves.
    strip.prepare_mirror_frame = lambda image: ScreenBarImage.prepare_mirror_frame(
        strip, image)
    strip.paint_mirror_frame = lambda payload: ScreenBarImage.paint_mirror_frame(
        strip, payload)

    # Recording stand-in for the module's GLib. Prepare/paint must not reach
    # for the loop at all, and set_image must reach for it exactly once.
    scheduled: list = []
    real_glib, real_app = screenbar_mod.GLib, gl.app
    screenbar_mod.GLib = SimpleNamespace(
        idle_add=lambda callback, *args, **kwargs: scheduled.append(callback))
    gl.app = SimpleNamespace(
        main_win=SimpleNamespace(
            sidebar=SimpleNamespace(
                active_identifier=Input.Dial("0"),
                key_editor=SimpleNamespace(icon_selector=icon_selector),
            )
        )
    )
    try:
        frame = Image.new("RGBA", (800, 100), (10, 20, 30, 255))

        payload = ScreenBarImage.prepare_mirror_frame(strip, frame)
        assert icon_selector.painted == [], (
            "the dial preview painted from the producer thread"
        )
        assert scheduled == [], (
            f"preparing a strip frame scheduled {len(scheduled)} main-loop "
            "callbacks -- conversion belongs on the producer, scheduling to "
            "the mirror slot"
        )
        dial = payload[2]
        assert dial is not None and dial[0] is icon_selector, (
            f"the dial preview never reached the strip's payload: {dial!r}"
        )

        assert ScreenBarImage.paint_mirror_frame(strip, payload) is False
        assert len(strip.painted) == 1, "the strip pixbuf did not paint"
        assert len(icon_selector.painted) == 1, (
            "the dial preview did not paint with the strip frame it was cut "
            f"from: {icon_selector.painted!r}"
        )
        assert scheduled == [], (
            "painting the payload scheduled an extra callback -- the dial "
            "preview is already on the loop with the strip frame"
        )

        # The replay path still costs exactly one callback for the whole
        # frame, dial preview included.
        ScreenBarImage.set_image(strip, frame)
        assert len(scheduled) == 1, (
            f"set_image scheduled {len(scheduled)} callbacks, expected 1"
        )

        # No dial selected. Nothing rides along.
        gl.app.main_win.sidebar.active_identifier = Input.Key("0x0")
        assert ScreenBarImage.prepare_mirror_frame(strip, frame)[2] is None, (
            "a strip frame carried a dial preview with no dial selected"
        )
    finally:
        screenbar_mod.GLib, gl.app = real_glib, real_app
    print("PASS: the dial preview rides the strip's payload instead of its own idle")


def check_port_methods_headless_safe() -> None:
    """No window, no gl.app. Every method must be a quiet no-op."""
    from src.windows.ui_adapter import GtkUIAdapter

    identifier = Input.Key("0x0")
    controller = object()

    # Anything the adapter logs at WARNING or above is a failure for the
    # quiet-no-op half below. push_input_image's broad except reports through
    # the log, so without this sink a guard that degraded into the except
    # path would look identical to a guard that worked.
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
    # pygobject swallows an exception raised inside an idle callback, so the
    # exercise() pass above proves little about them. With no main loop the
    # _run_* bodies, which hold every real guard, never execute. The calls
    # below reach those bodies directly.
    #
    # Each must also return False. A GLib idle/timeout callback that returns
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

    # An unknown aspect is a programming error, not a crash. It logs and
    # returns, so it is checked separately from the no-warning assertion.
    assert adapter._run_input_visuals_changed(controller, identifier, 0, "bogus") is False

    # push_input_image's except path (the containment that keeps a failing
    # preview from throttling the media writer). Exercised with a widget that
    # raises, because a refusal of any other kind returns False through a
    # guard and never reaches the except at all.
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
    # that imports KeyGrid lazily, so the import cycle between ui_adapter and
    # KeyGrid is broken.
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
    check_port_methods_headless_safe()
    check_mirror_pushes_coalesce()
    check_unbind_tolerates_concurrent_drain()
    check_dial_preview_rides_strip_payload()

    print("PASS: scenario_ui_port_events")


if __name__ == "__main__":
    main()
