"""
Scenario: page edits are written once per burst, and at every boundary.

`Page.save()` used to be the write: one json.dump plus an fsync of the file
and an fsync of the directory, inline, on whichever thread edited the page --
the GTK main thread for every editor widget, once per keystroke. It now marks
the page and arms a trailing timer, so a burst of edits costs one write. That
is only safe if two things hold, and this scenario holds them:

  1. WHAT READS THE FILE SEES THE PAGE. Every reader goes through a barrier
     that writes pending edits out first, and the pending record is retired
     only AFTER the bytes are written -- retiring it first would leave a
     window where the map says "clean" while the file is still stale, and the
     barrier's fast path would wave a reader straight through to it. Asserted
     from inside the write itself, which is the only place that window exists.

  2. EVERY BOUNDARY A USER READS AS "DONE" WRITES NOW. Leaving the page,
     closing the deck, quitting, renaming the page. Deletion is the exception
     that proves it: pending edits for a deleted page are DISCARDED, because
     flushing them would write the file back into existence.

Timing is driven, never waited on: the flush's scheduler and clock are
constructor arguments, so this runs in virtual time with no sleeps -- the
trailing delay, the re-arm and the max-dirty-age cap are assertions about
what was armed, not about what happened to have fired yet.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import ast
import gc
import json
import os

from fixtures import make_headless_controller, seed_page, start_watchdog, teardown

import globals as gl
from src.backend.PageManagement import page_flush

WATCHDOG_SECONDS = 90

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_PY = os.path.join(REPO_ROOT, "src", "app.py")

# Every atomic_write_json the flush seam performed, as
# (path, path_still_marked_pending_while_writing, virtual_time).
WRITES: list[tuple[str, bool, float]] = []


def written_paths() -> list[str]:
    return [path for path, _pending, _at in WRITES]


class VirtualTime:
    """The flush seam's clock and timer source in one, on virtual time.

    Modelled on the timer wheel it replaces: a timer fires when the clock
    reaches its due time, so `advance()` is what a second of wall clock does
    to the process -- no sleeps, and the moment a write happens is a number
    the test can assert on.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.armed: dict[int, tuple[float, object]] = {}   # handle -> (due, callback)
        self.cancelled: list[int] = []
        self.arm_log: list[float] = []                     # every delay armed, in order
        self._next_handle = 1

    # -- clock --
    def __call__(self) -> float:
        return self.now

    # -- Scheduler --
    def schedule(self, delay_s, callback):
        handle = self._next_handle
        self._next_handle += 1
        self.armed[handle] = (self.now + delay_s, callback)
        self.arm_log.append(delay_s)
        return handle

    def cancel(self, handle):
        # Tolerates a handle that already fired, exactly like the wheel's:
        # the flush cancels the timer of the entry it just wrote, which is
        # the very timer that may have called it.
        self.cancelled.append(handle)
        self.armed.pop(handle, None)

    # -- driving --
    def advance(self, seconds: float) -> int:
        """Run the clock forward, firing every timer as its due time passes.
        Callbacks may arm more; those fire too if they fall inside the window.
        """
        target = self.now + seconds
        fired = 0
        while True:
            due = [(due_at, handle) for handle, (due_at, _cb) in self.armed.items()
                   if due_at <= target]
            if not due:
                break
            due_at, handle = min(due)
            _due_at, callback = self.armed.pop(handle)
            self.now = max(self.now, due_at)
            callback()
            fired += 1
        self.now = target
        return fired

    def fire_all(self) -> int:
        """Fire everything armed, however far out it is."""
        if not self.armed:
            return 0
        furthest = max(due_at for due_at, _cb in self.armed.values())
        return self.advance(max(0.0, furthest - self.now))


# The virtual time the flush seam currently runs on.
VT = VirtualTime()


def install_write_recorder() -> None:
    """Counts writes, and records whether the path was still marked pending
    while its bytes were going down -- the retire-AFTER-write ordering -- and
    the virtual moment the write happened."""
    real_write = page_flush.atomic_write_json

    def recording_write(path, data):
        WRITES.append((path, page_flush.get().pending_page(path) is not None, VT.now))
        real_write(path, data)

    page_flush.atomic_write_json = recording_write


def fresh_flush() -> VirtualTime:
    """A clean flush seam on virtual time, installed process-wide.

    Every production caller reaches the seam through `page_flush.get()`, so
    replacing the singleton is the injection point for the whole process.
    """
    global VT
    VT = VirtualTime()
    page_flush._flush = page_flush.PageFlush(scheduler=VT, clock=VT)
    WRITES.clear()
    return VT


def read_page(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def edit(page, value) -> None:
    """One user-visible edit: mutate the dict, persist it the way every
    setter in Page does."""
    page.dict["debounce-marker"] = value
    page.save()


def check_burst_is_one_write(controller) -> None:
    vt = fresh_flush()
    path = seed_page("Burst")
    page = gl.page_manager.get_page(path, controller)

    started = vt.now
    for _ in range(12):
        vt.advance(0.05)          # typing speed: never a full second of quiet
    for i in range(12):
        edit(page, i)

    assert WRITES == [], f"marking an edit wrote to disk: {WRITES}"
    assert len(vt.armed) == 1, (
        f"a burst left {len(vt.armed)} timers armed -- each edit must "
        f"cancel and re-arm the one trailing timer for its path")
    assert len(vt.cancelled) == 11, (
        f"expected 11 cancellations for 12 edits, got {len(vt.cancelled)}")
    assert set(vt.arm_log) == {page_flush.DEBOUNCE_S}, (
        f"a trailing timer was armed with something other than the debounce: "
        f"{vt.arm_log}")

    marked_at = vt.now
    assert vt.advance(page_flush.DEBOUNCE_S - 0.01) == 0, (
        "the write went out before the debounce window closed")
    assert vt.advance(0.02) == 1
    assert len(WRITES) == 1 and WRITES[0][0] == path, (
        f"12 edits produced {len(WRITES)} writes: {WRITES}")
    assert abs(WRITES[0][2] - (marked_at + page_flush.DEBOUNCE_S)) < 1e-9, (
        f"the write landed at {WRITES[0][2] - started:.2f}s, not one debounce "
        f"after the last edit")
    assert read_page(path)["debounce-marker"] == 11, "the write is not the last edit"
    assert page_flush.get().pending_page(path) is None, "the written page is still pending"
    print("PASS: a burst of edits is one write, one debounce after the last of them")


def check_read_barrier_sees_pending_edits(controller) -> None:
    vt = fresh_flush()
    path = seed_page("Barrier")
    page = gl.page_manager.get_page(path, controller)

    edit(page, "typed-but-not-written")
    assert WRITES == []

    data = gl.page_manager.get_page_data(path)
    assert data.get("debounce-marker") == "typed-but-not-written", (
        "a read of the page file mid-burst returned the stale file: the read "
        "barrier is missing or ran after the load")
    assert len(WRITES) == 1, f"the barrier did not write exactly once: {WRITES}"
    assert WRITES[0][1] is True, (
        "the pending record was retired BEFORE the bytes were written -- a "
        "concurrent reader would take the barrier's fast path and read the "
        "file as it was")
    assert page_flush.get().pending_page(path) is None, (
        "the pending record survived its own write")
    assert not vt.armed, (
        "the flush left its timer armed: it would fire again with nothing to do")
    print("PASS: a read mid-burst observes the pending edits, and the mark "
          "outlives its own write")


def check_max_dirty_age_cap(controller) -> None:
    vt = fresh_flush()
    path = seed_page("Cap")
    page = gl.page_manager.get_page(path, controller)

    first_marked = vt.now
    edit(page, "keystroke-0")
    assert vt.arm_log[-1] == page_flush.DEBOUNCE_S

    # Continuous editing: never a full second of quiet, so the trailing timer
    # on its own would be pushed back for as long as the user keeps typing.
    for i in range(1, 15):
        vt.advance(0.4)
        if WRITES:
            break
        edit(page, f"keystroke-{i}")

    assert len(WRITES) == 1, (
        f"continuous editing produced {len(WRITES)} writes in 5.6 virtual "
        f"seconds -- the max dirty age cap did not fire exactly once")
    assert abs(WRITES[0][2] - (first_marked + page_flush.MAX_DIRTY_AGE_S)) < 1e-9, (
        f"the capped write landed {WRITES[0][2] - first_marked:.2f}s after the "
        f"page first went dirty, not at the {page_flush.MAX_DIRTY_AGE_S}s cap")
    assert read_page(path).get("debounce-marker") is not None

    # And the cap starts a fresh window rather than pinning the page to a
    # write every MAX_DIRTY_AGE_S forever.
    edit(page, "after-the-cap")
    assert vt.arm_log[-1] == page_flush.DEBOUNCE_S, (
        f"the edit after a capped write was armed for {vt.arm_log[-1]}s "
        f"instead of a full trailing window")
    vt.advance(page_flush.DEBOUNCE_S)
    assert len(WRITES) == 2
    assert read_page(path)["debounce-marker"] == "after-the-cap"
    print("PASS: continuous editing cannot defer a write past the max dirty "
          "age, and the cap opens a fresh window")


def check_page_switch_flushes(controller) -> None:
    vt = fresh_flush()
    old_page = controller.active_page
    old_path = old_page.json_path
    edit(old_page, "left-behind")

    other_path = seed_page("SwitchTarget")
    other = gl.page_manager.get_page(other_path, controller)
    controller.load_page(other, allow_reload=True)

    assert written_paths() == [old_path], (
        f"leaving a page did not write its pending edits: {WRITES}")
    assert read_page(old_path)["debounce-marker"] == "left-behind"
    assert page_flush.get().pending_page(old_path) is None
    assert not vt.armed, "the switch flushed but left the timer armed"
    print("PASS: switching page writes the outgoing page")


def check_deck_close_flushes() -> None:
    vt = fresh_flush()
    closing = make_headless_controller(serial="debounce-closing", page_name="Closing")
    try:
        page = closing.active_page
        assert page is not None, "the fixture controller has no active page"
        path = page.json_path
        edit(page, "written-on-close")

        # A page this deck visited earlier and still has cached, dirty and
        # not on screen: closing drops its cache entry too, so its edits have
        # nowhere left to live either.
        cached_path = seed_page("ClosingCached")
        cached = gl.page_manager.get_page(cached_path, closing)
        edit(cached, "cached-but-dirty")

        closing.close(remove_media=True)
    finally:
        teardown(closing)

    for written, marker in ((path, "written-on-close"), (cached_path, "cached-but-dirty")):
        assert written in written_paths(), (
            f"closing the deck did not write {os.path.basename(written)}: {WRITES}")
        assert read_page(written)["debounce-marker"] == marker
        assert page_flush.get().pending_page(written) is None
    assert not vt.armed
    print("PASS: closing a deck writes every page it still holds")


def check_flush_all_covers_quit(controller) -> None:
    vt = fresh_flush()
    paths = []
    for name in ("QuitA", "QuitB"):
        path = seed_page(name)
        page = gl.page_manager.get_page(path, controller)
        edit(page, f"unsaved-{name}")
        paths.append(path)

    # What App.on_quit calls: the timers are daemon threads and the process
    # is about to os._exit, so nothing else would write these.
    page_flush.get().flush_all()

    assert sorted(written_paths()) == sorted(paths), (
        f"flush_all did not write every dirty page: {WRITES}")
    for path, name in zip(paths, ("QuitA", "QuitB")):
        assert read_page(path)["debounce-marker"] == f"unsaved-{name}"
        assert page_flush.get().pending_page(path) is None
    assert not vt.armed
    print("PASS: flush_all writes every page with edits outstanding")


def check_quit_flush_placement() -> None:
    """The quit flush must sit behind the force-quit watchdog, like every
    other unbounded write on that path: two fsyncs with no timeout of their
    own cost 6s and a force_quit behind it, and hang the quit ahead of it."""
    with open(APP_PY) as f:
        tree = ast.parse(f.read())

    quits = [node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name == "on_quit"]
    assert len(quits) == 1, f"expected exactly one on_quit in app.py, found {len(quits)}"

    watchdog_line = flush_line = None
    for node in ast.walk(quits[0]):
        if not isinstance(node, ast.Call):
            continue
        text = ast.unparse(node)
        if "force_quit" in text and "schedule" in text:
            watchdog_line = node.lineno
        elif "flush_all" in text:
            flush_line = node.lineno

    assert watchdog_line is not None, "on_quit no longer arms the force-quit watchdog"
    assert flush_line is not None, (
        "on_quit does not flush pending page edits -- a quit within the "
        "debounce window loses the last edit, because the flush timers are "
        "daemon threads and this process ends in os._exit")
    assert watchdog_line < flush_line, (
        f"the page flush (line {flush_line}) is armed before the force-quit "
        f"watchdog (line {watchdog_line}): a wedged filesystem would hang the "
        f"quit with nothing armed to end it")
    print("PASS: the quit flush runs on the force-quit watchdog's clock")


def check_move_flushes_then_discards(controller) -> None:
    vt = fresh_flush()
    old_path = seed_page("MoveMe")
    page = gl.page_manager.get_page(old_path, controller)
    edit(page, "carried-across")

    new_path = os.path.join(gl.page_manager.PAGE_PATH, "Moved.json")
    gl.page_manager.move_page(old_path, new_path)

    assert written_paths() == [old_path], (
        f"the move did not write the source page before copying it: {WRITES}")
    assert read_page(new_path)["debounce-marker"] == "carried-across", (
        "the renamed page arrived without the edits that were still pending")
    assert not os.path.exists(old_path)
    assert page.json_path == new_path, "the cached Page was not re-pointed"
    assert page_flush.get().pending_page(old_path) is None, (
        "a write is still pending for the file the move removed")

    vt.fire_all()
    assert not os.path.exists(old_path), (
        "a timer wrote the moved-from page back into existence")
    assert written_paths() == [old_path], (
        f"a stale timer wrote after the move: {WRITES}")
    print("PASS: a move writes the source, then retires its pending write")


def check_delete_discards(controller) -> None:
    vt = fresh_flush()
    path = seed_page("DeleteMe")
    page = gl.page_manager.get_page(path, controller)
    assert controller.active_page is not page
    edit(page, "never-written")

    gl.page_manager.remove_page(path)

    assert not os.path.exists(path)
    assert page_flush.get().pending_page(path) is None, (
        "the deleted page still has a write pending")
    assert [p for p in written_paths() if p == path] == [], (
        f"the delete wrote the page it was deleting: {WRITES}")

    vt.fire_all()
    assert not os.path.exists(path), (
        "a pending write resurrected the deleted page file")
    print("PASS: deleting a page discards its pending write, and nothing "
          "resurrects it")


def check_eviction_keeps_pending_edits(controller) -> None:
    """Eviction never touches disk, so a page evicted mid-window would take
    its unwritten edits with it -- unless the flush seam holds it."""
    vt = fresh_flush()
    path = seed_page("Evicted")
    page = gl.page_manager.get_page(path, controller)
    edit(page, "survives-eviction")
    del page

    original_max = gl.page_manager.max_pages
    try:
        gl.page_manager.set_pages_to_cache(0)   # keeps the active page only
        gc.collect()

        cached = gl.page_manager.pages.get(controller, {}).get(path)
        assert cached is None, "the page under test was not evicted"
        pending = page_flush.get().pending_page(path)
        assert pending is not None, (
            "the evicted page's pending edits were dropped -- the flush seam "
            "must hold a strong reference to a page whose file is behind")

        data = gl.page_manager.get_page_data(path)
        assert data.get("debounce-marker") == "survives-eviction", (
            "re-reading an evicted page returned the file without the edits "
            "that were still pending when it was evicted")
        assert len(WRITES) == 1
    finally:
        gl.page_manager.max_pages = original_max
    vt.fire_all()
    print("PASS: an evicted page keeps its pending edits, and the next read "
          "sees them")


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_page_write_debounce")
    install_write_recorder()
    controller = make_headless_controller(serial="debounce-1", page_name="Main")
    try:
        check_burst_is_one_write(controller)
        check_read_barrier_sees_pending_edits(controller)
        check_max_dirty_age_cap(controller)
        check_page_switch_flushes(controller)
        check_deck_close_flushes()
        check_flush_all_covers_quit(controller)
        check_quit_flush_placement()
        check_move_flushes_then_discards(controller)
        check_delete_discards(controller)
        check_eviction_keeps_pending_edits(controller)
    finally:
        teardown(controller)

    print("PASS: scenario_page_write_debounce")


if __name__ == "__main__":
    main()
