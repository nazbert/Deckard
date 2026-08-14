"""The six AssetManager chooser loaders must build no GTK widget off-main.

Each spawns a build thread in __init__. Pack discovery stays on the worker;
widget construction and append marshal through run_on_main.
"""
import fixtures  # noqa: F401  (import first: isolated --data tempdir)

import ast
import inspect
import re
import threading
import time
import types

from gi.repository import GLib

import globals as gl


# Shared helpers

def pump_until(condition, timeout: float, what: str) -> None:
    """Iterate the default main context until condition() holds.

    The build workers block inside run_on_main until this thread services
    their idle source, so pump while they run. A join first would deadlock.
    """
    context = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        while context.iteration(False):
            pass
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s: {what}")


class Recorder:
    """Collects the thread every stubbed widget construction ran on."""

    def __init__(self):
        self.threads: list[threading.Thread] = []

    def note(self) -> None:
        self.threads.append(threading.current_thread())

    def off_main(self) -> list[threading.Thread]:
        return [t for t in self.threads if t is not threading.main_thread()]


def make_stubs(recorder: Recorder):
    """Build a flow-box stand-in and a bare preview stand-in.

    Both record the thread they were constructed on.
    """

    class StubFlowBox:
        N_ITEMS_PER_PAGE = 50

        def __init__(self, *args, **kwargs):
            recorder.note()
            self.flow_box = types.SimpleNamespace(
                connect=lambda *a, **k: None,
                append=lambda *a, **k: None,
                select_child=lambda *a, **k: None,
            )

        def set_factory(self, *a, **k): pass
        def set_filter_func(self, *a, **k): pass
        def set_sort_func(self, *a, **k): pass
        def set_item_list(self, *a, **k): pass
        def show_range(self, *a, **k): pass

    class StubPreview:
        def __init__(self, *args, **kwargs):
            recorder.note()

    return StubFlowBox, StubPreview


class FakePack:
    name = "fake-pack"

    def get_thumbnail_path(self): return None
    def get_icons(self): return []
    def get_wallpapers(self): return []


class FakePackManager:
    def __init__(self, count: int = 2):
        self._packs = {f"pack-{i}": FakePack() for i in range(count)}

    def get_icon_packs(self): return dict(self._packs)
    def get_wallpaper_packs(self): return dict(self._packs)


# 1. Runtime check. Every construction lands on the main loop.

# Module-global names to swap when a chooser names its widget classes directly.
# A class attribute wins when it exists.
WIDGET_GLOBAL_RE = re.compile(r"(FlowBox|Preview)$")
WIDGET_CLASS_ATTRS = ("FLOW_BOX_CLASS", "PACK_FLOW_BOX_CLASS",
                      "PREVIEW_CLASS", "PACK_PREVIEW_CLASS")


def patch_widget_factories(cls, module, stub_flow_box, stub_preview):
    """Point whatever the chooser builds widgets with at the stubs.

    A class attribute wins over a module global. Returns an undo callable.
    """
    undo: list = []

    for attr in WIDGET_CLASS_ATTRS:
        current = cls.__dict__.get(attr) or getattr(cls, attr, None)
        if current is None:
            continue
        stub = stub_flow_box if "FLOW_BOX" in attr else stub_preview
        # Set on the concrete class, never the shared base, so the classes stay
        # independent although they inherit the declaration of the attribute.
        holder = next(k for k in cls.__mro__ if attr in k.__dict__)
        undo.append((holder, attr, holder.__dict__[attr]))
        setattr(holder, attr, stub)

    if not undo:
        for name, value in list(vars(module).items()):
            if isinstance(value, type) and WIDGET_GLOBAL_RE.search(name):
                undo.append((module, name, value))
                setattr(module, name, stub_flow_box if name.endswith("FlowBox")
                        else stub_preview)

    def _undo():
        for holder, name, value in undo:
            setattr(holder, name, value)

    return _undo, len(undo)


def make_page(cls):
    """A chooser instance with the ChooserPage surface build() touches.

    No real GTK widget anywhere.
    """
    page = cls.__new__(cls)
    page.asset_manager = types.SimpleNamespace(
        back_button=types.SimpleNamespace(set_visible=lambda v: None))
    page.stack = types.SimpleNamespace(on_load_finished=lambda: None)
    page.type_box = types.SimpleNamespace(set_visible=lambda v: None)
    page.scrolled_box = types.SimpleNamespace(prepend=lambda w: None)
    page.scrolled_window = object()
    page.main_box = types.SimpleNamespace(remove=lambda w: None,
                                          append=lambda w: None)
    page.search_entry = types.SimpleNamespace(get_text=lambda: "")
    page.set_loading = lambda *a, **k: None
    page.build_finished = False
    page._pending_pack = None
    return page


def check_runtime(label: str, module_path: str, class_name: str,
                  min_constructions: int) -> int:
    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)

    recorder = Recorder()
    stub_flow_box, stub_preview = make_stubs(recorder)
    undo, patched = patch_widget_factories(cls, module, stub_flow_box, stub_preview)
    if patched == 0:
        print(f"FAIL({label}): found nothing to stub -- the check would be vacuous")
        return 1

    page = make_page(cls)
    done = threading.Event()

    def _drive():
        try:
            page.build()
        finally:
            done.set()

    try:
        worker = threading.Thread(target=_drive, daemon=True,
                                  name=f"{label}-build")
        worker.start()
        pump_until(done.is_set, 10, f"{label}.build() never finished")
        worker.join(timeout=5)
    finally:
        undo()

    if len(recorder.threads) < min_constructions:
        print(f"FAIL({label}): {len(recorder.threads)} widget constructions, "
              f"expected at least {min_constructions} -- build() bailed out "
              f"(@log.catch swallows) and the check proves nothing")
        return 1
    off_main = recorder.off_main()
    if off_main:
        print(f"FAIL({label}): {len(off_main)}/{len(recorder.threads)} widget "
              f"constructions ran on the build worker thread (off-main GTK, "
              f"the process-fatal class)")
        return 1
    print(f"PASS: {label} builds all {len(recorder.threads)} widgets on the main loop")
    return 0


# 2. Static tripwire. No widget construction is reachable off-main.

MARSHALS = ("run_on_main", "idle_add", "timeout_add", "on_main")

# Constructors that produce GTK widgets. The AssetManager classes end in
# FlowBox, Preview, Chooser or Page. Anything built straight off Gtk, Adw or
# Gdk counts too, as does the self.*_CLASS indirection the base uses.
WIDGET_CTOR_RE = re.compile(r"(FlowBox|Preview|Button|Label|Dialog|Window)$")
WIDGET_MODULES = ("Gtk", "Adw", "Gdk")
WIDGET_CLASS_ATTR_RE = re.compile(r"_CLASS$")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_widget_construction(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and WIDGET_CTOR_RE.search(func.id):
        return func.id
    if isinstance(func, ast.Attribute):
        if (isinstance(func.value, ast.Name)
                and func.value.id in WIDGET_MODULES):
            return f"{func.value.id}.{func.attr}"
        if WIDGET_CLASS_ATTR_RE.search(func.attr):
            return f"self.{func.attr}"
    return None


def _marshalled_targets(fn: ast.AST) -> set[str]:
    """Names handed to run_on_main or idle_add inside fn.

    These are the callables the function schedules onto the main loop.
    """
    targets: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) not in MARSHALS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute):
                targets.add(arg.attr)
            elif isinstance(arg, ast.Name):
                targets.add(arg.id)
            elif isinstance(arg, ast.Lambda):
                targets.add("<lambda>")
    return targets


def find_offmain_constructions(class_node: ast.ClassDef,
                               entries: tuple = ("build",)) -> list[str]:
    """Widget constructions reachable from entries without a main-loop marshal.

    Follows self.<method>() calls inside the class.
    """
    methods = {n.name: n for n in class_node.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [e for e in entries if e not in methods]
    if missing:
        return [f"{class_node.name}: no {', '.join(missing)}() to check"]

    violations: list[str] = []
    seen: set[str] = set()

    def visit(name: str, trail: str) -> None:
        if name in seen or name not in methods:
            return
        seen.add(name)
        fn = methods[name]
        marshalled = _marshalled_targets(fn)

        def walk(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # A nested def handed to run_on_main runs on the main loop.
                    # Anything else nested still runs on the worker.
                    if child.name in marshalled:
                        continue
                    walk(child)
                    continue
                if isinstance(child, ast.Call):
                    widget = _is_widget_construction(child)
                    if widget is not None:
                        violations.append(
                            f"{class_node.name}.{trail}: constructs {widget}() "
                            f"off the main loop")
                    called = _call_name(child)
                    if (isinstance(child.func, ast.Attribute)
                            and isinstance(child.func.value, ast.Name)
                            and child.func.value.id == "self"
                            and called not in marshalled):
                        visit(called, f"{trail} -> {called}")
                walk(child)

        walk(fn)

    for entry in entries:
        visit(entry, entry)
    return violations


def class_node(owner: type) -> tuple[ast.ClassDef, str]:
    source_file = inspect.getsourcefile(owner)
    tree = ast.parse(open(source_file, encoding="utf-8").read(), source_file)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == owner.__name__)
    return node, source_file


def class_node_for(cls, entry: str = "build") -> tuple[ast.ClassDef, str]:
    """The ClassDef that defines entry for cls."""
    owner = next(k for k in cls.__mro__ if entry in k.__dict__)
    return class_node(owner)


# Hooks the build worker calls into. A subclass may override these, and its
# body then runs off the main thread like build() does, so they get the same
# reachability check in the subclass file. The rest of the subclass surface
# runs from main-loop callbacks only.
WORKER_SIDE_HOOKS = ("get_packs", "get_pack_thumbnail_path",
                     "on_build_finished", "_reset_build_state")

# Sum of those hooks across the six classes is three get_packs and two
# on_build_finished. This guards the static leg against going vacuous when the
# six classes collapse onto shared bases.
MIN_SUBCLASS_HOOKS_CHECKED = 5


def check_static(label: str, module_path: str, class_name: str) -> tuple[int, int]:
    """Returns (rc, number of subclass-owned worker-side hooks checked)."""
    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)

    node, source_file = class_node_for(cls)
    violations = find_offmain_constructions(node)

    # The six classes share two bases, so the check above covers the same two
    # ClassDefs six times. Each class owns only its own hook bodies.
    own_node, own_file = class_node(cls)
    own_methods = {n.name for n in own_node.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    own_hooks = tuple(h for h in WORKER_SIDE_HOOKS if h in own_methods)
    if own_hooks:
        violations += [f"{v} [{own_file}]"
                       for v in find_offmain_constructions(own_node, own_hooks)]

    if violations:
        for v in violations:
            print(f"FAIL({label}): {v} [{source_file}]")
        return 1, len(own_hooks)

    # The check means something only while build() really is a thread body,
    # directly or through the one-hop _run_build wrapper that clears the
    # in-flight flag around it.
    owner_src = open(source_file, encoding="utf-8").read()
    if not re.search(r"threading\.Thread\(target=self\.(build|_run_build)\b", owner_src):
        print(f"FAIL({label}): build() is no longer started on a worker thread "
              f"in {source_file} -- re-point this tripwire at the new loader")
        return 1, len(own_hooks)

    print(f"PASS: {label} reaches no off-main widget construction from build() "
          f"or its {len(own_hooks)} own worker-side hook(s)")
    return 0, len(own_hooks)


# The tripwire's own regression test. It must flag the broken shape, both the
# direct construction and the one behind a helper call, and clear the fixed
# shape. A broken checker would otherwise pass everything.
BAD_SOURCE = """
class Chooser:
    def build(self):
        self.flow = IconPackFlowBox(self)
        self.load()

    def load(self):
        for pack in self.packs:
            self.flow.flow_box.append(IconPackPreview(self, pack))
"""

GOOD_SOURCE = """
class Chooser:
    def build(self):
        packs = self.get_packs()
        run_on_main(self._build_ui, packs)

    def _build_ui(self, packs):
        self.flow = IconPackFlowBox(self)
        for pack in packs:
            self.flow.flow_box.append(IconPackPreview(self, pack))
"""


def check_tripwire_self_test() -> int:
    bad_node = next(n for n in ast.walk(ast.parse(BAD_SOURCE))
                    if isinstance(n, ast.ClassDef))
    bad = find_offmain_constructions(bad_node)
    if len(bad) < 2:
        print(f"FAIL(self-test): the checker found {len(bad)} violations in the "
              f"pre-fix shape, expected the flow box AND the preview behind "
              f"load() -- it would pass vacuously")
        return 1

    good_node = next(n for n in ast.walk(ast.parse(GOOD_SOURCE))
                     if isinstance(n, ast.ClassDef))
    good = find_offmain_constructions(good_node)
    if good:
        print(f"FAIL(self-test): the checker flags the marshalled shape: {good}")
        return 1

    print("PASS: the off-main tripwire flags the pre-fix shape and clears the "
          "marshalled one")
    return 0


# 2b. A marshal that times out must not strand the page

def check_marshal_timeout(label: str, module_path: str, class_name: str) -> int:
    """A run_on_main timeout must not strand the page.

    run_on_main raises RuntimeError when the main loop does not service its
    idle in time. Unhandled, the page keeps spinning, build_finished stays
    unset, and readers hit AttributeError on the flow box.
    """
    import importlib

    import src.backend.main_loop as main_loop

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)

    recorder = Recorder()
    stub_flow_box, stub_preview = make_stubs(recorder)
    undo, _ = patch_widget_factories(cls, module, stub_flow_box, stub_preview)

    page = make_page(cls)
    ui = {"spinner_stopped": False, "label": None, "loading": []}
    page.spinner = types.SimpleNamespace(
        stop=lambda: ui.__setitem__("spinner_stopped", True), start=lambda: None)
    page.loading_label = types.SimpleNamespace(
        set_label=lambda text: ui.__setitem__("label", text))
    page.set_visible_child_name = lambda name: None
    page.set_loading = lambda value: ui["loading"].append(value)

    # Only the leaf pages hold a drill-in request across the build, and each
    # base owns one flow-box attribute. Assert what this page has.
    holds_pending = hasattr(cls, "load_for_pack")
    flow_attr = "pack_flow" if hasattr(cls, "PACK_FLOW_BOX_CLASS") else "asset_flow"
    if holds_pending:
        # A drill-in requested while the build was still queued.
        page._pending_pack = FakePack()

    real_timeout = main_loop.RUN_ON_MAIN_TIMEOUT_S
    main_loop.RUN_ON_MAIN_TIMEOUT_S = 0.2
    try:
        done = threading.Event()

        def _drive():
            try:
                page.build()
            finally:
                done.set()

        # Do not pump, which models a stalled main loop.
        worker = threading.Thread(target=_drive, daemon=True, name=f"{label}-stall")
        worker.start()
        if not done.wait(timeout=15):
            print(f"FAIL({label} timeout): build() never returned after the "
                  f"marshal deadline -- the worker is stuck too")
            return 1
    finally:
        main_loop.RUN_ON_MAIN_TIMEOUT_S = real_timeout

    if not getattr(page, "build_failed", False):
        print(f"FAIL({label} timeout): the page does not know its build failed "
              f"(@log.catch swallowed the RuntimeError)")
        undo()
        return 1
    if getattr(page, "build_finished", False):
        print(f"FAIL({label} timeout): build_finished set despite the failure")
        undo()
        return 1
    if holds_pending and page._pending_pack is not None:
        print(f"FAIL({label} timeout): a pending pack is still held for a grid "
              f"that was never built -- stranded")
        undo()
        return 1
    flow_value = getattr(page, flow_attr, "<AttributeError>")
    if flow_value is not None:
        print(f"FAIL({label} timeout): {flow_attr} after a failed build is "
              f"{flow_value!r}, not None -- readers get an AttributeError "
              f"instead of a clean 'not built'")
        undo()
        return 1

    # The queued error panel lands when the loop is pumped.
    pump_until(lambda: ui["spinner_stopped"], 5,
               "the spinner was never stopped after the failure")
    if ui["label"] is None:
        print(f"FAIL({label} timeout): the page shows no error message")
        undo()
        return 1

    # Reopening the window retries, and the retry must succeed.
    try:
        started = page.retry_build()
        if not started:
            print(f"FAIL({label} timeout): retry_build() refused to rebuild a "
                  f"failed page")
            return 1
        pump_until(lambda: getattr(page, "build_finished", False), 10,
                   "the retried build never finished")
    finally:
        undo()

    if not recorder.threads or recorder.off_main():
        print(f"FAIL({label} timeout): the retried build did not construct its "
              f"widgets on the main loop")
        return 1
    print(f"PASS: {label} survives a marshal timeout and rebuilds on retry")
    return 0


# 3. Real-widget check over the actual window, when a display is available.

def check_real_window() -> int:
    """Build the real AssetManager and check every flow box against live GTK.

    All six choosers must construct their flow box on the main thread and
    attach it. Prints SKIP and exits without a display.
    """
    import importlib
    import os

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    if not Gtk.init_check():
        print("SKIP(real-window): no display; the stubbed checks above still ran")
        return 0
    Adw.init()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    image = os.path.join(repo_root, "Assets", "Onboarding", "icon.png")

    class FakeAsset:
        def __init__(self, name):
            self.path = image
            self.name = name

        def get_attribution(self): return {}

    class RealFakePack:
        name = "fake-pack"

        def __init__(self):
            self._assets = [FakeAsset(f"asset-{i}") for i in range(3)]

        def get_thumbnail_path(self): return image
        def get_pack_attribution(self): return {}
        def get_icons(self): return list(self._assets)
        def get_wallpapers(self): return list(self._assets)

    class RealFakeManager:
        def __init__(self):
            self._packs = {f"pack-{i}": RealFakePack() for i in range(2)}

        def get_icon_packs(self): return dict(self._packs)
        def get_wallpaper_packs(self): return dict(self._packs)

    class EmptyBackend:
        def get_all(self): return []
        def has_by_internal_path(self, path): return True
        def remove_asset_by_id(self, asset_id): pass
        def add_custom_media_set_by_ui(self, *a, **k): pass

    gl.icon_pack_manager = RealFakeManager()
    gl.wallpaper_pack_manager = RealFakeManager()
    gl.sd_plus_bar_wallpaper_pack_manager = RealFakeManager()
    gl.asset_manager_backend = EmptyBackend()
    gl.lm = types.SimpleNamespace(get=lambda key, *a, **k: key)
    gl.app = None
    if getattr(gl, "settings_manager", None) is None:
        from src.backend.SettingsManager import SettingsManager
        gl.settings_manager = SettingsManager()

    recorder = Recorder()
    undo: list = []
    for _, module_path, class_name, _ in CASES:
        cls = getattr(importlib.import_module(module_path), class_name)
        for attr in WIDGET_CLASS_ATTRS:
            real = cls.__dict__.get(attr)
            if real is None or "FLOW_BOX" not in attr:
                continue

            # A real flow box that also records its construction thread.
            class Recording(real):
                def __init__(self, *a, **k):
                    recorder.note()
                    super().__init__(*a, **k)

            undo.append((cls, attr, real))
            setattr(cls, attr, Recording)

    from src.windows.AssetManager.AssetManager import AssetManager

    try:
        window = AssetManager(main_window=Gtk.Window())
        chooser = window.asset_chooser
        pages = [
            chooser.icon_pack_chooser.pack_chooser,
            chooser.icon_pack_chooser.icon_chooser,
            chooser.wallpaper_pack_chooser.pack_chooser,
            chooser.wallpaper_pack_chooser.wallpaper_chooser,
            chooser.sd_plus_bar_wallpaper_pack_chooser.pack_chooser,
            chooser.sd_plus_bar_wallpaper_pack_chooser.wallpaper_chooser,
        ]
        pump_until(lambda: all(getattr(p, "build_finished", False) for p in pages),
                   20, "the six real chooser builds never finished")
    finally:
        for cls, attr, real in undo:
            setattr(cls, attr, real)

    if len(recorder.threads) != len(pages):
        print(f"FAIL(real-window): {len(recorder.threads)} flow boxes built, "
              f"expected {len(pages)}")
        return 1
    if recorder.off_main():
        print(f"FAIL(real-window): {len(recorder.off_main())} real flow boxes "
              f"were constructed off the main thread")
        return 1

    for page in pages:
        flow = getattr(page, "pack_flow", None) or getattr(page, "asset_flow", None)
        if flow is None or flow.get_parent() is None:
            print(f"FAIL(real-window): {type(page).__name__} never attached its "
                  f"flow box to the page")
            return 1

    # Drill into a pack the way a click does, then let the recycler bind. A
    # rendered child must be visible and carry the pack asset.
    leaf_pages = [p for p in pages if getattr(p, "asset_flow", None) is not None]
    pack = RealFakePack()
    for page in leaf_pages:
        page.load_for_pack(pack)
    pump_until(
        lambda: all(p.asset_flow.flow_box.get_child_at_index(0).get_visible()
                    for p in leaf_pages),
        10, "the recycler never rendered the pack's assets")

    for page in leaf_pages:
        child = page.asset_flow.flow_box.get_child_at_index(0)
        if page.get_child_asset(child) is None:
            print(f"FAIL(real-window): {type(page).__name__} rendered a child "
                  f"with no asset bound")
            return 1

    print(f"PASS: the real AssetManager builds all {len(pages)} chooser flow "
          f"boxes on the main thread and renders a pack's assets")
    return 0


# Cases

# (label, module, class, minimum widget constructions the runtime drive sees).
# A pack chooser builds a flow box and one preview per pack, with two fake
# packs. A leaf chooser builds the flow box; its 50 previews live inside the
# real DynamicFlowBox, which the stub replaces.
CASES = [
    ("icon-packs", "src.windows.AssetManager.IconPacks.PackChooser",
     "IconPackChooser", 3),
    ("icons", "src.windows.AssetManager.IconPacks.Icons.IconChooser",
     "IconChooserPage", 1),
    ("wallpaper-packs", "src.windows.AssetManager.WallpaperPacks.PackChooser",
     "WallpaperPackChooser", 3),
    ("wallpapers",
     "src.windows.AssetManager.WallpaperPacks.Wallpapers.WallpaperChooser",
     "WallpaperChooserPage", 1),
    ("sd+bar-packs",
     "src.windows.AssetManager.SDPlusBarWallpaperPacks.PackChooser",
     "SDPlusBarWallpaperPackChooser", 3),
    ("sd+bar-wallpapers",
     "src.windows.AssetManager.SDPlusBarWallpaperPacks.SDPlusBarWallpaper."
     "SDPlusBarWallpaperChooser", "SDPlusBarWallpaperChooserPage", 1),
]


def main() -> int:
    fixtures.start_watchdog(120, label="scenario_asset_chooser_offmain")

    gl.icon_pack_manager = FakePackManager()
    gl.wallpaper_pack_manager = FakePackManager()
    gl.sd_plus_bar_wallpaper_pack_manager = FakePackManager()
    gl.lm = types.SimpleNamespace(get=lambda key, *a, **k: key)

    rc = check_tripwire_self_test()
    hooks_checked = 0
    for label, module_path, class_name, minimum in CASES:
        rc |= check_runtime(label, module_path, class_name, minimum)
        static_rc, hooks = check_static(label, module_path, class_name)
        rc |= static_rc
        hooks_checked += hooks
    # Three get_packs and two on_build_finished. A drop means subclass bodies
    # moved somewhere this static leg does not look at.
    if hooks_checked < MIN_SUBCLASS_HOOKS_CHECKED:
        print(f"FAIL: the static leg only checked {hooks_checked} subclass-owned "
              f"worker-side hooks, expected at least "
              f"{MIN_SUBCLASS_HOOKS_CHECKED} -- it has gone partly vacuous")
        rc |= 1
    else:
        print(f"PASS: the static leg checked {hooks_checked} subclass-owned "
              f"worker-side hook bodies on top of the two shared build()s")
    # One pack page and one leaf page, the two build() bodies that marshal.
    print("NOTE: the next two ERROR tracebacks are DELIBERATE -- the timeout "
          "legs stall the loop on purpose and this is the log the fix emits.")
    for label, module_path, class_name, _ in (CASES[0], CASES[1]):
        rc |= check_marshal_timeout(label, module_path, class_name)
    # Run last, because it installs a real window and real gl.* collaborators.
    rc |= check_real_window()

    if rc == 0:
        print("ALL PASS: scenario_asset_chooser_offmain")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
