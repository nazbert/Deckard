"""
Scenario: two writers of one page file, and neither loses.

A page could be changed two ways at once. A deck's Page mutated its dict and
saved; the page-settings writers (every override row in the Page Editor, the
whole-page editor, the asset sweep) read the whole page FILE, changed the
dict they got back, and wrote the whole file again. The two shared no lock
and no dict, so an edit landing between the settings writer's read and its
write was written over -- and once the settings writer told the page to
re-read itself, the page's own copy was overwritten too. Whichever of the two
edits the interleaving picked was gone from the file AND from memory, with no
error anywhere. Every override row and every plugin `set_settings` re-rolled
the dice.

There is no read-modify-write of a page file left to lose one. Both writers
now change the same dict -- the page's content, held once per file -- under
the same per-file lock, and the file is written from that content by the one
seam that writes page files at all.

THE INTERLEAVING, MADE DETERMINISTIC

The window is the settings write's own body, so the pause has to go where
that write commits -- and that differs between the two mechanisms this
closes, because the defect IS the gap between reading and writing: an
implementation without one has no gap to pause in. Both commit points are
hooked here and the first to fire opens the window:

  * the whole-file write (`save_settings_to_file`), which is where the old
    mechanism has just finished reading the file and is about to overwrite
    it, and
  * the flush seam's write, which is where the new one hands the page's
    content to its file.

The window opens, a plugin thread lands the save that `ActionCore.set_settings`
makes, and the window closes. Afterwards BOTH edits must be on the page and
in the file -- they touch different parts of the page, so there is no version
of "correct" in which one of them is missing.

Timers are disarmed for the whole scenario: every write here is one the test
asks for by name, so what interleaves with what is the test's choice and not
the wheel's.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
import threading

import globals as gl
from fixtures import make_headless_controller, start_watchdog, teardown

from src.backend.PageManagement import page_flush

WATCHDOG_SECONDS = 90

# How long any wait in here may take before the interleaving is declared
# broken rather than slow.
HANDOFF_TIMEOUT_S = 20


class NoTimers:
    """A timer source that arms nothing.

    Every write in this scenario is one the test asks for, so the moment a
    page reaches its file is a line in the test rather than a race with a
    background thread.
    """

    def schedule(self, delay_s, callback):
        return object()

    def cancel(self, handle):
        pass


def fresh_flush() -> None:
    """A flush seam that writes only when told, installed process-wide.

    Every production caller reaches the seam through `page_flush.get()`, so
    replacing the singleton is the injection point for the whole process.
    """
    page_flush._flush = page_flush.PageFlush(scheduler=NoTimers())


def read_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def seed_page_with_action(name: str) -> str:
    """A page carrying one action with settings -- what a plugin writes to."""
    path = os.path.join(gl.page_manager.PAGE_PATH, f"{name}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({
            "keys": {
                "0x0": {
                    "states": {
                        "0": {
                            "actions": [
                                {"id": "com_example_Plugin::Action",
                                 "settings": {"volume": 1}},
                            ],
                        },
                    },
                },
            },
            "dials": {},
            "touchscreens": {},
            "settings": {"screensaver": {"enable": False, "time-delay": 60}},
        }, f)
    return path


def action_settings(content: dict) -> dict:
    return content["keys"]["0x0"]["states"]["0"]["actions"][0].get("settings", {})


class Window:
    """The pause the plugin thread's save lands in.

    One-shot: the settings write commits once, and the writes that follow it
    (the pending save going out, the final settle) must not stall on a
    handshake nobody is waiting on the other end of.
    """

    def __init__(self, path: str):
        self.path = os.path.realpath(path)
        self.open_now = threading.Event()      # the plugin thread may edit
        self.edit_done = threading.Event()     # ...and has
        self.fired = False
        self._guard = threading.Lock()

    def reached(self, path: str) -> None:
        with self._guard:
            if self.fired or os.path.realpath(str(path)) != self.path:
                return
            self.fired = True
        self.open_now.set()
        assert self.edit_done.wait(HANDOFF_TIMEOUT_S), (
            "the plugin thread never landed its save inside the settings write")


def install_commit_hooks(window: Window) -> None:
    """Pause at whichever point this tree's settings write commits."""
    real_save_settings = gl.settings_manager.save_settings_to_file
    real_atomic_write = page_flush.atomic_write_json

    def hooked_save_settings(file_path, settings):
        window.reached(file_path)
        return real_save_settings(file_path, settings)

    def hooked_atomic_write(path, data):
        window.reached(path)
        return real_atomic_write(path, data)

    gl.settings_manager.save_settings_to_file = hooked_save_settings
    page_flush.atomic_write_json = hooked_atomic_write


def remove_commit_hooks(saved) -> None:
    gl.settings_manager.save_settings_to_file = saved[0]
    page_flush.atomic_write_json = saved[1]


def check_a_plugin_save_and_a_settings_write_both_survive(controller) -> int:
    """The traced interleaving: a plugin thread saves the page while the
    page's settings are being written."""
    fresh_flush()
    path = seed_page_with_action("LostUpdate")
    page = gl.page_manager.get_page(path, controller)

    window = Window(path)
    saved_hooks = (gl.settings_manager.save_settings_to_file, page_flush.atomic_write_json)
    install_commit_hooks(window)

    def plugin_thread() -> None:
        # What ActionCore.set_settings reaches: the action's settings inside
        # the page, changed in place, and then Page.save(). Written out here
        # rather than called through Page.set_action_dict because that needs
        # a live action object, which needs a plugin manager this harness
        # deliberately has no room for.
        try:
            if not window.open_now.wait(HANDOFF_TIMEOUT_S):
                return
            action_settings(page.dict)["volume"] = 42
            page.save()
        finally:
            window.edit_done.set()

    plugin = threading.Thread(target=plugin_thread, name="plugin-set-settings")
    plugin.start()
    try:
        # The Page Editor's screensaver row.
        gl.page_manager.overwrite_screensaver_settings(path, enable=True)
        # The new mechanism commits at the flush rather than inside the call
        # above, so ask for the write the user's next page switch would.
        page_flush.get().flush_path(path)
        if not window.fired:
            window.open_now.set()
            plugin.join(HANDOFF_TIMEOUT_S)
            print("FAIL: the settings write never reached a commit point -- "
                  "nothing was interleaved, so this check proves nothing")
            return 1
        plugin.join(HANDOFF_TIMEOUT_S)
        page_flush.get().flush_all()
    finally:
        remove_commit_hooks(saved_hooks)

    on_disk = read_file(path)
    if on_disk.get("settings", {}).get("screensaver", {}).get("enable") is not True:
        print(f"FAIL: the settings write was lost -- a page save landed inside it: "
              f"{on_disk.get('settings')}")
        return 1
    if action_settings(on_disk).get("volume") != 42:
        print(f"FAIL: the plugin's action settings were lost -- a settings write "
              f"landed around it: {action_settings(on_disk)}")
        return 1

    # ...and the page in memory says the same thing, which is the half that
    # decides what the NEXT save writes.
    if page.dict.get("settings", {}).get("screensaver", {}).get("enable") is not True:
        print(f"FAIL: the page lost the settings write: {page.dict.get('settings')}")
        return 1
    if action_settings(page.dict).get("volume") != 42:
        print(f"FAIL: the page lost the plugin's edit: {action_settings(page.dict)}")
        return 1

    print("PASS: a plugin's save inside a page-settings write loses neither")
    return 0


def check_whole_page_replace_outlives_a_pending_edit(controller) -> int:
    """The whole-page editor against an edit that has not reached the file.

    Replacing a page means the caller's content wins -- but it has to win in
    the page and in the file together. Writing the file alone left the page
    holding what it held before, so the edit still on its timer wrote the
    pre-replacement page straight back over the replacement, and the page
    manager's own re-read then adopted it. No thread needed: the mark is the
    other writer.
    """
    fresh_flush()
    path = seed_page_with_action("ReplaceCross")
    page = gl.page_manager.get_page(path, controller)

    page.dict["pending-edit"] = "marked-not-written"
    page.save()
    if read_file(path).get("pending-edit") is not None:
        print("FAIL: the edit reached the file inline -- nothing is pending, "
              "so this check proves nothing")
        return 1

    replacement = {
        "keys": {}, "dials": {}, "touchscreens": {},
        "settings": {"brightness": {"overwrite": True, "value": 7}},
        "replaced": True,
    }
    gl.page_manager.set_page_data(path, replacement)
    page_flush.get().flush_all()

    on_disk = read_file(path)
    if on_disk.get("replaced") is not True:
        print(f"FAIL: the page replacement was undone by an edit still on its "
              f"timer: {on_disk}")
        return 1
    if on_disk.get("settings", {}).get("brightness", {}).get("value") != 7:
        print(f"FAIL: the replacement's settings never reached the file: {on_disk}")
        return 1
    if page.dict.get("replaced") is not True:
        print(f"FAIL: the page still holds the pre-replacement content: {page.dict}")
        return 1
    if page.dict.get("pending-edit") is not None:
        print(f"FAIL: a replaced page kept a key the replacement does not have: "
              f"{page.dict}")
        return 1

    print("PASS: replacing a whole page outlives an edit still on its timer")
    return 0


def check_no_loss_under_contention(controller) -> int:
    """The same two writers, unsynchronised, with writes going out underneath
    them: the file must end up saying exactly what the page says.

    An invariant net rather than a second reproducer -- the window the checks
    above open by hand is sub-microsecond when nobody holds it open, so this
    one is not expected to catch the defect. What it does catch is the cost
    of closing it: three parties now contend for one lock per page file, and
    a settings edit, a page save and a write of the same page running flat
    out against each other must neither deadlock nor leave the file and the
    page disagreeing.
    """
    fresh_flush()
    path = seed_page_with_action("Contention")
    page = gl.page_manager.get_page(path, controller)

    rounds = 200
    failures: list[str] = []

    def settings_writer() -> None:
        try:
            for i in range(rounds):
                gl.page_manager.overwrite_brightness_settings(path, brightness=i)
        except Exception as e:  # noqa: BLE001 -- reported, not swallowed
            failures.append(f"settings writer raised: {e!r}")

    def page_writer() -> None:
        try:
            for i in range(rounds):
                action_settings(page.dict)["volume"] = i
                page.save()
        except Exception as e:  # noqa: BLE001
            failures.append(f"page writer raised: {e!r}")

    def flusher() -> None:
        try:
            for _ in range(rounds):
                page_flush.get().flush_path(path)
        except Exception as e:  # noqa: BLE001
            failures.append(f"flusher raised: {e!r}")

    threads = [threading.Thread(target=target, name=target.__name__)
               for target in (settings_writer, page_writer, flusher)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(HANDOFF_TIMEOUT_S)
        if thread.is_alive():
            print(f"FAIL: {thread.name} never finished -- the writers are "
                  f"deadlocked against each other")
            return 1

    if failures:
        print(f"FAIL: {failures}")
        return 1

    page_flush.get().flush_all()

    on_disk = read_file(path)
    in_memory = page.get_without_action_objects()
    if on_disk != in_memory:
        print(f"FAIL: the file and the page disagree after concurrent edits\n"
              f"  file: {on_disk}\n  page: {in_memory}")
        return 1
    if on_disk.get("settings", {}).get("brightness", {}).get("value") != rounds - 1:
        print(f"FAIL: the last settings edit is not the one that stands: "
              f"{on_disk.get('settings')}")
        return 1
    if action_settings(on_disk).get("volume") != rounds - 1:
        print(f"FAIL: the last page edit is not the one that stands: "
              f"{action_settings(on_disk)}")
        return 1

    print("PASS: concurrent page and settings edits leave the file saying "
          "exactly what the page says")
    return 0


def check_asset_sweep_edits_the_page_it_finds(controller) -> int:
    """Deleting an asset strips it out of a page a deck is showing.

    The sweep is the third writer of a page file, and the only one that walks
    every page rather than one: for the pages this process is holding it goes
    through the page, and for the rest it still reads and writes the file --
    there is nothing else to hold those. The page it finds must end up
    stripped in memory and in its file, and an edit of that page still on its
    timer must survive being swept.
    """
    fresh_flush()
    asset = os.path.join(gl.DATA_PATH, "asset.png")
    with open(asset, "w") as f:
        f.write("not really a png")

    held = seed_page_with_action("SweptHeld")
    with open(held) as f:
        content = json.load(f)
    content["keys"]["0x0"]["states"]["0"]["media"] = {"path": asset}
    with open(held, "w") as f:
        json.dump(content, f)
    page = gl.page_manager.get_page(held, controller)

    unheld = os.path.join(gl.page_manager.PAGE_PATH, "SweptUnheld.json")
    with open(unheld, "w") as f:
        json.dump({"keys": {"0x0": {"states": {"0": {"media": {"path": asset}}}}}}, f)

    page.dict["pending-edit"] = "marked-not-written"
    page.save()

    gl.page_manager.remove_asset_from_all_pages(asset)
    page_flush.get().flush_all()

    if page.dict["keys"]["0x0"]["states"]["0"]["media"]["path"] is not None:
        print(f"FAIL: the swept page still points at the deleted asset: "
              f"{page.dict['keys']}")
        return 1
    if page.dict.get("pending-edit") != "marked-not-written":
        print(f"FAIL: the sweep dropped an edit that was still on its timer: "
              f"{page.dict}")
        return 1

    on_disk = read_file(held)
    if on_disk["keys"]["0x0"]["states"]["0"]["media"]["path"] is not None:
        print(f"FAIL: the swept page's file still points at the asset: {on_disk}")
        return 1
    if on_disk.get("pending-edit") != "marked-not-written":
        print(f"FAIL: the pending edit never reached the file: {on_disk}")
        return 1

    on_disk_unheld = read_file(unheld)
    if on_disk_unheld["keys"]["0x0"]["states"]["0"]["media"]["path"] is not None:
        print(f"FAIL: a page nothing is holding was not swept: {on_disk_unheld}")
        return 1

    print("PASS: an asset sweep strips a held page through the page and an "
          "unheld one through its file")
    return 0


def main() -> int:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_page_lost_update")
    controller = make_headless_controller(serial="lost-update-1")
    failures = 0
    try:
        for check in (
            check_a_plugin_save_and_a_settings_write_both_survive,
            check_whole_page_replace_outlives_a_pending_edit,
            check_asset_sweep_edits_the_page_it_finds,
            check_no_loss_under_contention,
        ):
            failures += check(controller)
    finally:
        teardown(controller)

    if failures:
        print(f"FAIL: scenario_page_lost_update ({failures} failed)")
        return 1
    print("PASS: scenario_page_lost_update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
