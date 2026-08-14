"""
Page edits are written once per burst, and at every boundary.

Page.save marks the page and arms a trailing timer, so a burst of edits costs
one write. Every reader crosses a barrier that writes pending edits first, and
every boundary a user reads as "done" writes now.
"""

# A virtual clock drives the timing with no sleeps, so the delay, the re-arm
# and the cap are assertions.
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import ast
import gc
import json
import os
import shutil
import threading
import time

from fixtures import make_headless_controller, seed_page, start_watchdog, teardown

import globals as gl
from src.backend.PageManagement import page_flush

WATCHDOG_SECONDS = 120

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO_ROOT, "src")
APP_PY = os.path.join(SRC, "app.py")
MENU_BUTTON_PY = os.path.join(SRC, "windows", "PageManager", "elements", "MenuButton.py")
SC_IMPORTER_PY = os.path.join(SRC, "windows", "PageManager", "Importer", "StreamController",
                              "StreamController.py")
SDUI_IMPORTER_PY = os.path.join(SRC, "windows", "PageManager", "Importer", "StreamDeckUI",
                                "StreamDeckUI.py")

# Every atomic_write_json the flush seam performed, as
# (path, path_still_marked_pending_while_writing, virtual_time).
WRITES: list[tuple[str, bool, float]] = []

# Every copy into pages/backups/, as (source page path, destination).
BACKUPS: list[tuple[str, str]] = []


def written_paths() -> list[str]:
    return [path for path, _pending, _at in WRITES]


def backups_of(path: str) -> list[tuple[str, str]]:
    return [copy for copy in BACKUPS if copy[0] == path]


class VirtualTime:
    """The flush seam's clock and timer source in one, on virtual time.

    A timer fires when the clock reaches its due time, so advance() does to
    the process what a second of wall clock does. No sleeps run, and the
    moment of a write is a number the checks can read.
    """

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.armed: dict[int, tuple[float, object]] = {}   # handle -> (due, callback)
        self.cancelled: list[int] = []
        self.arm_log: list[float] = []                     # every delay armed, in order
        self._next_handle = 1

    # clock
    def __call__(self) -> float:
        return self.now

    # scheduler
    def schedule(self, delay_s, callback):
        handle = self._next_handle
        self._next_handle += 1
        self.armed[handle] = (self.now + delay_s, callback)
        self.arm_log.append(delay_s)
        return handle

    def cancel(self, handle):
        # Tolerates a handle that already fired, like the wheel does. The
        # flush cancels the timer of the entry it just wrote, and that timer
        # may be the one that called it.
        self.cancelled.append(handle)
        self.armed.pop(handle, None)

    # driving
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


class ManualScheduler:
    """A timer source whose handles fire one at a time, when told.

    VirtualTime models the wheel's clock and this models its concurrency. A
    fire runs on a real thread, so a mark can land while a write is in
    flight, which is the interleaving that decides whether an edit lands.
    """

    def __init__(self):
        self.armed: dict[int, object] = {}
        self.cancelled: list[int] = []
        self.last_handle = 0

    def schedule(self, delay_s, callback):
        self.last_handle += 1
        self.armed[self.last_handle] = callback
        return self.last_handle

    def cancel(self, handle):
        self.cancelled.append(handle)
        self.armed.pop(handle, None)

    def fire(self, handle) -> None:
        callback = self.armed.pop(handle, None)
        if callback is not None:
            callback()


class CountingFlush:
    """A flush seam that records what it was asked to do and does nothing.

    Some barrier sites hold nothing but the ask. The write they would cause
    belongs to another check's assertions.
    """

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def mark_dirty(self, page) -> None:
        self.calls.append(("mark_dirty", page.json_path))

    def pending_source(self, path):
        return None

    def flush_path(self, path) -> None:
        self.calls.append(("flush_path", path))

    def flush_all(self) -> None:
        self.calls.append(("flush_all", None))

    def discard_path(self, path) -> None:
        self.calls.append(("discard_path", path))


# The virtual time the flush seam currently runs on.
VT = VirtualTime()


def install_write_recorder() -> None:
    """Counts writes. Records whether the path was still marked pending while
    its bytes went down, which pins the retire-after-write order, and records
    the virtual moment of the write."""
    real_write = page_flush.atomic_write_json

    def recording_write(path, data):
        WRITES.append((path, page_flush.get().pending_source(path) is not None, VT.now))
        real_write(path, data)

    page_flush.atomic_write_json = recording_write


def install_backup_recorder() -> None:
    """Counts the copies into pages/backups/.

    The hook sits on shutil.copy2 rather than Page.make_backup, because half
    of what the checks assert is a copy that must not happen. make_backup is
    entered for a corrupt primary and then declines.
    """
    real_copy2 = shutil.copy2
    backups_dir = os.path.join("pages", "backups")

    def recording_copy2(src, dst, *args, **kwargs):
        if os.path.dirname(str(dst)).endswith(backups_dir):
            BACKUPS.append((str(src), str(dst)))
        return real_copy2(src, dst, *args, **kwargs)

    shutil.copy2 = recording_copy2


def backup_path_of(path: str) -> str:
    return os.path.join(gl.page_manager.PAGE_PATH, "backups", os.path.basename(path))


def write_page(path: str, data: dict) -> None:
    """A page file written by somebody other than the flush seam, such as an
    importer, a migrator or the boot restore."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def fresh_flush() -> VirtualTime:
    """A clean flush seam on virtual time, installed process-wide.

    Every production caller reaches the seam through page_flush.get(), so
    replacing the singleton is the injection point for the whole process. A
    new seam is also a new session, so each check owns its own first copy.
    """
    global VT
    VT = VirtualTime()
    page_flush._flush = page_flush.PageFlush(scheduler=VT, clock=VT)
    WRITES.clear()
    BACKUPS.clear()
    return VT


def read_page(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def edit(page, value) -> None:
    """One user-visible edit. Mutate the dict, then persist it the way every
    setter in Page does."""
    page.dict["debounce-marker"] = value
    page.save()


def check_burst_is_one_write(controller) -> None:
    vt = fresh_flush()
    path = seed_page("Burst")
    page = gl.page_manager.get_page(path, controller)

    started = vt.now
    for _ in range(12):
        vt.advance(0.05)          # typing speed, never a full second of quiet
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
    assert page_flush.get().pending_source(path) is None, "the written page is still pending"
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
    assert page_flush.get().pending_source(path) is None, (
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

    # Continuous editing never leaves a full second of quiet, so the trailing
    # timer alone would move back for as long as the user types.
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

    # The cap starts a fresh window instead of pinning the page to a write
    # every MAX_DIRTY_AGE_S.
    edit(page, "after-the-cap")
    assert vt.arm_log[-1] == page_flush.DEBOUNCE_S, (
        f"the edit after a capped write was armed for {vt.arm_log[-1]}s "
        f"instead of a full trailing window")
    vt.advance(page_flush.DEBOUNCE_S)
    assert len(WRITES) == 2
    assert read_page(path)["debounce-marker"] == "after-the-cap"
    print("PASS: continuous editing cannot defer a write past the max dirty "
          "age, and the cap opens a fresh window")


def check_mid_write_mark_survives(controller) -> None:
    """An edit made while a write is in flight must not be swallowed by it.

    The write in flight serializes a snapshot taken before that edit existed,
    so retiring the pending record it finds afterwards drops an edit.
    """
    # No timer comes back for that edit, and both outcomes leave the map empty
    # and the file written, so nothing else can tell them apart.
    scheduler = ManualScheduler()
    page_flush._flush = page_flush.PageFlush(scheduler=scheduler, clock=time.monotonic)
    WRITES.clear()
    path = seed_page("MidWrite")
    page = gl.page_manager.get_page(path, controller)

    in_write = threading.Event()
    release = threading.Event()
    recorder = page_flush.atomic_write_json

    def blocking_write(target, data):
        in_write.set()
        assert release.wait(20), "the write was never released"
        recorder(target, data)

    page_flush.atomic_write_json = blocking_write
    try:
        edit(page, "before-the-write")
        first_handle = scheduler.last_handle

        firing = threading.Thread(target=scheduler.fire, args=(first_handle,),
                                  name="mid-write-flush")
        firing.start()
        assert in_write.wait(20), "the deferred write never started"

        edit(page, "during-the-write")
        second_handle = scheduler.last_handle
        assert second_handle != first_handle, (
            "the mid-write mark did not arm a timer of its own")

        release.set()
        firing.join(20)
        assert not firing.is_alive(), "the flush thread never finished"
    finally:
        page_flush.atomic_write_json = recorder
        release.set()

    assert page_flush.get().pending_source(path) is not None, (
        "the write retired a pending record it did not write -- the edit made "
        "while it was serializing is now unreachable, and nothing will raise "
        "or log about it")
    assert second_handle in scheduler.armed, (
        "the mid-write mark's timer was cancelled by the write that did not "
        "include it: nothing is left to write that edit")

    scheduler.fire(second_handle)
    assert read_page(path)["debounce-marker"] == "during-the-write", (
        "the edit made during the write never reached disk")
    assert page_flush.get().pending_source(path) is None
    print("PASS: an edit made during a write keeps its own record and its own "
          "timer, and lands")


def check_flush_writes_locked_path(controller) -> None:
    """The file written is the key the edits were marked under, never whatever
    json_path says when the flush runs.

    A page move re-points json_path in place, so the two names can differ while
    a mark is outstanding.
    """
    # With this invariant a flush and its lock can never be about different
    # files.
    scheduler = ManualScheduler()
    page_flush._flush = page_flush.PageFlush(scheduler=scheduler, clock=time.monotonic)
    WRITES.clear()
    marked_path = seed_page("Diverge")
    other_path = seed_page("DivergeOther")
    page = gl.page_manager.get_page(marked_path, controller)

    edit(page, "belongs-to-the-marked-path")
    other_before = read_page(other_path)

    # The re-point, exactly as move_page performs it, with a mark outstanding.
    page.json_path = other_path
    try:
        page_flush.get().flush_path(marked_path)
    finally:
        page.json_path = marked_path

    assert read_page(marked_path)["debounce-marker"] == "belongs-to-the-marked-path", (
        "the flush wrote somewhere other than the path it locked")
    assert read_page(other_path) == other_before, (
        "the flush wrote the page's CURRENT json_path instead of the path it "
        "locked and was asked for -- two files, one lock, and the wrong one "
        "overwritten")

    backup = os.path.join(gl.page_manager.PAGE_PATH, "backups", os.path.basename(marked_path))
    assert os.path.exists(backup), (
        "no backup was taken for the file being overwritten")
    print("PASS: a flush writes -- and backs up -- the path it locked, not the "
          "page's current json_path")


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
    assert page_flush.get().pending_source(old_path) is None
    assert not vt.armed, "the switch flushed but left the timer armed"
    print("PASS: switching page writes the outgoing page")


def check_deck_close_flushes() -> None:
    vt = fresh_flush()
    # A deck with no default page loads whichever page sorts first, which by
    # now belongs to another check. Name this deck's page before the
    # controller exists, and hold the fixture to it.
    serial = "debounce-closing"
    active_path = seed_page("Closing")
    gl.page_manager.set_default_page(serial, active_path)

    closing = make_headless_controller(serial=serial, page_name="Closing")
    try:
        page = closing.active_page
        assert page is not None, "the fixture controller has no active page"
        path = page.json_path
        assert path == active_path, (
            f"the closing deck is showing {os.path.basename(path)}, not the "
            f"page this check is about")
        edit(page, "written-on-close")

        # A page this deck visited earlier, still cached, dirty and off
        # screen. Closing drops its cache entry too, so its edits have
        # nowhere left to go.
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
        assert page_flush.get().pending_source(written) is None
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

    # What App.on_quit calls. The timers are daemon threads and the process is
    # about to os._exit, so nothing else writes these.
    page_flush.get().flush_all()

    assert sorted(written_paths()) == sorted(paths), (
        f"flush_all did not write every dirty page: {WRITES}")
    for path, name in zip(paths, ("QuitA", "QuitB")):
        assert read_page(path)["debounce-marker"] == f"unsaved-{name}"
        assert page_flush.get().pending_source(path) is None
    assert not vt.armed
    print("PASS: flush_all writes every page with edits outstanding")


def check_quit_flush_placement() -> None:
    """The quit flush must sit behind the force-quit watchdog, like every
    other unbounded write on that path. Two fsyncs carry no timeout of their
    own, so a wedged filesystem hangs a quit with nothing armed to end it."""
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

    # A save landing mid-move, keyed under the path the move is about to
    # remove. The hook sits on the copy, between the move's flush and its
    # json_path re-point, which is the only window where a mark can still
    # take the old key. Without it the move's discard has nothing to discard.
    raced = {"landed": False}
    real_copy2 = shutil.copy2

    def copy2_then_race(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        if not raced["landed"]:
            raced["landed"] = True
            page.dict["raced-edit"] = "marked-mid-move"
            page.save()
        return result

    new_path = os.path.join(gl.page_manager.PAGE_PATH, "Moved.json")
    shutil.copy2 = copy2_then_race
    try:
        gl.page_manager.move_page(old_path, new_path)
    finally:
        shutil.copy2 = real_copy2

    assert raced["landed"], "the mid-move save never ran -- the check is vacuous"
    assert written_paths() == [old_path], (
        f"the move did not write the source page before copying it: {WRITES}")
    assert read_page(new_path)["debounce-marker"] == "carried-across", (
        "the renamed page arrived without the edits that were still pending")
    assert not os.path.exists(old_path)
    assert page.json_path == new_path, "the cached Page was not re-pointed"
    assert page_flush.get().pending_source(old_path) is None, (
        "a write is still pending for the file the move removed")

    vt.fire_all()
    assert not os.path.exists(old_path), (
        "a pending write resurrected the moved-from page")
    assert written_paths() == [old_path], (
        f"a stale timer wrote after the move: {WRITES}")

    # The raced edit left the pending map but stayed in memory. The next save
    # carries it to the page's new path.
    page.save()
    assert list(page_flush.get()._pending) == [new_path], (
        "a save after the move keyed the pending write under the old path")
    vt.fire_all()
    assert read_page(new_path)["raced-edit"] == "marked-mid-move", (
        "the edit made mid-move was lost rather than carried to the new path")
    print("PASS: a move writes the source and retires its pending write, and a "
          "save that raced it neither resurrects the old file nor is lost")


def check_delete_discards(controller) -> None:
    vt = fresh_flush()
    path = seed_page("DeleteMe")
    page = gl.page_manager.get_page(path, controller)
    assert controller.active_page is not page
    edit(page, "never-written")

    gl.page_manager.remove_page(path)

    assert not os.path.exists(path)
    assert page_flush.get().pending_source(path) is None, (
        "the deleted page still has a write pending")
    assert [p for p in written_paths() if p == path] == [], (
        f"the delete wrote the page it was deleting: {WRITES}")

    vt.fire_all()
    assert not os.path.exists(path), (
        "a pending write resurrected the deleted page file")
    print("PASS: deleting a page discards its pending write, and nothing "
          "resurrects it")


def check_backup_is_once_per_session(controller) -> None:
    """One copy into pages/backups/ per page per session, holding the file as
    the seam found it.

    A backup is read only when the primary will not parse, so the state worth
    keeping is the one from before this session started editing.
    """
    vt = fresh_flush()
    path = seed_page("SessionBackup")
    backup = backup_path_of(path)
    opening_state = read_page(path)
    page = gl.page_manager.get_page(path, controller)

    edit(page, "first-write")
    vt.advance(page_flush.DEBOUNCE_S)

    assert len(WRITES) == 1, f"expected one write, got {WRITES}"
    assert len(backups_of(path)) == 1, (
        f"the first write of a page took {len(backups_of(path))} backups "
        f"instead of one")
    assert read_page(backup) == opening_state, (
        "the backup was taken AFTER the write it protects against: it holds "
        "the bytes the write put down, not the ones it replaced")

    edit(page, "second-write")
    vt.advance(page_flush.DEBOUNCE_S)

    assert len(WRITES) == 2, f"expected a second write, got {WRITES}"
    assert len(backups_of(path)) == 1, (
        f"the second write took another backup ({len(backups_of(path))} in "
        f"total): the copy is per session, not per write")
    assert read_page(backup) == opening_state, (
        "a later write refreshed the backup, so it now holds this session's "
        "own output instead of the state it found the file in")
    assert read_page(path)["debounce-marker"] == "second-write"
    print("PASS: a page is copied into pages/backups/ once per session, "
          "before the first write, and later writes leave that copy alone")


def check_discard_reopens_backup(controller) -> None:
    """A discard hands the file to another writer, so the next flush of that
    path backs up what that writer left there.

    Every discard means the page at this path is not the page the backup
    describes.
    """
    # Carrying the "already backed up" record across would leave a heal
    # restoring a page that has since gone from this name.
    vt = fresh_flush()

    # The shape of an import. The pending state is discarded, because a flush
    # would land after the import and undo it, and the file is replaced
    # wholesale.
    path = seed_page("DiscardImportedOver")
    backup = backup_path_of(path)
    page = gl.page_manager.get_page(path, controller)

    edit(page, "before-the-import")
    vt.advance(page_flush.DEBOUNCE_S)
    assert len(backups_of(path)) == 1, "the first write took no backup"

    imported = {"keys": {}, "dials": {}, "touchscreens": {}, "imported": True}
    page_flush.get().discard_path(path)
    write_page(path, imported)

    edit(page, "after-the-import")
    vt.advance(page_flush.DEBOUNCE_S)

    assert len(backups_of(path)) == 2, (
        "the write after an import reused the session's backup record, so "
        "pages/backups/ still holds the page the import replaced -- a heal "
        "would undo the import")
    assert read_page(backup) == imported, (
        "the backup taken after the import is not what the import left on "
        "disk")

    # The shape of a delete. The file goes away and a new page takes the
    # name, so the old backup describes a page that has since gone.
    deleted_path = seed_page("DiscardDeleted")
    deleted_backup = backup_path_of(deleted_path)
    old_page = gl.page_manager.get_page(deleted_path, controller)
    assert controller.active_page is not old_page
    edit(old_page, "the-page-that-was-deleted")
    vt.advance(page_flush.DEBOUNCE_S)
    assert len(backups_of(deleted_path)) == 1

    gl.page_manager.remove_page(deleted_path)
    recreated = {"keys": {}, "dials": {}, "touchscreens": {}, "recreated": True}
    assert gl.page_manager.add_page("DiscardDeleted", recreated) == deleted_path
    new_page = gl.page_manager.get_page(deleted_path, controller)
    edit(new_page, "the-page-that-took-the-name")
    vt.advance(page_flush.DEBOUNCE_S)

    assert len(backups_of(deleted_path)) == 2, (
        "the re-created page was written over without a backup of its own: "
        "pages/backups/ still holds the deleted page, and healing this name "
        "would bring that one back")
    assert read_page(deleted_backup) == recreated, (
        "the backup of the re-created page is not the page that was created")
    print("PASS: a delete, a move or an import reopens the backup, and the "
          "next write copies what the new owner left on disk")


def check_discard_waits_for_flush(controller) -> None:
    """A discard arriving while a flush holds the path must wait for it.

    The flush finishes its backup bookkeeping after the copy, so a discard that
    slips through mid-copy is overwritten by the record the flush adds on its
    way out. Real threads open that window.
    """
    # The flush's own write then lands on top of the file the importer wrote.
    scheduler = ManualScheduler()
    page_flush._flush = page_flush.PageFlush(scheduler=scheduler, clock=time.monotonic)
    WRITES.clear()
    BACKUPS.clear()
    path = seed_page("DiscardRace")
    backup = backup_path_of(path)
    page = gl.page_manager.get_page(path, controller)

    # Stalled after the copy and before the write, the widest point of the
    # window, where the seam still has both of them to lose.
    in_flush = threading.Event()
    at_discard = threading.Event()
    release = threading.Event()
    real_make_backup = page.make_backup
    imported = {"keys": {}, "dials": {}, "touchscreens": {}, "imported": True}

    def stalling_make_backup(json_path=None):
        result = real_make_backup(json_path)
        in_flush.set()
        assert release.wait(20), "the stalled flush was never released"
        return result

    page.make_backup = stalling_make_backup
    try:
        edit(page, "pre-import")
        flusher = threading.Thread(target=scheduler.fire, args=(scheduler.last_handle,),
                                   name="discard-race-flush")
        flusher.start()
        assert in_flush.wait(20), "the deferred write never reached its backup"

        def importing():
            at_discard.set()
            page_flush.get().discard_path(path)
            write_page(path, imported)

        importer = threading.Thread(target=importing, name="discard-race-import")
        importer.start()
        assert at_discard.wait(20), "the importing thread never started"
        importer.join(0.5)
        assert importer.is_alive(), (
            "the discard returned while a flush held the path: that flush "
            "still has a write and a backup record to land, both of them on "
            "top of the import this discard is clearing the way for")

        release.set()
        flusher.join(20)
        importer.join(20)
        assert not flusher.is_alive(), "the stalled flush never finished"
        assert not importer.is_alive(), "the import never finished"
    finally:
        del page.make_backup
        release.set()

    assert read_page(path) == imported, (
        "the flush that was in flight wrote its pre-import page over the "
        "file the import had just written")
    assert len(backups_of(path)) == 1, (
        f"expected the pre-import page to have been backed up once: {BACKUPS}")

    # The next write of this path backs up the import's own content. That
    # works only if the record the discard cleared stayed cleared to the end
    # of the flush that was in flight.
    edit(page, "post-import")
    scheduler.fire(scheduler.last_handle)
    assert len(backups_of(path)) == 2, (
        "the write after the import reused a backup record the discard had "
        "cleared: pages/backups/ still holds the page the import replaced, "
        "and a heal would put it back")
    assert read_page(backup) == imported
    print("PASS: a discard waits for a flush already holding the path, so "
          "neither its write nor its bookkeeping outlives the handover")


def check_quarantined_primary_is_written_back(controller) -> None:
    """A page whose file is gone stays writable, because the write recreates
    it.

    A page can be live on screen with no primary behind it.
    """
    # The loader quarantines an unparseable page by renaming it aside, and
    # get_page_data then serves the backup.
    # Nothing to copy is a refusal like any other, and the write it guards
    # still runs and puts the page back.
    vt = fresh_flush()
    path = seed_page("Quarantined")
    quarantined = path + ".corrupt"
    backup = backup_path_of(path)
    page = gl.page_manager.get_page(path, controller)

    last_good = {"keys": {}, "dials": {}, "touchscreens": {}, "from": "before the quarantine"}
    write_page(backup, last_good)
    os.rename(path, quarantined)

    try:
        edit(page, "written-after-the-quarantine")
        vt.advance(page_flush.DEBOUNCE_S)

        assert os.path.exists(path), (
            "the page file was not recreated: the write raised on the "
            "missing primary, so nothing the user does to this page can be "
            "saved for the rest of the session")
        assert read_page(path)["debounce-marker"] == "written-after-the-quarantine"
        assert page_flush.get().pending_source(path) is None, (
            "the edit is still pending after its write")
        assert backups_of(path) == [], "a primary that was not there was copied"
        assert read_page(backup) == last_good, (
            "the recreated page was copied over the backup it was healed from")

        # The refusal stands for the session, like the corrupt one. The file
        # exists again, but it holds this seam's own output.
        edit(page, "and-again")
        vt.advance(page_flush.DEBOUNCE_S)
        assert backups_of(path) == []
        assert read_page(backup) == last_good
    finally:
        os.remove(quarantined)
    print("PASS: a page whose file was quarantined is written back rather "
          "than silently unwritable, and its backup is left alone")


def check_corrupt_primary_is_never_backed_up(controller) -> None:
    """A primary that will not parse is not copied over the backup, and that
    refusal stands for the session.

    A corrupt page is healed from the backup, so copying the corruption over it
    loses the page for good.
    """
    # A later write would find the primary parseable again, because this seam
    # wrote it a moment ago.
    vt = fresh_flush()
    path = seed_page("CorruptPrimary")
    backup = backup_path_of(path)
    page = gl.page_manager.get_page(path, controller)

    # A good copy from an earlier session, and a primary damaged since. The
    # truncation mid-object is what a full disk or a killed writer leaves.
    last_good = {"keys": {}, "dials": {}, "touchscreens": {}, "from": "before the damage"}
    write_page(backup, last_good)
    with open(path, "w") as f:
        f.write('{"keys": {"0x0": ')

    edit(page, "written-over-the-damage")
    vt.advance(page_flush.DEBOUNCE_S)

    assert len(WRITES) == 1, f"the write did not happen: {WRITES}"
    assert backups_of(path) == [], (
        "the unparseable primary was copied into pages/backups/ -- the only "
        "readable copy of the page has been replaced by the corruption it "
        "exists to survive")
    assert read_page(backup) == last_good
    assert read_page(path)["debounce-marker"] == "written-over-the-damage", (
        "refusing the backup also cost the write it was guarding")

    edit(page, "and-again")
    vt.advance(page_flush.DEBOUNCE_S)

    assert len(WRITES) == 2
    assert backups_of(path) == [], (
        "a later write took the backup the corrupt primary was refused")
    assert read_page(backup) == last_good, (
        "the last copy taken before the corruption was replaced by a "
        "duplicate of the file that is already on disk")

    # Garbage bytes rather than malformed JSON. Decoding fails before the
    # parser runs, which raises ValueError but not a JSON error. A
    # JSONDecodeError-only guard lets that through and drops the write.
    binary_path = seed_page("BinaryPrimary")
    binary_backup = backup_path_of(binary_path)
    binary_page = gl.page_manager.get_page(binary_path, controller)
    write_page(binary_backup, last_good)
    with open(binary_path, "wb") as f:
        f.write(b"\x00\xff\xfe not text at all")

    edit(binary_page, "written-over-the-garbage")
    vt.advance(page_flush.DEBOUNCE_S)

    assert read_page(binary_path)["debounce-marker"] == "written-over-the-garbage", (
        "an undecodable primary took the write down with it: the edit was "
        "dropped on a timer thread with only a log line to show for it")
    assert backups_of(binary_path) == [], (
        "a file of garbage bytes was copied into pages/backups/")
    assert read_page(binary_backup) == last_good
    print("PASS: an unparseable primary -- malformed or not text at all -- is "
          "never copied over its backup and never costs the write, and the "
          "refusal is not revisited later in the session")


def call_sites_in(module_path: str, func_name: str) -> list[tuple[int, str]]:
    """(line, source) for every call in one function, outermost call first."""
    with open(module_path) as f:
        tree = ast.parse(f.read())
    funcs = [node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name == func_name]
    assert len(funcs) == 1, (
        f"expected exactly one {func_name} in {os.path.basename(module_path)}, "
        f"found {len(funcs)}")
    return [(node.lineno, ast.unparse(node))
            for node in ast.walk(funcs[0]) if isinstance(node, ast.Call)]


def assert_barrier_precedes(module_path: str, func_name: str, barrier: str,
                            guarded: str, why: str) -> None:
    calls = call_sites_in(module_path, func_name)
    barriers = [line for line, src in calls if barrier in src]
    guards = [line for line, src in calls if guarded in src]
    where = f"{os.path.basename(module_path)}:{func_name}"
    assert guards, f"{where} no longer contains `{guarded}` -- this check has gone stale"
    assert barriers, f"{where} does not call `{barrier}`: {why}"
    assert min(barriers) < min(guards), (
        f"{where} calls `{barrier}` only AFTER `{guarded}`: {why}")


def check_every_reader_takes_barrier() -> None:
    """The six sites that touch a page file without going through
    get_page_data, each pinned so that removing its barrier turns red.

    A reader without a barrier sees a page as it was up to a second ago, and
    an importer without one has its work undone a second later.
    """
    # Two sites run headless through a counting seam. The four behind GTK are
    # pinned at the source, where the call must also come before the read or
    # the write it guards.
    counting = CountingFlush()
    page_flush._flush = counting

    gl.page_manager.backup_pages()
    assert ("flush_all", None) in counting.calls, (
        "the boot backup zips every page file without asking for pending "
        f"edits first: {counting.calls}")

    counting.calls.clear()
    from src.backend.DeckManagement.Subclasses import video_cache_sweeper
    video_cache_sweeper.collect_referenced_video_hashes()
    assert ("flush_all", None) in counting.calls, (
        "the video cache sweep reads every page file without asking for "
        f"pending edits first -- and deletes the caches it finds no reference to: {counting.calls}")

    assert_barrier_precedes(
        MENU_BUTTON_PY, "export_page_callback", "flush_path(page_path)", "open(page_path",
        "an export would ship the page as it was up to a second ago")
    assert_barrier_precedes(
        MENU_BUTTON_PY, "import_page_name_selected_callback",
        "flush_path(source_path)", "open(source_path",
        "duplicating a page would copy a stale version of what is on screen")
    assert_barrier_precedes(
        SC_IMPORTER_PY, "perform_import",
        "discard_path(page_path)", "self.save_json(page_path, page)",
        "a write pending for an imported-over page would land after the "
        "import and undo it")
    assert_barrier_precedes(
        SDUI_IMPORTER_PY, "perform_import",
        "discard_path(page_path)", "self.save_json(page_path, page)",
        "a write pending for an imported-over page would land after the "
        "import and undo it")
    print("PASS: every page-file reader outside get_page_data takes the "
          "barrier, and takes it first")


def check_eviction_keeps_pending_edits(controller) -> None:
    """Eviction never touches disk, so a page evicted mid-window takes its
    unwritten edits with it unless the flush seam holds a reference."""
    vt = fresh_flush()
    path = seed_page("Evicted")
    page = gl.page_manager.get_page(path, controller)
    edit(page, "survives-eviction")
    del page
    # The fetch above reserves the page against eviction until its caller
    # activates it or the deck fetches again. No caller exists here, so stand
    # in for one that moved on. Otherwise the reservation keeps the page
    # rather than the flush seam, and this check proves nothing.
    gl.page_manager.pins.release_fetch(controller)

    original_max = gl.page_manager.max_pages
    try:
        gl.page_manager.set_pages_to_cache(0)   # keeps the active page only
        gc.collect()

        cached = gl.page_manager.pages.get(controller, {}).get(path)
        assert cached is None, "the page under test was not evicted"
        pending = page_flush.get().pending_source(path)
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
    install_backup_recorder()
    controller = make_headless_controller(serial="debounce-1", page_name="Main")
    try:
        check_burst_is_one_write(controller)
        check_read_barrier_sees_pending_edits(controller)
        check_max_dirty_age_cap(controller)
        check_mid_write_mark_survives(controller)
        check_flush_writes_locked_path(controller)
        check_page_switch_flushes(controller)
        check_deck_close_flushes()
        check_flush_all_covers_quit(controller)
        check_quit_flush_placement()
        check_move_flushes_then_discards(controller)
        check_delete_discards(controller)
        check_backup_is_once_per_session(controller)
        check_discard_reopens_backup(controller)
        check_discard_waits_for_flush(controller)
        check_quarantined_primary_is_written_back(controller)
        check_corrupt_primary_is_never_backed_up(controller)
        check_eviction_keeps_pending_edits(controller)
        check_every_reader_takes_barrier()
    finally:
        teardown(controller)

    print("PASS: scenario_page_write_debounce")


if __name__ == "__main__":
    main()
