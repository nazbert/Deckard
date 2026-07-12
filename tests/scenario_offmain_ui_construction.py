"""
Scenario: onboarding and store-page loaders must not construct GTK widgets
on their worker threads (issue #10 / B-10).

The trap in the store pages was subtle: `GLib.idle_add(section.append_child,
XPreview(...))` marshals the APPEND, but the widget tree is built as the
argument -- on the loader thread. PluginRecommendations.load() built whole
rows on a plain thread; onboarding's install worker read CheckButton state
directly.

Technique: the real loader methods run on a worker thread with the preview/
row classes monkeypatched to thread-recording stubs; the main thread pumps
the GLib context. Post-fix every construction records the main thread;
pre-fix they record the worker.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time
import types

from gi.repository import GLib

from fixtures import start_watchdog


def pump_until(condition, timeout: float, what: str) -> None:
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


class ThreadRecorder:
    def __init__(self):
        self.threads: list[threading.Thread] = []

    def make_stub_class(recorder_self):
        class Stub:
            def __init__(self, *a, **k):
                recorder_self.threads.append(threading.current_thread())
        return Stub


def check_store_page() -> int:
    import src.windows.Store.Plugins.PluginPage as pp_mod

    recorder = ThreadRecorder()
    real_preview = pp_mod.PluginPreview
    pp_mod.PluginPreview = recorder.make_stub_class()

    page = pp_mod.PluginPage.__new__(pp_mod.PluginPage)
    appended = []
    section = types.SimpleNamespace(append_child=lambda w: appended.append(w))
    page.compatible_section = section
    page.incompatible_section = section
    page.set_loading = lambda *a: None
    page.set_loaded = lambda *a: None
    page.show_connection_error = lambda *a: None

    plugin = types.SimpleNamespace(is_compatible=True)
    page.store = types.SimpleNamespace(backend=types.SimpleNamespace(
        get_all_plugins=lambda: [plugin, plugin]))

    try:
        worker = threading.Thread(target=page.load, daemon=True)
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            print("FAIL(store): loader did not finish")
            return 1
        pump_until(lambda: len(appended) == 2, 5,
                   "previews never appended via the main loop")
    finally:
        pp_mod.PluginPreview = real_preview

    off_main = [t for t in recorder.threads if t is not threading.main_thread()]
    if off_main:
        print(f"FAIL(store): {len(off_main)}/2 PluginPreviews were "
              f"constructed on the loader thread (off-main GTK, the "
              f"process-fatal class)")
        return 1
    print("PASS: store previews are constructed on the main loop")
    return 0


def check_recommendations() -> int:
    import src.windows.Onboarding.PluginRecommendations as pr_mod

    recorder = ThreadRecorder()

    class StubRow:
        def __init__(self, plugin=None):
            recorder.threads.append(threading.current_thread())
            self.plugin = plugin
            self.check = types.SimpleNamespace(set_active=lambda v: None)

    real_row = pr_mod.PluginRow
    pr_mod.PluginRow = StubRow

    import globals as gl
    plugin = types.SimpleNamespace(is_compatible=True, plugin_id="x")
    real_backend = getattr(gl, "store_backend", None)
    gl.store_backend = types.SimpleNamespace(get_all_plugins=lambda: [plugin])

    rec = pr_mod.PluginRecommendations.__new__(pr_mod.PluginRecommendations)
    rec.defaults = []
    added = []
    rec.group = types.SimpleNamespace(add=lambda row: added.append(row))
    rec.loading_box = types.SimpleNamespace(set_spinning=lambda v: None)
    rec.main_stack = types.SimpleNamespace(set_visible_child=lambda w: None)
    rec.scrolled_window = object()

    try:
        worker = threading.Thread(target=rec.load, daemon=True)
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            print("FAIL(recommendations): loader did not finish")
            return 1
        pump_until(lambda: len(added) == 1, 5,
                   "rows never added via the main loop")
    finally:
        pr_mod.PluginRow = real_row
        gl.store_backend = real_backend

    off_main = [t for t in recorder.threads if t is not threading.main_thread()]
    if off_main:
        print("FAIL(recommendations): PluginRow was constructed on the "
              "worker thread (off-main GTK)")
        return 1
    print("PASS: recommendation rows are constructed on the main loop")
    return 0


def check_selection_read() -> int:
    import src.windows.Onboarding.OnboardingWindow as ow_mod

    read_threads = []

    def fake_selection():
        read_threads.append(threading.current_thread())
        return []

    # Drive the real install worker body up to the selection read, then
    # bail out (empty selection short-circuits the install loop).
    page = ow_mod.Recommendations.__new__(ow_mod.Recommendations) \
        if hasattr(ow_mod, "Recommendations") else None
    holder = None
    for name in dir(ow_mod):
        cls = getattr(ow_mod, name)
        if isinstance(cls, type) and hasattr(cls, "_on_start_button_click"):
            holder = cls
            break
    if holder is None:
        print("FAIL(selection): could not locate the install worker class")
        return 1

    obj = holder.__new__(holder)
    obj.onboarding_window = types.SimpleNamespace(
        recommendations=types.SimpleNamespace(
            get_selected_plugins=fake_selection),
        stack=types.SimpleNamespace(set_visible_child_name=lambda n: None),
        loading_box=types.SimpleNamespace(
            loading_label=types.SimpleNamespace(set_label=lambda t: None),
            set_spinning=lambda v: None,
            progress_bar=types.SimpleNamespace(
                set_visible=lambda v: None,
                set_text=lambda t: None,
                set_fraction=lambda f: None)),
        close=lambda *a: None,
        destroy=lambda *a: None,
    )
    # The tail after the install loop touches more window state; an empty
    # selection reaches it, so stub what it needs or tolerate the raise --
    # the assertion only concerns where the selection read ran.
    worker = threading.Thread(
        target=lambda: holder._on_start_button_click(obj), daemon=True)
    worker.start()
    pump_until(lambda: len(read_threads) > 0, 5,
               "selection read never happened")
    worker.join(timeout=5)

    if read_threads[0] is not threading.main_thread():
        print("FAIL(selection): get_selected_plugins (CheckButton reads) ran "
              "on the install worker thread")
        return 1
    print("PASS: selection is read on the main loop")
    return 0


def main() -> int:
    start_watchdog(40, "offmain_ui_construction")
    rc = check_store_page()
    rc |= check_recommendations()
    rc |= check_selection_read()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
