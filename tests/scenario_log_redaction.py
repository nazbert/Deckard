"""Scenario for src/backend/log_redaction.py.

A loguru core patcher scrubs every record, message and folded traceback,
before any sink formats it. install_exception_hooks() must install it.
"""
import fixtures  # must be first; isolates DATA_PATH before any src import

import getpass
import os
import threading

from loguru import logger

from src.backend import log_hooks
from src.backend.log_redaction import redact_record, scrub

HOME = os.path.expanduser("~")
USER = getpass.getuser()
UT = "<user>"  # the token scrub() substitutes for the username


def check_scrub_unit() -> None:
    # The home directory becomes a tilde, and the project-relative tail stays.
    assert scrub(f"{HOME}/dev/StreamController/src/app.py") == "~/dev/StreamController/src/app.py"
    assert scrub(f'File "{HOME}/.config/x.json", line 3') == 'File "~/.config/x.json", line 3'
    assert scrub(HOME) == "~", "bare home path (end of string) must redact"
    # Boundary guards. A longer username sharing the prefix must not be
    # clipped, and dot-suffix siblings must not collapse into the tilde form.
    assert scrub(HOME + "ette/f") == HOME + "ette/f", "prefix-sharing sibling user must survive"
    if os.path.basename(HOME) == USER:
        parent = os.path.dirname(HOME)
        assert scrub(HOME + ".old/f") == f"{parent}/{UT}.old/f", (
            "sibling dir of home must keep its suffix, hide the username"
        )
        assert scrub(f"logs in {HOME}.") == f"logs in {parent}/{UT}.", (
            "sentence-final home path must still hide the username"
        )

    # The username matches in path segments and in user@host, never bare.
    assert scrub(f"/run/media/{USER}/stick") == f"/run/media/{UT}/stick"
    assert scrub(f"/var/home/{USER}") == f"/var/home/{UT}"
    assert scrub(f"ssh {USER}@build-host: refused") == f"ssh {UT}@build-host: refused"
    prose = f"the {USER}xyz option"
    assert scrub(prose) == prose, "username as a word prefix must not be touched"

    # URL credentials collapse to a mask, and the host and path stay.
    assert scrub("https://alice:hunter2@example.com/a/b") == "https://***@example.com/a/b"
    assert scrub("https://alice@example.com/a") == "https://***@example.com/a"
    assert "example.com/a/b" in scrub("https://alice:hunter2@example.com/a/b?x=1")

    # Secret params in the equals form. Unambiguous names match anywhere and
    # tolerate spaces. A bare key equals matches only when query-anchored,
    # because key is deck vocabulary and key=3 in a debug message must survive.
    assert scrub("GET /repo?access_token=abc123&x=1") == "GET /repo?access_token=***&x=1"
    assert scrub("retry with token=tok-9") == "retry with token=***"
    assert scrub("retry with token = tok-9") == "retry with token=***", (
        "whitespace around '=' must not defeat redaction (round 1)"
    )
    assert scrub("https://h/p?key=sekrit&b=2") == "https://h/p?key=***&b=2"
    assert scrub("painting key=3 gen=7") == "painting key=3 gen=7"

    # Secret params in the colon form, from a dict repr or a JSON dump, such as
    # the HomeAssistant plugin logging its settings dict on error.
    assert scrub("{'access_token': 'eyJabc.def'}") == "{'access_token': '***'}"
    assert scrub('{"api_key": "sk-12345"}') == '{"api_key": "***"}'
    assert scrub("headers token: abc.def") == "headers token: ***"
    assert scrub("{'key': 3, 'gen': 7}") == "{'key': 3, 'gen': 7}", (
        "deck 'key' dict field must survive the colon rule"
    )

    # Authorization headers. A Basic b64 value decodes straight to user and
    # pass, and BEARER in any case must not slip the fast path.
    assert scrub("Authorization: Basic dXNlcjpwYXNz") == "Authorization: Basic ***"
    assert scrub('"Authorization": "Bearer eyJhbGciOi"') == '"Authorization": "Bearer ***"'
    assert scrub("Authorization: Bearer eyJhbGciOi.payload") == "Authorization: Bearer ***"
    assert scrub("Proxy-Authorization: Digest sometokenvalue") == "Proxy-Authorization: Digest ***"
    assert scrub("Authorization: rawtokenvalue") == "Authorization: ***", (
        "the schemeless header form must still redact its raw value"
    )
    assert scrub("auth hdr BEARER SECRETTOKEN123") == "auth hdr BEARER ***", (
        "fast-path bearer probe must be case-folded (round 1)"
    )
    prose_basic = "covers the basic setup steps"
    assert scrub(prose_basic) == prose_basic, (
        "'basic' is prose vocabulary -- only redact it in header context"
    )

    # The no-scheme branch must never consume a bare scheme word as the value.
    # A credential that merely starts with those letters is not a scheme word
    # and must still be redacted.
    assert scrub("Authorization: basicauthvalue123") == "Authorization: ***"
    assert scrub("Authorization: tokenvalue99") == "Authorization: ***"
    assert scrub("Authorization: bearertoken.abc") == "Authorization: ***"


def check_scrub_idempotent() -> None:
    """scrub(scrub(x)) must equal scrub(x) over the whole corpus.

    The auth-header rule can backtrack out of its optional scheme group and
    consume the scheme word itself on a second pass, so any pipeline that
    scrubs twice would mangle every header it had already redacted.
    """
    corpus = [
        # Auth headers, every scheme, both delimiters, quoted and bare.
        "Authorization: Basic dXNlcjpwYXNz",
        "Authorization: Bearer eyJhbGciOi.payload",
        "authorization: Token abc123def",
        "Proxy-Authorization: Digest sometokenvalue",
        '{"Authorization": "Bearer eyJhbGciOi.abc"}',
        "Authorization: rawtokenvalue",
        "Authorization: basicauthvalue123",
        "Authorization: Digest username=x, nonce=abcdef",
        "Authorization: Basic dXNlcjpwYXNz\nProxy-Authorization: Bearer secretvalue",
        "auth hdr BEARER SECRETTOKEN123",
        # The rest of the corpus, for a real property test rather than an
        # auth-header-only one.
        f"{HOME}/dev/Deckard/src/app.py",
        f'File "{HOME}/.config/x.json", line 3',
        HOME,
        f"/run/media/{USER}/stick",
        f"ssh {USER}@build-host: refused",
        "https://alice:hunter2@example.com/a/b?x=1",
        "https://alice@example.com/a",
        "GET /repo?access_token=abc123&x=1",
        "retry with token = tok-9",
        "https://h/p?key=sekrit&b=2",
        "{'access_token': 'eyJabc.def'}",
        '{"api_key": "sk-12345"}',
        "headers token: abc.def",
        # Must-not-touch vocabulary. Idempotent trivially, but a rule that
        # starts eating these would show up here too.
        "painting key=3 gen=7",
        "{'key': 3, 'gen': 7}",
        "covers the basic setup steps",
        "plain message, nothing sensitive",
        "",
    ]
    for text in corpus:
        once = scrub(text)
        assert scrub(once) == once, (
            f"scrub() is not idempotent for {text!r}: "
            f"pass 1 -> {once!r}, pass 2 -> {scrub(once)!r}"
        )
        # Already-redacted markers must survive a re-scrub verbatim, so the
        # count can only be what pass 1 produced and never grow.
        assert once.count("***") == scrub(once).count("***")

    # The fast path returns unchanged text untouched.
    assert scrub("plain message, nothing sensitive") == "plain message, nothing sensitive"
    assert scrub("") == ""


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_log_redaction")
    check_scrub_unit()
    check_scrub_idempotent()

    # The real boot wiring, and nothing else. main() only ever calls
    # install_exception_hooks(), and redaction must ride along. This does not
    # call install_log_redaction(), so reverting the piggyback inside
    # install_exception_hooks() turns this red.
    log_hooks.install_exception_hooks()
    assert logger._core.patcher is redact_record, (
        "install_exception_hooks() must install the redaction patcher -- "
        "main()'s boot path has no other install site"
    )
    log_hooks.install_exception_hooks()  # idempotent
    assert logger._core.patcher is redact_record

    # A real file sink plus a capture sink. Both must receive scrubbed text.
    # backtrace and diagnose are on here, because they are the loudest possible
    # exception expansion, so a patcher that failed to clear the record would
    # leak the most here.
    log_dir = os.path.join(fixtures.DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "logs.log")
    sink_id = logger.add(log_path, backtrace=True, diagnose=True, level="TRACE")
    records: list[str] = []
    logger.add(lambda m: records.append(str(m)), level="TRACE")

    # Plain messages through the normal path.
    logger.info(f"config at {HOME}/.config/streamcontroller/settings.json")
    logger.info(f"fetching https://{USER}:hunter2@git.example.com/repo.git?access_token=abc123&x=1")
    logger.info(f"mounted /run/media/{USER}/stick")
    logger.info("HA settings: {'host': 'ha.local', 'access_token': 'eyJlongtoken'}")

    # An uncaught thread exception through the real hook. The message, the
    # frame paths and a diagnose-visible local all carry PII.
    def boom() -> None:
        key_path = f"{HOME}/.ssh/id_rsa"  # a local that diagnose=True would leak
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
        # The raw values must be gone, traceback frame paths included, which is
        # why the exception is folded into the message.
        assert HOME not in output, f"{label}: raw home path leaked"
        assert "hunter2" not in output, f"{label}: URL password leaked"
        assert "sekrit" not in output, f"{label}: URL password (exception message) leaked"
        assert "access_token=abc123" not in output, f"{label}: token param leaked"
        assert "token=tok123" not in output, f"{label}: token param (exception message) leaked"
        assert "eyJlongtoken" not in output, f"{label}: dict-repr access_token leaked"
        assert f"/run/media/{USER}/" not in output, f"{label}: username path segment leaked"
        assert f"{USER}:hunter2" not in output and f"{USER}:sekrit" not in output, (
            f"{label}: URL userinfo leaked"
        )
        assert f"//{USER}@" not in output and f" {USER}@" not in output, (
            f"{label}: bare user@host leaked"
        )

        # The redacted forms are present.
        assert "~/.config/streamcontroller/settings.json" in output, f"{label}: home must map to ~"
        assert "https://***@git.example.com/repo.git?access_token=***&x=1" in output, label
        assert "/run/media/<user>/stick" in output, label
        assert "https://***@host.example/x?token=***" in output, label
        assert "'access_token': '***'" in output, f"{label}: dict-repr token must redact"
        assert "'host': 'ha.local'" in output, f"{label}: non-secret dict fields must survive"

        # Debuggability floor. The traceback is still a traceback.
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
