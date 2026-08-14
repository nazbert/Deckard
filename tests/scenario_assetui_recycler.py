"""Regression test for the asset-manager recycler and chooser races.

One main-loop callback must unselect, bind and then show a recycled child.
The build-finished flag and the deferred-task queue move under one lock.
"""
import fixtures  # noqa: F401  (isolated --data tempdir; import first)

import threading
import time

import src.windows.AssetManager.DynamicFlowBox as dfb_mod
import src.windows.AssetManager.CustomAssets.AssetPreview as ap_mod
import src.windows.AssetManager.CustomAssets.Chooser as chooser_mod
import src.windows.AssetManager.IconPacks.Stack as stack_mod


# Fakes

class FakeGLib:
    """Captures idle_add callbacks, so the test controls main-loop time."""

    def __init__(self):
        self.queue = []

    def idle_add(self, fn, *args):
        self.queue.append((fn, args))
        return 1

    def drain(self):
        while self.queue:
            fn, args = self.queue.pop(0)
            fn(*args)


class FakeChild:
    def __init__(self, log, index):
        self._log = log
        self._index = index
        self.visible = False
        self.asset = None

    def set_visible(self, visible):
        self._log.append(("visible", self._index, visible, self.asset))
        self.visible = visible


class FakeFlowBox:
    def __init__(self, children, log):
        self._children = children
        self._log = log
        self.selected = []

    def get_child_at_index(self, i):
        return self._children[i] if 0 <= i < len(self._children) else None

    def unselect_all(self):
        self._log.append(("unselect_all",))
        self.selected.clear()

    def select_child(self, child):
        self._log.append(("select", child._index))
        self.selected.append(child)


class FakeButton:
    def __init__(self):
        self.sensitive = None

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive


class StubFlow:
    """Carries exactly the attributes DynamicFlowBox.show_range touches."""

    N_ITEMS_PER_PAGE = 4

    def __init__(self, items):
        self.items = items
        self.event_log = []
        self.children = [FakeChild(self.event_log, i) for i in range(self.N_ITEMS_PER_PAGE)]
        self.flow_box = FakeFlowBox(self.children, self.event_log)
        self.back_button = FakeButton()
        self.next_button = FakeButton()
        self.factory_func = self._factory

    def _factory(self, preview, item):
        self.event_log.append(("bind", preview._index, item))
        preview.asset = item

    def get_items_to_show(self):
        return self.items

    # Borrow the real methods under test.
    show_range = dfb_mod.DynamicFlowBox.show_range
    if hasattr(dfb_mod.DynamicFlowBox, "_apply_range"):
        _apply_range = dfb_mod.DynamicFlowBox._apply_range


# 1. DynamicFlowBox leaves no visible-but-unbound gap and swaps pages atomically.

def test_children_not_shown_before_bound() -> None:
    fake_glib = FakeGLib()
    dfb_mod.GLib = fake_glib

    flow = StubFlow(items=["a1", "a2", "a3"])
    flow.show_range(0, flow.N_ITEMS_PER_PAGE)

    # Before the main loop runs, the pool must be untouched. A visible child
    # with asset None here is the click gap.
    for child in flow.children:
        assert not (child.visible and child.asset is None), (
            f"child {child._index} is clickable with no asset bound -- "
            "a click here raises TypeError in on_child_activated"
        )
        assert not child.visible, (
            f"child {child._index} became visible before the main-loop bind ran"
        )

    fake_glib.drain()

    assert [c.asset for c in flow.children] == ["a1", "a2", "a3", None]
    assert [c.visible for c in flow.children] == [True, True, True, False]

    # For each child the bind must precede the visibility flip, and the flip
    # must already see the new asset.
    for event in flow.event_log:
        if event[0] == "visible" and event[2] is True:
            _, index, _, asset_at_flip = event
            assert asset_at_flip is not None, (
                f"child {index} was shown before its asset was bound"
            )

    assert flow.back_button.sensitive is False
    assert flow.next_button.sensitive is False


def test_page_swap_atomic_clears_selection() -> None:
    fake_glib = FakeGLib()
    dfb_mod.GLib = fake_glib

    flow = StubFlow(items=["a1", "a2", "a3", "a4", "b1", "b2"])
    flow.show_range(0, flow.N_ITEMS_PER_PAGE)
    fake_glib.drain()
    flow.flow_box.selected.append(flow.children[2])  # user selects a3
    flow.event_log.clear()

    # On the next page, until the main loop runs, page A must stay intact. The
    # swap is atomic, so the old content stays consistent.
    flow.show_range(4, 8)
    assert [c.asset for c in flow.children] == ["a1", "a2", "a3", "a4"]
    assert all(c.visible for c in flow.children)

    fake_glib.drain()

    assert [c.asset for c in flow.children] == ["b1", "b2", "a3", "a4"]
    assert [c.visible for c in flow.children] == [True, True, False, False]

    # The stale selection from page A was dropped before any page-B child was
    # bound or shown, so no phantom selection survives.
    assert flow.flow_box.selected == [], "page A's selection survived onto page B"
    first_bind = next(i for i, e in enumerate(flow.event_log) if e[0] == "bind")
    unselect = next(i for i, e in enumerate(flow.event_log) if e[0] == "unselect_all")
    assert unselect < first_bind, "unselect_all must precede the rebind"

    assert flow.back_button.sensitive is True
    assert flow.next_button.sensitive is False


def test_filter_shrink_hides_leftovers_atomically() -> None:
    fake_glib = FakeGLib()
    dfb_mod.GLib = fake_glib

    flow = StubFlow(items=["a1", "a2", "a3", "a4"])
    flow.show_range(0, flow.N_ITEMS_PER_PAGE)
    fake_glib.drain()

    # A filter change shrinks the result set to one item.
    flow.items = ["a4"]
    flow.show_range(0, flow.N_ITEMS_PER_PAGE)
    fake_glib.drain()

    assert [c.visible for c in flow.children] == [True, False, False, False]
    assert flow.children[0].asset == "a4"


# 2. AssetPreview.set_asset binds synchronously

def test_set_asset_binds_synchronously() -> None:
    fake_glib = FakeGLib()
    ap_mod.GLib = fake_glib

    class StubPreview:
        def __init__(self):
            self.texts = []
            self.images = []

        def set_text(self, text):
            self.texts.append(text)

        def set_image(self, image):
            self.images.append(image)

    stub = StubPreview()
    asset = {"name": "cat", "thumbnail": "/tmp/cat.png"}
    ap_mod.AssetPreview.set_asset(stub, flow="flow-sentinel", asset=asset)

    assert stub.texts == ["cat"] and stub.images == ["/tmp/cat.png"], (
        "set_asset must bind text/image directly (it runs on the main loop "
        f"now); got texts={stub.texts} images={stub.images} with "
        f"{len(fake_glib.queue)} deferred idle(s) instead"
    )
    assert stub.asset == asset and stub.flow == "flow-sentinel"


# 3. CustomAssetChooser races build_finished against the deferred task.

def _make_chooser_stub(ran):
    class Stub:
        pass

    stub = Stub()
    stub.build_finished = False
    stub.build_task_finished_tasks = []
    stub._build_tasks_lock = threading.Lock()

    class StubAssetChooser:
        def show_for_path(self, path):
            ran.append(path)

    stub.asset_chooser = StubAssetChooser()
    return stub


def test_raced_deferred_task_still_runs() -> None:
    """Replay the losing interleaving deterministically.

    show_for_path reads build_finished as False, stops inside its append, and
    the build finishes meanwhile. The deferred task must still run.
    """
    ran = []
    stub = _make_chooser_stub(ran)

    gate_reached = threading.Event()
    proceed = threading.Event()

    class GatedList(list):
        def append(self, item):
            gate_reached.set()
            assert proceed.wait(timeout=5), "test wiring: proceed never set"
            super().append(item)

    stub.build_task_finished_tasks = GatedList()

    t_caller = threading.Thread(
        target=chooser_mod.CustomAssetChooser.show_for_path, args=(stub, "raced"),
        name="raced_show_for_path",
    )
    t_caller.start()
    assert gate_reached.wait(timeout=5), "show_for_path never tried to defer its task"

    # The build finishes now. Without the lock this drains an empty queue and
    # the raced append lands afterwards, stranded forever.
    t_finish = threading.Thread(
        target=chooser_mod.CustomAssetChooser._finish_build, args=(stub,),
        name="finish_build",
    )
    t_finish.start()

    time.sleep(0.1)
    assert not ran, "finish_build must not have completed while the append is mid-flight"

    proceed.set()
    t_caller.join(timeout=5)
    t_finish.join(timeout=5)
    assert not t_caller.is_alive() and not t_finish.is_alive()

    assert ran == ["raced"], (
        f"the raced deferred task must run exactly once, got {ran!r}"
    )
    assert stub.build_task_finished_tasks == [], "no task may be left stranded"
    assert stub.build_finished is True


def test_post_finish_calls_dispatch_directly() -> None:
    ran = []
    stub = _make_chooser_stub(ran)

    chooser_mod.CustomAssetChooser._finish_build(stub)
    chooser_mod.CustomAssetChooser.show_for_path(stub, "direct")

    assert ran == ["direct"]
    assert stub.build_task_finished_tasks == []


def test_enqueue_storm_loses_no_tasks() -> None:
    """Stress the lock with N threads racing show_for_path and _finish_build.

    Every path must be delivered exactly once, deferred or direct.
    """
    for _ in range(50):
        ran = []
        stub = _make_chooser_stub(ran)
        paths = [f"p{i}" for i in range(8)]

        threads = [
            threading.Thread(
                target=chooser_mod.CustomAssetChooser.show_for_path, args=(stub, p)
            )
            for p in paths
        ]
        finisher = threading.Thread(
            target=chooser_mod.CustomAssetChooser._finish_build, args=(stub,)
        )

        for t in threads[:4]:
            t.start()
        finisher.start()
        for t in threads[4:]:
            t.start()
        for t in threads + [finisher]:
            t.join(timeout=5)
            assert not t.is_alive()

        assert sorted(ran) == sorted(paths), (
            f"lost/duplicated tasks: delivered {sorted(ran)}, wanted {sorted(paths)}"
        )
        assert stub.build_task_finished_tasks == []


# 4. IconPackChooserStack races a two-flag build against the deferred task.

class _StubChooser:
    def __init__(self):
        self.build_finished = False


def _make_stack_stub(ran):
    """Stub carrying what Stack.show_for_path and on_load_finished touch.

    A patched gl.icon_pack_manager with an empty pack set records the path, so
    the GTK-heavy tail never runs and only the dispatch logic is observed.
    """
    class Stub:
        pass

    stub = Stub()
    stub.on_loads_finished_tasks = []
    stub._loads_lock = threading.Lock()
    stub.pack_chooser = _StubChooser()
    stub.icon_chooser = _StubChooser()

    # get_is_build_finished is the real method and reads the two flags.
    stub.get_is_build_finished = lambda: stack_mod.IconPackChooserStack.get_is_build_finished(stub)

    class FakePackManager:
        def get_icon_packs(self):
            # Called only once show_for_path passes the build-finished gate.
            # Record the delivery, then return nothing, so the loop is a no-op.
            ran.append(stub._current_path)
            return {}

    # show_for_path takes only (self, path). Stash the path, so the patched
    # manager can record which delivery reached the post-lock body.
    real_show = stack_mod.IconPackChooserStack.show_for_path

    def show_for_path(path):
        stub._current_path = path
        return real_show(stub, path)

    stub.show_for_path = show_for_path
    stub._orig_pack_manager = stack_mod.gl.icon_pack_manager
    stack_mod.gl.icon_pack_manager = FakePackManager()
    return stub


def _restore_stack_stub(stub):
    stack_mod.gl.icon_pack_manager = stub._orig_pack_manager


def test_iconpack_raced_deferred_task_runs() -> None:
    """Replay the losing interleaving with the two-flag icon-pack build.

    show_for_path reads one flag as False and stops inside its append. The
    build then finishes. The deferred task must still run.
    """
    ran = []
    stub = _make_stack_stub(ran)
    try:
        # Pack chooser already done; icon chooser still building.
        stub.pack_chooser.build_finished = True

        gate_reached = threading.Event()
        proceed = threading.Event()

        class GatedList(list):
            def append(self, item):
                gate_reached.set()
                assert proceed.wait(timeout=5), "test wiring: proceed never set"
                super().append(item)

        stub.on_loads_finished_tasks = GatedList()

        t_caller = threading.Thread(
            target=stub.show_for_path, args=("raced",), name="raced_show_for_path"
        )
        t_caller.start()
        assert gate_reached.wait(timeout=5), "show_for_path never tried to defer its task"

        # The icon build finishes now. The flag flips, then on_load_finished
        # runs. Without the lock this drains an empty queue and the raced
        # append lands afterwards, stranded forever.
        def finish():
            stub.icon_chooser.build_finished = True
            stack_mod.IconPackChooserStack.on_load_finished(stub)

        t_finish = threading.Thread(target=finish, name="on_load_finished")
        t_finish.start()

        time.sleep(0.1)
        assert not ran, "on_load_finished must not complete while the append is mid-flight"

        proceed.set()
        t_caller.join(timeout=5)
        t_finish.join(timeout=5)
        assert not t_caller.is_alive() and not t_finish.is_alive()

        assert ran == ["raced"], (
            f"the raced deferred task must run exactly once, got {ran!r}"
        )
        assert stub.on_loads_finished_tasks == [], "no task may be left stranded"
    finally:
        _restore_stack_stub(stub)


def test_iconpack_drain_runs_task_once() -> None:
    """Both build threads can enter the drain at the same time.

    Drainer 1 blocks inside its first task while drainer 2 enters. The lock
    makes drainer 1 snapshot and clear the queue before any task runs, so
    drainer 2 finds it empty. Every task must run exactly once.
    """
    ran = []
    order_lock = threading.Lock()  # serialize appends to the ran list
    stub = _make_stack_stub(ran)
    try:
        first_task_entered = threading.Event()
        release_first_task = threading.Event()
        paths = [f"i{i}" for i in range(6)]

        # Seed the queue directly with instrumented tasks and bypass the
        # GTK-heavy show_for_path body, because the drain is under test and not
        # the dispatch tail. The first task gates; the rest record immediately.
        def make_task(p, is_first):
            def task():
                if is_first:
                    first_task_entered.set()
                    assert release_first_task.wait(timeout=5), "wiring: never released"
                with order_lock:
                    ran.append(p)
            return task

        stub.on_loads_finished_tasks = [
            make_task(p, i == 0) for i, p in enumerate(paths)
        ]

        stub.pack_chooser.build_finished = True
        stub.icon_chooser.build_finished = True

        d1 = threading.Thread(
            target=stack_mod.IconPackChooserStack.on_load_finished, args=(stub,),
            name="drainer1",
        )
        d1.start()
        assert first_task_entered.wait(timeout=5), "drainer1 never entered its first task"

        # drainer2 enters while drainer1 is parked in task[0].
        d2 = threading.Thread(
            target=stack_mod.IconPackChooserStack.on_load_finished, args=(stub,),
            name="drainer2",
        )
        d2.start()
        d2.join(timeout=5)
        # Without the lock, d2 re-snapshots the full queue, re-runs task[0] and
        # blocks on the same gate, so it is still alive here. With the lock, d1
        # already cleared the queue, so d2 finds nothing and returns.
        assert not d2.is_alive(), (
            "drainer2 did not return -- it re-entered the drain and re-ran a "
            "task (double-run) instead of seeing the queue already cleared"
        )

        release_first_task.set()
        d1.join(timeout=5)
        assert not d1.is_alive()

        assert sorted(ran) == sorted(paths), (
            f"double-run/lost tasks: delivered {sorted(ran)}, wanted {sorted(paths)}"
        )
        assert stub.on_loads_finished_tasks == []
    finally:
        _restore_stack_stub(stub)


def test_iconpack_post_finish_dispatches_directly() -> None:
    ran = []
    stub = _make_stack_stub(ran)
    try:
        stub.pack_chooser.build_finished = True
        stub.icon_chooser.build_finished = True
        stub.show_for_path("direct")
        assert ran == ["direct"]
        assert stub.on_loads_finished_tasks == []
    finally:
        _restore_stack_stub(stub)


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_assetui_recycler")
    test_children_not_shown_before_bound()
    test_page_swap_atomic_clears_selection()
    test_filter_shrink_hides_leftovers_atomically()
    test_set_asset_binds_synchronously()
    test_raced_deferred_task_still_runs()
    test_post_finish_calls_dispatch_directly()
    test_enqueue_storm_loses_no_tasks()
    test_iconpack_raced_deferred_task_runs()
    test_iconpack_drain_runs_task_once()
    test_iconpack_post_finish_dispatches_directly()
    print("scenario_assetui_recycler: PASS")


if __name__ == "__main__":
    main()
