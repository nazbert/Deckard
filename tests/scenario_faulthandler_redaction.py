"""
Scenario for issue #122 (stacked on !6 / issue #105): faulthandler.log must
not bypass log redaction.

The hole: faulthandler writes its dumps at the C level straight to the
stored fd -- by design, so they still land when the interpreter is wedged --
which means the issue-#105 loguru patcher never sees them, and traceback
frame paths (File "/home/<user>/...") reach disk raw. A live Python-level
intercept is impossible without breaking that wedged-interpreter guarantee.

The fix under test: log_hooks.redirect_faulthandler() scrubs the EXISTING
file content through log_redaction.scrub() (atomic tmp + os.replace) before
appending the next boot marker and re-attaching faulthandler. That covers
the sharing scenario -- the file is only ever read after a restart -- while
accepting the documented residual: a dump from the CURRENT session stays raw
until the next boot.

Covers:

  1. first boot over a seeded raw dump (the "previous session crashed"
     case): home -> ~, username path segments -> <user>, URL credentials ->
     ***@ (scrub() is reused wholesale, so all !6 rules apply), previous
     boot marker preserved, new marker appended AFTER the scrubbed content;
  2. fd attachment: faulthandler is enabled on the REWRITTEN file (inode
     check -- if the scrub ran after the append-open, the fd would point at
     a replaced, unlinked inode and every future dump would be invisible),
     proven live via signal.raise_signal(SIGQUIT) landing a real dump;
  3. the residual, explicitly: that current-session dump is raw on disk;
  4. next boot (module state reset = process restart): the session-2 marker
     is appended and the session-1 dump is now scrubbed -- frame paths
     ~-relative but still identifiable (file name, "most recent call first");
  5. edge cases: file absent (first boot ever) and an unreadable/invalid
     path (path is a directory) -- _scrub_fault_log must never raise, and
     redirect_faulthandler must still attach over a scrub failure.

Run in isolation (run_all.py gives each scenario its own interpreter):
faulthandler.enable/register and log_hooks._fault_file are process-global.
"""
import fixtures  # must be first: isolates DATA_PATH before any src import

import faulthandler
import getpass
import os
import signal
import stat

from src.backend import log_hooks

HOME = os.path.expanduser("~")
USER = getpass.getuser()

BOOT_MARKER = "===== boot "

SEEDED_DUMP = f"""
{BOOT_MARKER}2026-07-01T09:00:00 pid=11111 =====
Fatal Python error: Segmentation fault

Current thread 0x00007f0a1c2b3c00 (most recent call first):
  File "{HOME}/dev/StreamController/src/backend/DeckManagement/DeckController.py", line 512 in _write
  File "{HOME}/dev/StreamController/plugins/store_plugin/main.py", line 88 in fetch
  File "/run/media/{USER}/stick/sideloaded/plugin.py", line 3 in <module>
  File "{HOME}/dev/StreamController/main.py", line 40 in <module>
remote = https://alice:hunter2@git.example.com/repo.git
"""


def read_log(path: str) -> str:
    with open(path) as f:
        return f.read()


def simulate_restart() -> None:
    # In a real run the module-level _fault_file reference lives for the
    # whole process (faulthandler stores the raw fd -- see log_hooks);
    # clearing it is exactly what a process restart does to module state.
    log_hooks._fault_file = None


def main() -> None:
    log_dir = os.path.join(fixtures.DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "faulthandler.log")

    # --- Edge cases first: neither may raise. ------------------------------
    log_hooks._scrub_fault_log(os.path.join(log_dir, "does-not-exist.log"))
    assert not os.path.exists(os.path.join(log_dir, "does-not-exist.log")), (
        "scrubbing an absent file must not create it"
    )
    log_hooks._scrub_fault_log(log_dir)  # path is a directory: log + continue

    # --- Boot 1: seeded raw dump from a "previous session". ----------------
    with open(log_path, "w") as f:
        f.write(SEEDED_DUMP)
    # The scrub's tmp+replace must not silently restamp the log's mode: a
    # user who chose a non-default mode keeps it. Seed 0640 -- deliberately
    # different from BOTH mkstemp's 0600 (so a dropped os.chmod leaves 0600,
    # failing this assert) AND the umask default (so a naive open()-based
    # tmp would leave 0644). Only copying the source mode yields 0640.
    os.chmod(log_path, 0o640)

    log_hooks.redirect_faulthandler(log_dir)
    content = read_log(log_path)

    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o640, (
        "boot scrub changed the log's mode (tmp+replace must preserve it)"
    )

    # Raw PII gone -- all !6 rule classes, since scrub() is reused wholesale.
    assert HOME not in content, "raw home path survived the boot scrub"
    assert "hunter2" not in content, "URL password survived the boot scrub"
    assert f"/run/media/{USER}/" not in content, "username path segment survived"

    # Redacted forms present and the dump still debuggable.
    assert '  File "~/dev/StreamController/main.py", line 40 in <module>' in content
    assert "/run/media/<user>/stick/sideloaded/plugin.py" in content
    assert "https://***@git.example.com/repo.git" in content, (
        "URL host/path must survive; only the userinfo is redacted"
    )
    assert "Fatal Python error: Segmentation fault" in content
    assert "(most recent call first):" in content

    # Previous boot marker preserved; new marker appended AFTER the scrubbed
    # content (the file must end with it).
    assert f"{BOOT_MARKER}2026-07-01T09:00:00 pid=11111 =====" in content
    assert content.count(BOOT_MARKER) == 2, "boot 1 must append exactly one new marker"
    assert content.rstrip("\n").endswith("====="), "boot marker must be the last line"

    # faulthandler attached, to the REWRITTEN file: scrub must happen before
    # the append fd is opened, or dumps would go to a replaced, unlinked inode.
    assert faulthandler.is_enabled(), "faulthandler must end up enabled"
    assert log_hooks._fault_file is not None
    assert os.fstat(log_hooks._fault_file.fileno()).st_ino == os.stat(log_path).st_ino, (
        "faulthandler fd must point at the scrubbed file's inode "
        "(scrub ran after the append-open?)"
    )

    # --- Session dump through the real fd: fd-attachment proof + residual. -
    size_before = len(content)
    signal.raise_signal(signal.SIGQUIT)  # registered with chain=False: dumps, no core
    content = read_log(log_path)
    session_dump = content[size_before:]
    assert "(most recent call first):" in session_dump, (
        "SIGQUIT dump did not land in faulthandler.log -- fd not attached"
    )
    assert "scenario_faulthandler_redaction.py" in session_dump
    # The documented residual: a CURRENT-session dump is raw until next boot.
    assert HOME in session_dump, (
        "expected the live dump to be raw (C-level write); if this starts "
        "failing, the residual-risk comments in log_hooks are stale"
    )

    # --- Boot 2: the residual is cleaned up by the next restart. -----------
    simulate_restart()
    log_hooks.redirect_faulthandler(log_dir)
    content = read_log(log_path)

    assert HOME not in content, "session-1 dump must be scrubbed at the next boot"
    assert 'File "~/' in content, "frame paths must stay identifiable as ~-relative"
    assert "scenario_faulthandler_redaction.py" in content, "frame file name must survive"
    assert content.count(BOOT_MARKER) == 3, "boot 2 must append exactly one new marker"
    assert content.rstrip("\n").endswith("=====")
    assert faulthandler.is_enabled()
    assert os.fstat(log_hooks._fault_file.fileno()).st_ino == os.stat(log_path).st_ino

    # No scrub temp file left behind.
    leftovers = [n for n in os.listdir(log_dir) if n.startswith("faulthandler.log.")]
    assert not leftovers, f"scrub temp files left behind: {leftovers}"

    print("PASS: scenario_faulthandler_redaction")


if __name__ == "__main__":
    main()
