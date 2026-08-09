"""
Unit-tier scenario for the shared font-row reload debounce (gl#78).

`GtkHelper/debounce.py`'s `TrailingDebouncer` is what the four Settings font
rows (family/size, colour, outline width, outline colour) now share instead
of each spawning its own `reload-all-pages` thread out of `on_set`.

Covers:
  (a) a burst of triggers coalesces to exactly one callback, and that
      callback happens only after the window -- never inline.
  (b) a trigger raised while a fire is pending re-arms the timer (trailing,
      not leading): the pending handle is cancelled and a fresh full-length
      one takes its place.
  (c) THE !100 CONSTRAINT: any trigger is eventually followed by a fire.
      The font-defaults -> reload_all_pages -> create_n_states path is what
      rebuilds every LabelManager, and the !100 label memos treat that
      rebuild as their pixel-correctness guarantee, so the debounce may
      DELAY the reload but must never ELIDE it. Checked across the shapes a
      dedupe/early-return regression would break: repeated identical
      triggers, a trigger from inside the callback, a trigger after a
      completed cycle.
  (d) the production GLibScheduler path really coalesces and really is
      one-shot, driven headless through the default GLib main context (this
      is where a wrong SOURCE_REMOVE return or a stale source_remove id
      would show up, not in the fake-scheduler checks).
  (e) an AST tripwire on src/windows/Settings/Settings.py: no font row may
      spawn its own reload thread again, every one of them routes through
      FontPageGroup.request_page_reload, and each still saves the font
      defaults immediately (only the reload is deferred, never the write).
"""
import ast
import os
import time

import fixtures  # must be first: isolates DATA_PATH

from GtkHelper.debounce import GLibScheduler, TrailingDebouncer

WATCHDOG_SECONDS = 60

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SETTINGS_PY = os.path.join(REPO_ROOT, "src", "windows", "Settings", "Settings.py")

FONT_ROW_CLASSES = ("FontRow", "FontColorRow", "FontOutlineColorRow", "FontOutlineWidthRow")


class FakeScheduler:
    """Virtual-time timer source: nothing runs until the test says so."""

    def __init__(self):
        self.armed = {}          # handle -> (delay_ms, callback)
        self.cancelled = []      # handles passed to cancel(), in order
        self._next_handle = 1

    def schedule(self, delay_ms, callback):
        handle = self._next_handle
        self._next_handle += 1
        self.armed[handle] = (delay_ms, callback)
        return handle

    def cancel(self, handle):
        assert handle in self.armed, f"cancel() on an unknown/stale handle: {handle!r}"
        self.cancelled.append(handle)
        del self.armed[handle]

    def advance(self):
        """Fire every currently-armed timer once (callbacks may re-arm)."""
        due = list(self.armed.items())
        self.armed.clear()
        for _handle, (_delay, callback) in due:
            callback()
        return len(due)


def check_burst_coalesces_to_one_fire() -> None:
    scheduler = FakeScheduler()
    fires = []
    debouncer = TrailingDebouncer(300, lambda: fires.append(time.monotonic()), scheduler=scheduler)

    for _ in range(5):
        debouncer.trigger()

    assert fires == [], "the callback ran inline -- a trailing debounce must not fire during the burst"
    assert len(scheduler.armed) == 1, f"a burst left {len(scheduler.armed)} timers armed, expected 1"

    scheduler.advance()
    assert len(fires) == 1, f"5 rapid triggers produced {len(fires)} callbacks, expected exactly 1"
    assert scheduler.armed == {}, "the trailing fire left a timer armed"

    print("PASS: a burst of 5 triggers coalesces into exactly one trailing callback")


def check_trigger_during_window_rearms() -> None:
    scheduler = FakeScheduler()
    fires = []
    debouncer = TrailingDebouncer(300, lambda: fires.append(1), scheduler=scheduler)

    debouncer.trigger()
    first_handle = next(iter(scheduler.armed))
    debouncer.trigger()

    assert scheduler.cancelled == [first_handle], (
        f"a trigger during the pending window must cancel the pending timer "
        f"(cancelled={scheduler.cancelled}, pending was {first_handle})"
    )
    assert first_handle not in scheduler.armed, "the superseded timer is still armed"
    (delay, _callback), = scheduler.armed.values()
    assert delay == 300, f"the re-armed timer got a shortened window ({delay}ms), expected the full 300ms"

    scheduler.advance()
    assert len(fires) == 1, f"re-arming produced {len(fires)} callbacks, expected 1"

    print("PASS: a trigger inside the pending window re-arms the full window (trailing, not leading)")


def check_callback_always_fires_after_any_trigger() -> None:
    """The !100 constraint: DELAY the reload, never ELIDE it."""
    # 1. Repeated *identical* triggers still fire. The debouncer carries no
    #    value at all, so there is nothing an equality check could dedupe
    #    against -- this pins that property.
    scheduler = FakeScheduler()
    fires = []
    debouncer = TrailingDebouncer(300, lambda: fires.append(1), scheduler=scheduler)
    for _ in range(3):
        debouncer.trigger()
    scheduler.advance()
    assert len(fires) == 1, "identical repeated triggers must still produce a fire (!100: never elide)"

    # 2. A second, independent cycle after one completed still fires -- the
    #    pending handle is cleared on fire, so the debouncer is re-usable.
    debouncer.trigger()
    scheduler.advance()
    assert len(fires) == 2, "a trigger after a completed cycle was swallowed (!100: never elide)"

    # 3. A trigger raised from *inside* the callback (the reentrant shape:
    #    the user changes another font row while the reload is being
    #    spawned) arms a fresh timer instead of cancelling a dead source,
    #    and that timer fires too.
    scheduler = FakeScheduler()
    reentrant = []

    def callback():
        reentrant.append(1)
        if len(reentrant) == 1:
            debouncer_2.trigger()

    debouncer_2 = TrailingDebouncer(300, callback, scheduler=scheduler)
    debouncer_2.trigger()
    scheduler.advance()
    assert len(reentrant) == 1, "first fire did not happen"
    assert len(scheduler.armed) == 1, "a trigger from inside the callback did not arm a new timer"
    scheduler.advance()
    assert len(reentrant) == 2, "the trigger raised from inside the callback never fired (!100: never elide)"

    # 4. Sweep: for any burst length, draining always yields a fire.
    for burst in range(1, 8):
        scheduler = FakeScheduler()
        fires = []
        debouncer_3 = TrailingDebouncer(300, lambda: fires.append(1), scheduler=scheduler)
        for _ in range(burst):
            debouncer_3.trigger()
        scheduler.advance()
        assert len(fires) == 1, f"a burst of {burst} triggers ended with {len(fires)} fires, expected exactly 1"

    print("PASS: !100 constraint -- every trigger is eventually followed by exactly one fire, never zero")


def check_glib_scheduler_coalesces_and_is_one_shot() -> None:
    from gi.repository import GLib

    fires = []
    debouncer = TrailingDebouncer(40, lambda: fires.append(1), scheduler=GLibScheduler())

    for _ in range(6):
        debouncer.trigger()
    assert fires == [], "GLib path fired inline instead of on the timeout"

    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5.0
    while not fires and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)

    assert len(fires) == 1, f"the GLib-scheduled debounce fired {len(fires)} times, expected 1"

    # One-shot: keep pumping well past the window, it must not repeat.
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)
    assert len(fires) == 1, f"the GLib timeout repeated ({len(fires)} fires) -- it must return SOURCE_REMOVE"

    print("PASS: the production GLibScheduler coalesces a burst into one one-shot fire")


def _dotted_name(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _called_names(node) -> set:
    return {_dotted_name(sub.func) for sub in ast.walk(node) if isinstance(sub, ast.Call)}


def _class_def(tree, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} vanished from Settings.py -- update this scenario")


def _func_def(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{class_node.name}.{name} vanished from Settings.py -- update this scenario")


def _derive_font_row_classes(tree) -> tuple:
    """Every class whose on_set touches font_page_group IS a font row --
    derived from the AST so a fifth row added later is covered without
    anyone remembering to extend a hardcoded list (review L-4). The
    literal FONT_ROW_CLASSES stays as the minimum expected set."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "on_set":
                    if any("font_page_group" in name
                           for name in _called_names(item)) or \
                       "font_page_group" in ast.dump(item):
                        found.append(node.name)
    return tuple(found)


def check_font_rows_route_through_the_group() -> None:
    tree = ast.parse(open(SETTINGS_PY, encoding="utf-8").read(), filename=SETTINGS_PY)

    derived = _derive_font_row_classes(tree)
    missing = set(FONT_ROW_CLASSES) - set(derived)
    assert not missing, (
        f"font rows vanished from Settings.py without this scenario being told: {missing}"
    )

    for class_name in derived:
        on_set = _func_def(_class_def(tree, class_name), "on_set")
        calls = _called_names(on_set)
        assert "threading.Thread" not in calls, (
            f"{class_name}.on_set spawns its own reload thread again -- every font row must go "
            f"through FontPageGroup.request_page_reload so the reloads coalesce (#78)"
        )
        assert "self.font_page_group.request_page_reload" in calls, (
            f"{class_name}.on_set no longer requests a page reload -- font changes would stop "
            f"reaching the decks"
        )
        assert "gl.settings_manager.save_font_defaults" in calls, (
            f"{class_name}.on_set no longer saves the font defaults immediately -- only the "
            f"reload may be deferred, never the write"
        )

    group = _class_def(tree, "FontPageGroup")
    init_calls = _called_names(_func_def(group, "__init__"))
    assert "TrailingDebouncer" in init_calls, "FontPageGroup no longer owns a TrailingDebouncer"
    request = _func_def(group, "request_page_reload")
    assert "self.reload_debouncer.trigger" in _called_names(request), (
        "FontPageGroup.request_page_reload bypasses the debouncer"
    )
    # The !100 never-elide invariant, enforced structurally (review M-1): the
    # trigger must be UNCONDITIONAL. Any If/Return in the body is the
    # "skip the reload when nothing changed" optimization that silently
    # breaks the label memos' correctness contract -- the reload may be
    # delayed, never elided.
    conditional = [n for n in ast.walk(request)
                   if isinstance(n, (ast.If, ast.Return, ast.IfExp))]
    assert not conditional, (
        "FontPageGroup.request_page_reload grew conditional logic "
        f"({[type(n).__name__ for n in conditional]}) -- the trigger must be "
        "unconditional: font_defaults -> reload_all_pages -> create_n_states "
        "is a pixel-correctness dependency of the label memos (!100); the "
        "reload may be DELAYED, never ELIDED"
    )
    spawn_calls = _called_names(_func_def(group, "_reload_all_pages"))
    assert "threading.Thread" in spawn_calls, (
        "FontPageGroup._reload_all_pages no longer spawns the reload thread -- reload_all_pages "
        "is blocking and must not run on the GTK main thread"
    )

    print(f"PASS: all {len(FONT_ROW_CLASSES)} font rows route their reload through the shared debouncer")


def check_trigger_is_single_thread() -> None:
    """The debouncer's _pending has no lock; a second triggering thread must
    fail loudly instead of racing it (review L-1)."""
    import threading

    from GtkHelper.debounce import TrailingDebouncer

    deb = TrailingDebouncer(10, lambda: None, scheduler=FakeScheduler())
    deb.trigger()  # binds the owner thread (this one)

    caught = []

    def cross_thread():
        try:
            deb.trigger()
        except RuntimeError as e:
            caught.append(e)

    t = threading.Thread(target=cross_thread)
    t.start()
    t.join(5)
    assert caught, (
        "a second thread triggered the debouncer without the single-thread "
        "contract firing -- the unlocked _pending handle would race"
    )
    print("PASS: cross-thread trigger fails loudly")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_font_reload_debounce")

    check_burst_coalesces_to_one_fire()
    check_trigger_during_window_rearms()
    check_callback_always_fires_after_any_trigger()
    check_glib_scheduler_coalesces_and_is_one_shot()
    check_trigger_is_single_thread()
    check_font_rows_route_through_the_group()

    print("PASS: scenario_font_reload_debounce")


if __name__ == "__main__":
    main()
