"""
Dying inside a deferred page write costs the burst, never the page.

A page write happens about a second after the edit, on a timer thread, so the
process can die during a write nobody asked for. A child makes its pre-burst
state durable, arms a fatal fsync, edits, and waits for the debounce timer.
The parent then reads the page, the residue and the backup it left behind.
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
    # The state the session opens with. The backup taken before this
    # session's first write must hold it afterwards.
    with open(path) as f:
        opening = json.load(f)
    page = Page(json_path=path, deck_controller=StubController("flush-crash-1"))

    # Make the pre-burst state durable the ordinary way. Mark the page, then
    # ask for the flush like every synchronous flush site does.
    page.dict["flush-marker"] = "pre-burst"
    page.save()
    page_flush.get().flush_path(path)
    with open(path) as f:
        pre_burst = json.load(f)

    print("PAGE_PATH " + path, flush=True)
    print("PRE_BURST " + json.dumps(pre_burst), flush=True)
    print("OPENING " + json.dumps(opening), flush=True)

    # Armed before the edits, so no write can slip through. From here on the
    # first fsync anywhere is fatal, and the pre-burst state above is the
    # last thing that reached the disk.
    real_fsync = os.fsync

    def dying_fsync(fd):
        real_fsync(fd)
        os._exit(9)

    os.fsync = dying_fsync

    for i in range(5):
        page.dict["flush-marker"] = f"burst-{i}"
        page.save()

    # Reaching this line proves the edits above wrote nothing.
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
    opening_lines = [line for line in lines if line.startswith("OPENING ")]
    assert page_path_lines and pre_burst_lines and opening_lines, (
        f"child never got as far as its pre-burst state: {proc.stdout}\n{proc.stderr}")

    page_path = page_path_lines[-1].removeprefix("PAGE_PATH ")
    pre_burst = json.loads(pre_burst_lines[-1].removeprefix("PRE_BURST "))
    opening = json.loads(opening_lines[-1].removeprefix("OPENING "))
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
            on_disk = json.load(f)   # a raise here means the file was truncated
        assert on_disk == pre_burst, (
            f"the page file is no longer the pre-burst state after a crash "
            f"inside the deferred write: {on_disk}")
        assert on_disk.get("flush-marker") == "pre-burst", (
            "a burst edit reached the primary even though the write never "
            "completed")

        # 2. The only residue is the writer's own temp. A partial file under
        #    the page's real name is what atomicity exists to prevent.
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

        # 3. The heal path is untouched. get_page_data recovers a corrupt
        #    page from this file, and it still holds the complete page this
        #    session found on disk. The copy is taken once, before the
        #    session's first write, so the crashed write is neither in it nor
        #    half of it.
        backup_path = os.path.join(pages_dir, "backups", basename)
        with open(backup_path) as f:
            backup = json.load(f)
        assert backup == opening, (
            f"the backup the corrupt-page heal reads from was left holding "
            f"something other than the page this session opened with: {backup}")
        assert backup.get("flush-marker") is None, (
            "a write of this session reached the backup: the copy is taken "
            "before the first of them and is not refreshed by any")
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
