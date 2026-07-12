"""
Integration scenario for P4.1 (docs/memory-footprint-impl-plan.md): lazy
GenerativeUI widget construction. Building a full Adw row tree at every
action's construction time (whether or not its config sidebar is ever
opened) was pure memory churn; GenerativeUI.__init__ now stores the
subclass's `build` closure and only runs it on first `.widget` access (see
GtkHelper/GenerativeUI/GenerativeUI.py's _ensure_built).

Follows scenario_action_teardown.py's conventions: actions are constructed
directly against a real headless Page from fixtures.make_headless_controller
(no plugin_manager, no gl.app/main_win), and GLib.idle_add callbacks are
pumped manually since nothing else here runs a GTK main loop.

Checks (a)-(c) run against a REAL subclass (SwitchRow, a real Adw.SwitchRow
under the hood) -- laziness is a base-class property, and SwitchRow's own
get_active()/reset_value() overrides are exactly the kind of subclass code
P4.1's subclass pass had to make build-skipping. Check (d) uses a stub
subclass with an explicit build counter instead: "built exactly once" is a
property of _ensure_built's bookkeeping, not of GTK widget plumbing, and a
counter is a more direct way to assert it than inspecting a live Adw
composite.

  (a) Constructing a GenerativeUI subclass does not build a widget
      (`_widget is None`) and registers on the action immediately.
  (b) The value layer works fully unbuilt: set_value()/get_value() round-trip
      through the action's settings without ever touching `.widget`, and
      reset_value() persists the default without forcing a build either.
  (c) Tearing down a never-built object is a no-op build-wise: clean_up()'s
      idle-queued destroy batch skips it (P1.1's `_widget is None` skip)
      rather than building it just to destroy it.
  (d) `.widget` access builds the widget exactly once, no matter how many
      times it's read afterwards.
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
    """Stand-in for a plugin action, following scenario_action_teardown's
    pattern. get_settings/set_settings are overridden to a plain dict so the
    value-layer checks don't need a real page.dict entry for a specific
    input coordinate -- GenerativeUI.get_value/set_value only ever go
    through these two methods."""

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
    """Stub concrete GenerativeUI for check (d): counts build_fn invocations
    directly rather than inferring them from widget identity."""

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
    """Services queued GLib.idle_add callbacks (clean_up()'s destroy batch
    is queued this way, not run synchronously)."""
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

    # set_value/get_value round-trip through settings without a widget.
    row.set_value(True)
    assert row.get_value() is True, "set_value/get_value did not round-trip"
    assert row._widget is None, "set_value/get_value forced a build"

    # reset_value() persists the default and must not force a build either
    # (P4.1 item 5: value-layer operations guard on built-ness).
    row.reset_value()
    assert row.get_value() is False, "reset_value did not persist the default"
    assert row._widget is None, "reset_value forced a build"

    # get_active() (the subclass's widget-state getter) falls back to the
    # value layer when unbuilt instead of crashing on a None widget.
    assert row.get_active() is False, "get_active() did not fall back to the value layer"

    print("PASS: value layer round-trips to settings without ever building a widget")


def check_teardown_never_built_is_noop(page) -> None:
    action = _FakeAction(page)
    row = SwitchRow(action, "switch_c", True, title="Test Switch C")
    assert row._widget is None
    assert action.generative_ui_objects == [row]

    action.clean_up()
    # Synchronous per P1.1: cleared the instant clean_up() returns.
    assert action.generative_ui_objects == [], "generative_ui_objects not cleared synchronously"

    _pump_glib()
    # The never-built row must still be unbuilt: the idle destroy batch
    # skips it outright (ActionCore._destroy_gen_ui_batch's `_widget is
    # None` check) rather than building it just to tear it down.
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
    """#71 (d): every concrete GenerativeUI subclass paired with a
    zero-config factory (action, var_name) -> instance. Titles are left None
    so build() -> get_translation(None) short-circuits to "" without needing a
    plugin_base/locale_manager (the value-layer _FakeAction has none) -- this
    keeps the forced build a pure widget-plumbing exercise, which is exactly
    what laziness is a property of.

    FileDialogRow is intentionally absent: it is an abstract GenerativeUI
    subclass (it does not implement connect_signals/disconnect_signals) and
    cannot be instantiated directly -- it is only ever subclassed further.
    Documented here so the omission is a known, reasoned gap, not an
    oversight."""
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


def check_all_subclasses_lazy_and_build_once(page) -> None:
    """#71 (d): the scenario tested laziness on ONE subclass (SwitchRow), but
    laziness has to hold for EVERY concrete subclass -- a subclass whose
    build() closure accidentally ran widget work at construction time (e.g. a
    stray super().__init__ ordering bug) would regress silently. Iterate all
    concrete subclasses: each must be unbuilt + registered at construction,
    and a single `.widget` access must build exactly once (a second access
    returns the same object)."""
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
    """#71 (d): two threads reading `.widget` (calling _ensure_built)
    concurrently must build the widget EXACTLY once. _ensure_built guards the
    flag transition with _build_flag_lock and flips _built True BEFORE running
    the build, so whichever thread loses the lock sees _built and returns
    without queuing a second build.

    Headless detail: a worker thread's build is marshalled through
    run_on_main -> GLib.idle_add, which only fires when something pumps the
    default main context. So both worker threads contend on _ensure_built's
    flag lock (only one queues a build), and THIS (main) thread pumps the
    context until the single queued build runs. The single-build property is
    a property of the flag lock, independent of when the pump lets the build
    land -- which is exactly what we assert.

    Determinism: a barrier releases both readers at once to force real
    contention on the flag lock; a bounded pump loop then drives the queued
    build to completion. No bare sleep is used for synchronization."""
    from gi.repository import GLib

    action = _FakeAction(page)
    obj = _CountingGenUI(action, "race_counting")

    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def reader(tag):
        try:
            barrier.wait(timeout=5)
            # .widget -> _ensure_built: exactly one worker wins the flag lock
            # and queues the build via run_on_main; the other short-circuits
            # on _built. This blocks until the main-context pump below runs
            # the queued build.
            results[tag] = obj.widget
        except Exception as e:
            errors.append((tag, e))

    t1 = threading.Thread(target=reader, args=("a",), name="genui-race-a")
    t2 = threading.Thread(target=reader, args=("b",), name="genui-race-b")
    t1.start()
    t2.start()

    # Pump the default main context so the single marshalled build actually
    # runs, until both readers have returned (or a bounded deadline).
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 10
    while (t1.is_alive() or t2.is_alive()) and time.monotonic() < deadline:
        ctx.iteration(False)

    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive(), "a genui race reader wedged"
    assert not errors, f"genui race readers raised: {errors!r}"

    # THE invariant: exactly one build, no matter which thread won the flag
    # lock. This is what a double-build regression (dropping the _built flip
    # or the flag lock) would break.
    assert obj.build_count == 1, (
        f"_ensure_built must build exactly once under a concurrent double read, "
        f"built {obj.build_count} times"
    )
    # The loser of the flag-lock race may observe the documented transient
    # (_built True, _widget still None -- issue #56, an accepted residual the
    # base class comments call out), so a racing reader's result can be None.
    # But every result that IS non-None must be the one built widget, and once
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
    check_all_subclasses_lazy_and_build_once(page)
    check_ensure_built_double_build_race(page)

    fixtures.teardown(controller)
    print("PASS: scenario_genui_lazy")


if __name__ == "__main__":
    main()
