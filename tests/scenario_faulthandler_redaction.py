"""faulthandler.log must not bypass log redaction.

faulthandler writes at the C level to the stored fd, so redirect_faulthandler()
scrubs the existing file before it appends the next boot marker and re-attaches.
"""
import fixtures  # must be first; isolates DATA_PATH before any src import

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
    # In a real run the module-level _fault_file reference lives for the whole
    # process, because faulthandler stores the raw fd. Clearing it is what a
    # process restart does to module state.
    log_hooks._fault_file = None


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_faulthandler_redaction")
    log_dir = os.path.join(fixtures.DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "faulthandler.log")

    # Edge cases first. Neither may raise.
    log_hooks._scrub_fault_log(os.path.join(log_dir, "does-not-exist.log"))
    assert not os.path.exists(os.path.join(log_dir, "does-not-exist.log")), (
        "scrubbing an absent file must not create it"
    )
    log_hooks._scrub_fault_log(log_dir)  # path is a directory, so log and continue

    # Boot 1, over a seeded raw dump from a previous session.
    with open(log_path, "w") as f:
        f.write(SEEDED_DUMP)
    # The tmp-and-replace of the scrub must not restamp the log mode, so a user
    # who chose a non-default mode keeps it. Seed 0640, which differs from both
    # the 0600 of mkstemp, so a dropped os.chmod leaves 0600 and fails this
    # assert, and the umask default, so a naive open would leave 0644. Only
    # copying the source mode yields 0640.
    os.chmod(log_path, 0o640)

    log_hooks.redirect_faulthandler(log_dir)
    content = read_log(log_path)

    assert stat.S_IMODE(os.stat(log_path).st_mode) == 0o640, (
        "boot scrub changed the log's mode (tmp+replace must preserve it)"
    )

    # Raw PII is gone, in every rule class, because scrub() is reused wholesale.
    assert HOME not in content, "raw home path survived the boot scrub"
    assert "hunter2" not in content, "URL password survived the boot scrub"
    assert f"/run/media/{USER}/" not in content, "username path segment survived"

    # The redacted forms are present and the dump is still debuggable.
    assert '  File "~/dev/StreamController/main.py", line 40 in <module>' in content
    assert "/run/media/<user>/stick/sideloaded/plugin.py" in content
    assert "https://***@git.example.com/repo.git" in content, (
        "URL host/path must survive; only the userinfo is redacted"
    )
    assert "Fatal Python error: Segmentation fault" in content
    assert "(most recent call first):" in content

    # The previous boot marker survives, and the new marker is appended after
    # the scrubbed content, so the file must end with it.
    assert f"{BOOT_MARKER}2026-07-01T09:00:00 pid=11111 =====" in content
    assert content.count(BOOT_MARKER) == 2, "boot 1 must append exactly one new marker"
    assert content.rstrip("\n").endswith("====="), "boot marker must be the last line"

    # faulthandler is attached to the rewritten file. The scrub must happen
    # before the append fd opens, or dumps go to a replaced, unlinked inode.
    assert faulthandler.is_enabled(), "faulthandler must end up enabled"
    assert log_hooks._fault_file is not None
    assert os.fstat(log_hooks._fault_file.fileno()).st_ino == os.stat(log_path).st_ino, (
        "faulthandler fd must point at the scrubbed file's inode "
        "(scrub ran after the append-open?)"
    )

    # A session dump through the real fd proves the attachment.
    size_before = len(content)
    signal.raise_signal(signal.SIGQUIT)  # registered with chain=False, so no core
    content = read_log(log_path)
    session_dump = content[size_before:]
    assert "(most recent call first):" in session_dump, (
        "SIGQUIT dump did not land in faulthandler.log -- fd not attached"
    )
    assert "scenario_faulthandler_redaction.py" in session_dump
    # The known limitation. A current-session dump stays raw until next boot.
    assert HOME in session_dump, (
        "expected the live dump to be raw (C-level write); if this starts "
        "failing, the residual-risk comments in log_hooks are stale"
    )

    # Boot 2. The next restart scrubs the raw dump left by this session.
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

    # No scrub temp file is left behind.
    leftovers = [n for n in os.listdir(log_dir) if n.startswith("faulthandler.log.")]
    assert not leftovers, f"scrub temp files left behind: {leftovers}"

    print("PASS: scenario_faulthandler_redaction")


if __name__ == "__main__":
    main()
