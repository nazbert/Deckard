"""
Scenario: dying inside a DEFERRED page write costs the burst, never the page.

Page writes no longer happen in the mutator that made the edit -- they happen
about a second later, on a timer thread. That moves the fsync pair off the
GTK main thread, and it moves the crash window with it: the process can now
die *during* a write nobody asked for at that moment. What that must cost is
exactly the unwritten burst, and nothing else.

The technique is scenario_atomic_settings' (`os.fsync` replaced with one that
`os._exit(9)`s, in a child with its own data dir), aimed at the deferred
write instead of an inline one. The child makes its pre-burst state durable,
arms the trap, edits, and then does nothing at all -- so the fatal fsync can
only come from the debounce timer firing on its own. That the child prints
"MARKED" before dying is itself the proof the write was deferred: with an
inline save it would have died in `save()`, before that line.

Afterwards the parent inspects the child's surviving data dir:

  * the page file is the complete pre-burst JSON -- not truncated, not the
    half-serialized burst,
  * the only residue is the writer's own never-renamed temp, under the
    `.save-*.tmp` prefix a later write to that target reaps -- nothing
    partial under the page's real name,
  * `pages/backups/<page>.json`, which `get_page_data` heals a corrupt page
    from, still holds that same complete JSON. A crashed flush leaves the
    recovery path exactly as it found it.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import glob
import json
import os
import shutil
import subprocess
import sys
import threading

from fixtures import FaultyFakeDeck, start_watchdog

WATCHDOG_SECONDS = 120
CHILD_ENV = "DECKARD_PAGE_FLUSH_CRASH_CHILD"


class StubController:
    """Everything Page dereferences on its controller, and no more."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def run_child() -> None:
    """Dies inside the debounce timer's write. Never returns."""
    from src.backend.PageManagement import page_flush
    from src.backend.PageManagement.Page import Page

    fixtures._install_integration_globals()
    path = fixtures.seed_page("Crash")
    page = Page(json_path=path, deck_controller=StubController("flush-crash-1"))

    # Pre-burst state, made durable the ordinary way: mark, then ask for the
    # flush like every synchronous flush site does.
    page.dict["flush-marker"] = "pre-burst"
    page.save()
    page_flush.get().flush_path(path)
    with open(path) as f:
        pre_burst = json.load(f)

    print("PAGE_PATH " + path, flush=True)
    print("PRE_BURST " + json.dumps(pre_burst), flush=True)

    # Armed BEFORE the edits, so there is no window in which a write could
    # slip through: from here on the first fsync anywhere is fatal, and the
    # pre-burst state above is the last thing that reached the disk.
    real_fsync = os.fsync

    def dying_fsync(fd):
        real_fsync(fd)
        os._exit(9)

    os.fsync = dying_fsync

    for i in range(5):
        page.dict["flush-marker"] = f"burst-{i}"
        page.save()

    # Reaching this line at all is the point: the edits above did not write.
    print("MARKED", flush=True)

    # Nothing left to do but let the debounce timer come round. It fires on
    # its own thread, writes, and dies in the fsync.
    threading.Event().wait(30)
    print("NEVER_FLUSHED", flush=True)
    os._exit(3)


def main() -> None:
    start_watchdog(WATCHDOG_SECONDS, label="scenario_page_flush_crash")

    env = dict(os.environ)
    env[CHILD_ENV] = "1"
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__)],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )

    lines = proc.stdout.splitlines()
    marked = [line for line in lines if line == "MARKED"]
    page_path_lines = [line for line in lines if line.startswith("PAGE_PATH ")]
    pre_burst_lines = [line for line in lines if line.startswith("PRE_BURST ")]
    assert page_path_lines and pre_burst_lines, (
        f"child never got as far as its pre-burst state: {proc.stdout}\n{proc.stderr}")

    page_path = page_path_lines[-1].removeprefix("PAGE_PATH ")
    pre_burst = json.loads(pre_burst_lines[-1].removeprefix("PRE_BURST "))
    data_path = os.path.dirname(os.path.dirname(page_path))

    try:
        assert marked, (
            "the child died before it finished marking its edits -- the write "
            "is still happening inline in save(), not on the debounce timer")
        assert "NEVER_FLUSHED" not in proc.stdout, (
            "the child survived 30s of holding unwritten edits: either the "
            "debounce timer never fired, or the write stopped going through "
            "the atomic writer -- a plain open('w') never fsyncs, so it "
            f"cannot reach the trap\n{proc.stderr}")
        assert proc.returncode == 9, (
            f"child should have died in the deferred write's fsync, "
            f"rc={proc.returncode}: {proc.stderr}")

        # 1. The primary is the previous complete page, byte-for-byte the
        #    state that was durable before the burst.
        with open(page_path) as f:
            on_disk = json.load(f)   # raises -> the file was truncated, which is the point
        assert on_disk == pre_burst, (
            f"the page file is no longer the pre-burst state after a crash "
            f"inside the deferred write: {on_disk}")
        assert on_disk.get("flush-marker") == "pre-burst", (
            "a burst edit reached the primary even though the write never "
            "completed")

        # 2. Residue: the writer's own temp, nothing else. A partial file
        #    under the page's real name is what atomicity exists to prevent.
        pages_dir = os.path.dirname(page_path)
        basename = os.path.basename(page_path)
        temps = glob.glob(os.path.join(pages_dir, f".save-{basename}.*.tmp"))
        assert len(temps) == 1, (
            f"expected exactly one never-renamed temp from the interrupted "
            f"write, found {temps}")
        stray = [
            name for name in os.listdir(pages_dir)
            if name not in (basename, "backups") and os.path.basename(temps[0]) != name
        ]
        assert not stray, f"the crashed flush left {stray} behind in pages/"
        assert not glob.glob(os.path.join(pages_dir, "*.corrupt*")), (
            "something quarantined the page: a crashed write must leave the "
            "primary loadable, so nothing has cause to")

        # 3. The heal path is untouched: get_page_data recovers a corrupt
        #    page from this file, and it still holds the last durable state.
        backup_path = os.path.join(pages_dir, "backups", basename)
        with open(backup_path) as f:
            backup = json.load(f)
        assert backup == pre_burst, (
            f"the backup the corrupt-page heal reads from was left holding "
            f"something other than the last durable page: {backup}")
    finally:
        shutil.rmtree(data_path, ignore_errors=True)

    print("PASS: a crash inside a deferred page write costs the burst and "
          "leaves the page, its residue rule and its backup intact")
    print("PASS: scenario_page_flush_crash")


if __name__ == "__main__":
    if os.environ.get(CHILD_ENV):
        run_child()
    else:
        main()
