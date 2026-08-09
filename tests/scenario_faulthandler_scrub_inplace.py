"""
Regression test: the boot-time faulthandler scrub must preserve
the file's INODE.

The old scrub rewrote via tmp + os.replace. Atomic for the file -- but the
replace swaps the inode, and faulthandler registers the raw fd: every
already-RUNNING instance kept writing its crash/SIGQUIT dumps to the
unlinked old file. Field 2026-07-17: a short-lived third launch scrubbed,
and both long-running instances' dumps went to "faulthandler.log (deleted)"
-- a live thread dump was only recoverable via /proc/<pid>/fd.

This scenario simulates the running instance with a plain O_APPEND fd held
open across the scrub, then asserts: content is scrubbed, the inode is
unchanged, and a dump written through the pre-scrub fd still lands in the
file a reader would open.
"""
import os
import tempfile

import fixtures  # noqa: F401  -- sys.path setup for src imports

from src.backend.log_hooks import _scrub_fault_log
from src.backend.log_redaction import install_log_redaction, scrub


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_faulthandler_scrub_inplace")
    install_log_redaction()

    raw_line = f'  File "{os.path.expanduser("~")}/project/broken.py", line 1 in <module>\n'
    expected_line = scrub(raw_line)
    assert expected_line != raw_line, (
        "fixture sanity: scrub() must redact a home-path traceback line, "
        f"got it back unchanged: {raw_line!r}"
    )

    with tempfile.TemporaryDirectory(prefix="fh-scrub-") as d:
        path = os.path.join(d, "faulthandler.log")
        with open(path, "w") as f:
            f.write("===== boot 2026-01-01T00:00:00 pid=1 =====\n")
            f.write(raw_line)

        # The "running instance": faulthandler stores a raw fd, exactly like
        # this, and keeps writing through it after other boots come and go.
        running_fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        ino_before = os.stat(path).st_ino
        try:
            _scrub_fault_log(path)

            assert os.stat(path).st_ino == ino_before, (
                "scrub replaced the inode -- running instances' registered "
                "faulthandler fds are stranded on the unlinked old file"
            )
            with open(path) as f:
                content = f.read()
            assert expected_line in content, (
                f"scrub did not redact the traceback line: {content!r}"
            )
            assert raw_line not in content, "raw home path survived the scrub"

            # The running instance dumps AFTER the scrub: it must land in the
            # file a reader would open, not in an unlinked ghost.
            os.write(running_fd, b"LIVE DUMP MARKER\n")
            with open(path) as f:
                assert "LIVE DUMP MARKER" in f.read(), (
                    "a dump written through the pre-scrub fd is invisible in "
                    "the on-disk file -- the fd was stranded"
                )

            # Idempotence: a second boot's scrub (concurrent-boot path) must
            # be a byte-exact no-op -- not just "the redacted line survives".
            with open(path, "rb") as f:
                after_first = f.read()
            _scrub_fault_log(path)
            with open(path, "rb") as f:
                assert f.read() == after_first, "re-scrub was not a byte-exact no-op"

            # Undecodable bytes must not crash the scrub or corrupt the file:
            # with nothing left to redact, the file must stay byte-untouched
            # (the unchanged-skip path -- raw bytes preserved, no U+FFFD
            # rewrite).
            os.write(running_fd, b"garbage \xff\xfe bytes\n")
            with open(path, "rb") as f:
                with_garbage = f.read()
            _scrub_fault_log(path)
            with open(path, "rb") as f:
                assert f.read() == with_garbage, (
                    "scrub rewrote a file that had nothing to redact"
                )

            # No .scrub tmp may survive any of the runs above.
            leftovers = [n for n in os.listdir(d) if n.endswith(".scrub")]
            assert not leftovers, f"leaked scrub tmp files: {leftovers}"
        finally:
            os.close(running_fd)

    print("PASS: scrub preserves the inode, redacts, and keeps live fds visible")
    print("PASS: scenario_faulthandler_scrub_inplace")


if __name__ == "__main__":
    main()
