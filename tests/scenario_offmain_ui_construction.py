"""
Scenario: onboarding and store-page loaders must not construct GTK widgets
on their worker threads (issue #10 / B-10).

The trap in the store pages was subtle: `GLib.idle_add(section.append_child,
XPreview(...))` marshals the APPEND, but the widget tree is built as the
argument -- on the loader thread. PluginRecommendations.load() built whole
rows on a plain thread; onboarding's install worker read CheckButton state
directly; CustomAssetChooser.build() built the asset flow box + browse
button on its worker.

Four checks, one per site: store previews, recommendation rows, the
onboarding selection read, and the custom-asset chooser build.

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
    # The tail after the install loop touches more window state (the last
    # line is GLib.idle_add(gl.app.main_win.show), and gl.app is None in this
    # harness) -- an empty selection reaches it and raises. Swallow that here:
    # the assertion only concerns WHERE the selection read ran, and an
    # unswallowed raise would print a scary traceback under a PASS (thread
    # exceptions bypass loguru and go straight to the terminal).
    def _drive_worker():
        try:
            holder._on_start_button_click(obj)
        except Exception:
            pass
    worker = threading.Thread(target=_drive_worker, daemon=True)
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


def check_chooser() -> int:
    import src.windows.AssetManager.CustomAssets.Chooser as ch_mod

    recorder = ThreadRecorder()

    # The two widget trees build() constructs: the flow box (a page of
    # AssetPreviews) and the browse button. Both must land on the main loop.
    real_flowbox = ch_mod.CustomAssetChooserFlowBox
    ch_mod.CustomAssetChooserFlowBox = recorder.make_stub_class()

    real_gtk = ch_mod.Gtk

    class RecordingButton:
        def __init__(self, *a, **k):
            recorder.threads.append(threading.current_thread())

        def connect(self, *a, **k):
            pass

    ch_mod.Gtk = types.SimpleNamespace(Button=RecordingButton)

    import globals as gl
    real_settings = getattr(gl, "settings_manager", None)
    real_lm = getattr(gl, "lm", None)
    gl.settings_manager = types.SimpleNamespace(
        load_settings_from_file=lambda p: {})
    gl.lm = types.SimpleNamespace(get=lambda k: k)

    page = ch_mod.CustomAssetChooser.__new__(ch_mod.CustomAssetChooser)
    # main_box.remove/append and the toggle set_active all run INSIDE the
    # marshalled callback -- plain recording stubs on the main loop.
    page.main_box = types.SimpleNamespace(
        remove=lambda w: None, append=lambda w: None)
    page.scrolled_window = object()
    page.video_button = types.SimpleNamespace(set_active=lambda v: None)
    page.image_button = types.SimpleNamespace(set_active=lambda v: None)
    page.set_loading = lambda *a: None
    # _finish_build touches these; give it the real lock so it behaves.
    page._build_tasks_lock = threading.Lock()
    page.build_task_finished_tasks = []
    page.build_finished = False

    try:
        worker = threading.Thread(target=page.build, daemon=True)
        worker.start()
        # build() blocks on run_on_main -- the worker parks until the MAIN
        # thread pumps its idle callback. So we must keep pumping while the
        # worker runs (NOT join first, which would deadlock: worker waiting
        # on main, main waiting on worker).
        pump_until(lambda: not worker.is_alive() and page.build_finished, 8,
                   "build never finished via the main loop")
        worker.join(timeout=5)
        if worker.is_alive():
            print("FAIL(chooser): build did not finish")
            return 1
    finally:
        ch_mod.CustomAssetChooserFlowBox = real_flowbox
        ch_mod.Gtk = real_gtk
        gl.settings_manager = real_settings
        gl.lm = real_lm

    if not recorder.threads:
        print("FAIL(chooser): neither the flow box nor the browse button "
              "was constructed at all")
        return 1
    off_main = [t for t in recorder.threads if t is not threading.main_thread()]
    if off_main:
        print(f"FAIL(chooser): {len(off_main)}/{len(recorder.threads)} of the "
              f"asset flow box / browse button were constructed on the build "
              f"worker thread (off-main GTK, the process-fatal class)")
        return 1
    print("PASS: the asset flow box and browse button are constructed on the "
          "main loop")
    return 0


def main() -> int:
    start_watchdog(40, "offmain_ui_construction")
    rc = check_store_page()
    rc |= check_recommendations()
    rc |= check_selection_read()
    rc |= check_chooser()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
