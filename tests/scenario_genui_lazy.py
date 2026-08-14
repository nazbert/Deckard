"""Integration scenario for lazy GenerativeUI widget construction.

GenerativeUI.__init__ stores the build closure of the subclass and runs it on
first .widget access. The value layer works fully unbuilt.
"""
import threading
import time

import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from src.backend.PluginManager.ActionCore import ActionCore
from GtkHelper.GenerativeUI.GenerativeUI import GenerativeUI
from GtkHelper.GenerativeUI.SwitchRow import SwitchRow


class _FakeAction(ActionCore):
    """Stand-in for a plugin action, following scenario_action_teardown.

    get_settings and set_settings are overridden to a plain dict, so the
    value-layer checks need no real page.dict entry. GenerativeUI.get_value
    and set_value only ever go through those two methods.
    """

    def __init__(self, page):
        super().__init__(
            action_id="test::fake",
            action_name="Fake",
            deck_controller=page.deck_controller,
            page=page,
            plugin_base=None,
            state=0,
            input_ident=None,
        )
        self._fake_settings: dict = {}

    def get_settings(self):
        return self._fake_settings

    def set_settings(self, settings: dict):
        self._fake_settings = settings


class _CountingGenUI(GenerativeUI):
    """Stub concrete GenerativeUI that counts build_fn invocations.

    Counting is more direct than inferring builds from widget identity.
    """

    def __init__(self, action_core: "ActionCore", var_name: str):
        self.build_count = 0

        def build():
            self.build_count += 1
            self._widget = Gtk.Label(label="stub")

        super().__init__(action_core, var_name, default_value=None, build=build)

    def connect_signals(self):
        pass

    def disconnect_signals(self):
        pass

    def set_ui_value(self, value):
        pass


def _pump_glib(timeout: float = 2.0) -> None:
    """Service the queued GLib.idle_add callbacks.

    The destroy batch of clean_up() is queued this way, not run synchronously.
    """
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and ctx.pending():
        ctx.iteration(False)


def check_construct_is_lazy_and_registers(page) -> None:
    action = _FakeAction(page)
    row = SwitchRow(action, "switch_a", True, title="Test Switch A")

    assert row._widget is None, "SwitchRow built its widget eagerly at construction"
    assert row in action.generative_ui_objects, "SwitchRow did not register on the action"
    print("PASS: construction is lazy and registers on the action")


def check_value_layer_unbuilt(page) -> None:
    action = _FakeAction(page)
    row = SwitchRow(action, "switch_b", False, title="Test Switch B")
    assert row._widget is None

    # set_value and get_value round-trip through settings without a widget.
    row.set_value(True)
    assert row.get_value() is True, "set_value/get_value did not round-trip"
    assert row._widget is None, "set_value/get_value forced a build"

    # reset_value() persists the default and must not force a build either,
    # because value-layer operations guard on built-ness.
    row.reset_value()
    assert row.get_value() is False, "reset_value did not persist the default"
    assert row._widget is None, "reset_value forced a build"

    # get_active(), the widget-state getter of the subclass, falls back to the
    # value layer when unbuilt instead of crashing on a None widget.
    assert row.get_active() is False, "get_active() did not fall back to the value layer"

    print("PASS: value layer round-trips to settings without ever building a widget")


def check_teardown_never_built_is_noop(page) -> None:
    action = _FakeAction(page)
    row = SwitchRow(action, "switch_c", True, title="Test Switch C")
    assert row._widget is None
    assert action.generative_ui_objects == [row]

    action.clean_up()
    # Synchronous. The list is cleared the instant clean_up() returns.
    assert action.generative_ui_objects == [], "generative_ui_objects not cleared synchronously"

    _pump_glib()
    # The never-built row must still be unbuilt. The idle destroy batch skips
    # it outright through the _widget check rather than building it just to
    # tear it down.
    assert row._widget is None, "teardown built a never-built widget"
    print("PASS: teardown of a never-built object is a no-op build-wise")


def check_widget_builds_exactly_once(page) -> None:
    action = _FakeAction(page)
    obj = _CountingGenUI(action, "counting")
    assert obj._widget is None
    assert obj.build_count == 0

    first = obj.widget
    assert obj.build_count == 1, f"expected 1 build, got {obj.build_count}"
    assert first is not None

    for _ in range(5):
        again = obj.widget
        assert again is first, ".widget returned a different object on a later access"
    assert obj.build_count == 1, f"build_fn ran more than once ({obj.build_count} times)"

    print("PASS: .widget builds exactly once across repeated access")


def _all_concrete_subclass_factories():
    """Every concrete GenerativeUI subclass, paired with a zero-config factory.

    Titles are left None, so build() short-circuits to an empty translation and
    needs no plugin_base or locale manager. FileDialogRow is absent because it
    is abstract and is only ever subclassed further.
    """
    from gi.repository import Adw
    from GtkHelper.GenerativeUI.SwitchRow import SwitchRow
    from GtkHelper.GenerativeUI.ToggleRow import ToggleRow
    from GtkHelper.GenerativeUI.EntryRow import EntryRow
    from GtkHelper.GenerativeUI.PasswordEntryRow import PasswordEntryRow
    from GtkHelper.GenerativeUI.SpinRow import SpinRow
    from GtkHelper.GenerativeUI.ScaleRow import ScaleRow
    from GtkHelper.GenerativeUI.ComboRow import ComboRow
    from GtkHelper.GenerativeUI.ExpanderRow import ExpanderRow
    from GtkHelper.GenerativeUI.ColorButtonRow import ColorButtonRow

    return [
        ("SwitchRow", lambda a, v: SwitchRow(a, v, True)),
        ("ToggleRow", lambda a, v: ToggleRow(a, v, 0, toggles=[Adw.Toggle(label="a"), Adw.Toggle(label="b")])),
        ("EntryRow", lambda a, v: EntryRow(a, v, "x")),
        ("PasswordEntryRow", lambda a, v: PasswordEntryRow(a, v, "x")),
        ("SpinRow", lambda a, v: SpinRow(a, v, 1.0, 0.0, 10.0)),
        ("ScaleRow", lambda a, v: ScaleRow(a, v, 1.0, 0.0, 10.0)),
        ("ComboRow", lambda a, v: ComboRow(a, v, "a", items=["a", "b", "c"])),
        ("ExpanderRow", lambda a, v: ExpanderRow(a, v, False)),
        ("ColorButtonRow", lambda a, v: ColorButtonRow(a, v, (0, 0, 0, 255))),
    ]


def check_subclasses_lazy_build_once(page) -> None:
    """Laziness must hold for every concrete subclass, not one of them.

    A build closure that ran widget work at construction time would regress
    silently. Each subclass must be unbuilt and registered at construction, and
    one .widget access must build exactly once.
    """
    for name, factory in _all_concrete_subclass_factories():
        action = _FakeAction(page)
        row = factory(action, f"{name}_var")

        assert row._widget is None, f"{name} built its widget eagerly at construction"
        assert row in action.generative_ui_objects, f"{name} did not register on the action"
        assert row.is_built is False, f"{name}.is_built must be False before any .widget access"

        first = row.widget
        assert first is not None, f"{name}.widget did not build a widget"
        assert row.is_built is True, f"{name}.is_built must flip True once built"
        second = row.widget
        assert second is first, f"{name}.widget returned a different object on a second access"

    print(f"PASS: all {len(_all_concrete_subclass_factories())} concrete subclasses are lazy and build once")


def check_ensure_built_double_build_race(page) -> None:
    """Two threads reading .widget concurrently must build exactly once.

    _ensure_built guards the flag transition with _build_flag_lock and flips
    _built True before it runs the build, so the losing thread queues nothing.
    A barrier forces contention, and this thread pumps until the build lands.
    """
    from gi.repository import GLib

    action = _FakeAction(page)
    obj = _CountingGenUI(action, "race_counting")

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def reader(tag):
        try:
            barrier.wait(timeout=5)
            # Reading .widget calls _ensure_built. Exactly one worker wins the
            # flag lock and queues the build through run_on_main, and the other
            # short-circuits on _built. This blocks until the main-context pump
            # below runs the queued build.
            results[tag] = obj.widget
        except Exception as e:
            errors.append((tag, e))

    t1 = threading.Thread(target=reader, args=("a",), name="genui-race-a")
    t2 = threading.Thread(target=reader, args=("b",), name="genui-race-b")
    t1.start()
    t2.start()

    # Pump the default main context, so the single marshalled build runs, until
    # both readers have returned or a bounded deadline passes.
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    while (t1.is_alive() or t2.is_alive()) and time.monotonic() < deadline:
        ctx.iteration(False)

    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive(), "a genui race reader wedged"
    assert not errors, f"genui race readers raised: {errors!r}"

    # The invariant. Exactly one build, whichever thread won the flag lock.
    # Dropping the _built flip or the flag lock would break it.
    assert obj.build_count == 1, (
        f"_ensure_built must build exactly once under a concurrent double read, "
        f"built {obj.build_count} times"
    )
    # The loser of the flag-lock race may observe the documented transient,
    # where _built is True and _widget is still None, so a racing reader can
    # return None. Every non-None result must be the one built widget, and once
    # the build has landed a fresh read must converge on it for both.
    assert obj._widget is not None, "the single build must have produced a widget"
    for tag, w in results.items():
        assert w is None or w is obj._widget, (
            f"reader {tag} saw a widget other than the single built one"
        )
    assert obj.widget is obj._widget, "a post-build read must return the single built widget"
    assert obj.build_count == 1, "a post-race read must not trigger another build"
    print("PASS: _ensure_built builds exactly once under a concurrent double read")


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_genui_lazy")
    controller = fixtures.make_headless_controller(serial="genui-lazy-1")
    page = controller.active_page

    check_construct_is_lazy_and_registers(page)
    check_value_layer_unbuilt(page)
    check_teardown_never_built_is_noop(page)
    check_widget_builds_exactly_once(page)
    check_subclasses_lazy_build_once(page)
    check_ensure_built_double_build_race(page)

    fixtures.teardown(controller)
    print("PASS: scenario_genui_lazy")


if __name__ == "__main__":
    main()
