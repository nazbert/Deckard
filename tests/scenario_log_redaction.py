"""
Scenario for issue #105 (upstream #439): src/backend/log_redaction.py.

The regression: the issue-#80 central exception hooks route full tracebacks
into logs.log, so frame paths (/home/<user>/...), usernames and credentialed
URLs reach disk unredacted. The fix is a core-level loguru patcher
(install_log_redaction()) that scrubs every record -- message AND folded
traceback -- before any sink formats it.

Covers:

  1. scrub() unit behavior (pure, no loguru): home -> ~ with boundary
     guards, username only in path segments / user@host (never bare-word),
     URL credentials -> ***@host, secret params -> =***, ambiguous names
     (key=) only when query-anchored, Bearer tokens;
  2. the real pipeline: install_log_redaction() + install_exception_hooks(),
     a raising Thread whose message and frames carry $HOME, the username and
     a credentialed URL -- asserted against a REAL file sink configured like
     config_logger()'s logs.log (backtrace=True, diagnose=True) so the
     {exception} path is exercised, not simulated;
  3. debuggability floor: the traceback survives redaction identifiable --
     `File "~/...scenario_log_redaction.py"` frame, source line, exception
     type/message -- and no diagnose variable dump leaks values;
  4. idempotence of install_log_redaction().

Run in isolation (run_all.py gives each scenario its own interpreter):
logger.configure(patcher=...) and the exception hooks are process-global.
"""
import fixtures  # must be first: isolates DATA_PATH before any src import

import getpass
import os
import threading

from loguru import logger

from src.backend import log_hooks
from src.backend.log_redaction import install_log_redaction, scrub

HOME = os.path.expanduser("~")
USER = getpass.getuser()
UT = "<user>"  # the token scrub() substitutes for the username


def check_scrub_unit() -> None:
    # Home directory -> ~, project-relative tail preserved.
    assert scrub(f"{HOME}/dev/StreamController/src/app.py") == "~/dev/StreamController/src/app.py"
    assert scrub(f'File "{HOME}/.config/x.json", line 3') == 'File "~/.config/x.json", line 3'
    assert scrub(HOME) == "~", "bare home path (end of string) must redact"
    # Boundary guard: a LONGER username sharing the prefix must not be clipped.
    assert scrub(HOME + "ette/f") == HOME + "ette/f", "prefix-sharing sibling user must survive"

    # Username: path segments and user@host only -- never bare words.
    assert scrub(f"/run/media/{USER}/stick") == f"/run/media/{UT}/stick"
    assert scrub(f"/var/home/{USER}") == f"/var/home/{UT}"
    assert scrub(f"ssh {USER}@build-host: refused") == f"ssh {UT}@build-host: refused"
    prose = f"the {USER}xyz option"
    assert scrub(prose) == prose, "username as a word prefix must not be touched"

    # URL credentials: user[:pass]@host -> ***@host, host+path preserved.
    assert scrub("https://alice:hunter2@example.com/a/b") == "https://***@example.com/a/b"
    assert scrub("https://alice@example.com/a") == "https://***@example.com/a"
    assert "example.com/a/b" in scrub("https://alice:hunter2@example.com/a/b?x=1")

    # Secret params: unambiguous names anywhere; `key=` only query-anchored
    # ("key" is deck vocabulary -- key=3 in a debug message must survive).
    assert scrub("GET /repo?access_token=abc123&x=1") == "GET /repo?access_token=***&x=1"
    assert scrub("retry with token=tok-9") == "retry with token=***"
    assert scrub("https://h/p?key=sekrit&b=2") == "https://h/p?key=***&b=2"
    assert scrub("painting key=3 gen=7") == "painting key=3 gen=7"
    assert scrub("Authorization: Bearer eyJhbGciOi.payload") == "Authorization: Bearer ***"

    # Fast path returns unchanged text untouched.
    assert scrub("plain message, nothing sensitive") == "plain message, nothing sensitive"
    assert scrub("") == ""


def main() -> None:
    check_scrub_unit()

    install_log_redaction()
    install_log_redaction()  # idempotent: second call must be a no-op

    # File sink configured exactly like config_logger()'s logs.log handler
    # (minus rotation), PLUS a capture sink -- both must receive scrubbed text.
    log_dir = os.path.join(fixtures.DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "logs.log")
    sink_id = logger.add(log_path, backtrace=True, diagnose=True, level="TRACE")
    records: list[str] = []
    logger.add(lambda m: records.append(str(m)), level="TRACE")

    log_hooks.install_exception_hooks()

    # Plain messages through the normal path.
    logger.info(f"config at {HOME}/.config/streamcontroller/settings.json")
    logger.info(f"fetching https://{USER}:hunter2@git.example.com/repo.git?access_token=abc123&x=1")
    logger.info(f"mounted /run/media/{USER}/stick")

    # An uncaught thread exception through the REAL issue-#80 hook: message,
    # frame paths and a diagnose-visible local all carry PII.
    def boom() -> None:
        key_path = f"{HOME}/.ssh/id_rsa"  # local: would leak via diagnose=True
        raise ValueError(
            f"cannot open {key_path} "
            f"(remote=https://{USER}:sekrit@host.example/x?token=tok123)"
        )

    t = threading.Thread(target=boom, name="redaction-worker")
    t.start()
    t.join()

    logger.remove(sink_id)  # flush/close the file sink before reading
    with open(log_path) as f:
        content = f.read()
    joined = "".join(records)

    for output, label in ((content, "logs.log"), (joined, "capture sink")):
        # Raw values must be gone -- including traceback frame paths, which
        # is the whole point of folding {exception} into the message.
        assert HOME not in output, f"{label}: raw home path leaked"
        assert "hunter2" not in output, f"{label}: URL password leaked"
        assert "sekrit" not in output, f"{label}: URL password (exception message) leaked"
        assert "access_token=abc123" not in output, f"{label}: token param leaked"
        assert "token=tok123" not in output, f"{label}: token param (exception message) leaked"
        assert f"/run/media/{USER}/" not in output, f"{label}: username path segment leaked"
        assert f"{USER}:hunter2" not in output and f"{USER}:sekrit" not in output, (
            f"{label}: URL userinfo leaked"
        )
        assert f"//{USER}@" not in output and f" {USER}@" not in output, (
            f"{label}: bare user@host leaked"
        )

        # Redacted forms present.
        assert "~/.config/streamcontroller/settings.json" in output, f"{label}: home must map to ~"
        assert "https://***@git.example.com/repo.git?access_token=***&x=1" in output, label
        assert "/run/media/<user>/stick" in output, label
        assert "https://***@host.example/x?token=***" in output, label

        # Debuggability floor: the traceback is still a traceback.
        assert "Traceback (most recent call last):" in output, f"{label}: traceback text missing"
        assert 'File "~/' in output, (
            f"{label}: frame paths must stay identifiable as ~-relative, not vanish"
        )
        assert "scenario_log_redaction.py" in output, f"{label}: frame file name must survive"
        assert "raise ValueError(" in output, f"{label}: source line must survive"
        assert "cannot open ~/.ssh/id_rsa" in output, f"{label}: message must stay readable"
        assert "Uncaught exception [thread]" in output and "redaction-worker" in output, (
            f"{label}: the hooks' kind/thread-name context must survive redaction"
        )

    print("PASS: scenario_log_redaction")


if __name__ == "__main__":
    main()
