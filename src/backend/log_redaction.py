"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

---

Log redaction (issue #105, upstream #439): scrub PII from every log record
before any sink sees it, so users can share logs.log without leaking their
username, home directory layout, or credentials embedded in URLs.

Why a core-level loguru *patcher* and not a per-sink wrapper: Logger._log
applies `core.patcher` to the record BEFORE fanning out to handler.emit
(loguru 0.7 _logger.py), so one install covers every sink config_logger()
adds -- logs/logs.log, stderr, the gl.logs ring behind the About dialog, and
the enqueue'd plugins.log sink -- and keeps covering them across
log.remove()/log.add() cycles. Wrapping the file sink instead would forfeit
loguru's path-sink rotation (callable sinks don't rotate) and would have to
be repeated per sink.

The traceback subtlety: a patcher cannot scrub `{exception}` -- sinks format
the raw (type, value, tb) tuple themselves at emit time, and traceback frame
paths are the main leak (issue #80's central exception hooks route full
tracebacks into logs.log). So for records carrying an exception,
redact_record() formats the traceback itself (stdlib, chained), scrubs it,
folds it into the message, and clears record["exception"] so no sink can
ever format the raw frames. Deliberate side effect: loguru's diagnose=True
local-variable dumps stop reaching the sinks -- variable values are the
worst PII vector in a shareable log.

What is redacted vs preserved (scrub() is pure + stdlib-only, unit-testable
without loguru; all patterns compiled once at import):

  * the home directory (expanduser/realpath/$HOME variants, boundary-guarded
    so `/home/nazareth` is not clipped by user `naz`) -> `~`. Paths stay
    readable: `/home/x/dev/App/src/y.py` -> `~/dev/App/src/y.py`.
  * the username, ONLY as a path segment (`/run/media/<user>/..`) or in
    `user@host` -- never as a bare word, so common-word usernames don't
    shred ordinary prose.
  * URL credentials: `scheme://user[:pass]@host` -> `scheme://***@host`;
    hosts and paths are preserved (store-fetch URLs stay debuggable).
  * unambiguous secret params (`token=`, `access_token=`, `api_key=`,
    `password=`, ...) anywhere; ambiguous names (`key=`, `sig=`, `auth=`)
    only in URL-query position (`?`/`&`-anchored) because "key" is deck
    vocabulary in this codebase; `Bearer <token>` -> `Bearer ***`.

Import discipline: like log_hooks, stdlib + loguru only (loguru itself only
inside install_log_redaction()), nothing from src/ or globals.py -- must be
importable before `globals` (fixtures.py contract).
"""
import getpass
import os
import re
import traceback

_installed = False

_USER_TOKEN = "<user>"


def _home_candidates() -> list[str]:
    """Every spelling of the home directory that can show up in a path:
    expanduser, $HOME, and their realpath forms (e.g. /home symlinked to
    /var/home on ostree systems). Longest first so a nested variant wins."""
    homes: list[str] = []
    for candidate in (os.path.expanduser("~"), os.environ.get("HOME")):
        if not candidate:
            continue
        candidate = candidate.rstrip("/")
        # "/" or "" would turn every absolute path into "~..." -- refuse.
        if len(candidate) < 2:
            continue
        for variant in (candidate, os.path.realpath(candidate)):
            if len(variant) >= 2 and variant not in homes:
                homes.append(variant)
    return sorted(homes, key=len, reverse=True)


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def _compile_rules() -> list[tuple[re.Pattern, str]]:
    rules: list[tuple[re.Pattern, str]] = []

    # URL userinfo: scheme://user:pass@host and scheme://user@host.
    # Bounded quantifiers keep a pathological "@"-free wall of text cheap.
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}:[^/\s@]{0,256}@"), "***@"))
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}@"), "***@"))

    # Secret-bearing params. Unambiguous names anywhere (message text, query
    # strings, header dumps); ambiguous ones only when ?/&-anchored.
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])("
            r"token|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
            r"api[_-]?key|apikey|client[_-]?secret|secret|password|passwd|pwd|"
            r"authorization|signature"
            r")=[^&\s\"'<>)\]]+"
        ),
        r"\1=***",
    ))
    rules.append((
        re.compile(r"(?i)([?&](?:key|sig|auth))=[^&\s\"'<>)\]]+"),
        r"\1=***",
    ))
    rules.append((re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{4,}"), r"\1***"))

    # Home directory -> "~". The lookahead stops "/home/naz" from clipping
    # "/home/nazareth"; the lookbehind stops it from matching mid-path
    # (inside "/var/home/naz" or "/mnt/backup/home/naz" -- those fall
    # through to the username-segment rule below instead of being mangled).
    for home in _home_candidates():
        rules.append((
            re.compile(r"(?<![\w.-])" + re.escape(home) + r"(?![\w-])"),
            "~",
        ))

    # Username: only as a full path segment or the user part of user@host.
    user = _username()
    if user:
        escaped = re.escape(user)
        rules.append((
            re.compile(r"(?<=/)" + escaped + r"(?=[/\s\"'`:;,)\]}>]|$)"),
            _USER_TOKEN,
        ))
        rules.append((
            re.compile(r"(?<![\w.-])" + escaped + r"(?=@[\w[])"),
            _USER_TOKEN,
        ))

    return rules


_RULES = _compile_rules()


def scrub(text: str) -> str:
    """Return `text` with home paths, usernames and credentials redacted.
    Pure, thread-safe, no loguru dependency."""
    if not text:
        return text
    # Fast path: every rule needs one of these characters ("earer" covers
    # bare "Bearer xyz" header dumps, which may contain none of the others).
    if (
        "/" not in text and "@" not in text and "=" not in text
        and "earer" not in text
    ):
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_record(record) -> None:
    """loguru patcher: scrub the message and, if an exception is attached
    (opt(exception=...), @log.catch, the issue-#80 hooks), replace it with a
    scrubbed stdlib-formatted traceback folded into the message. Clearing
    record["exception"] FIRST guarantees no sink formats the raw frames even
    if traceback formatting itself fails. Must never raise: a patcher
    exception would propagate to every logging call site."""
    try:
        record["message"] = scrub(record["message"])
        exc = record.get("exception")
        if exc is not None:
            record["exception"] = None
            try:
                text = "".join(
                    traceback.format_exception(exc.type, exc.value, exc.traceback)
                )
            except Exception:
                name = getattr(exc.type, "__name__", None) or repr(exc.type)
                text = f"<traceback unavailable: formatting failed for {name}>"
            record["message"] = (
                record["message"].rstrip("\n") + "\n" + scrub(text).rstrip("\n")
            )
    except Exception:
        # Fail open (log unredacted) rather than lose the record or crash
        # the caller; scrub() on a str cannot realistically raise, this
        # guards exotic record shapes.
        pass


def install_log_redaction() -> None:
    """Install redact_record as loguru's core patcher. Idempotent. Uses
    logger.configure(patcher=...), which REPLACES any earlier core patcher --
    nothing else in this codebase sets one (checked at introduction); if that
    changes, compose there, don't stack installs here."""
    global _installed
    if _installed:
        return
    from loguru import logger
    logger.configure(patcher=redact_record)
    _installed = True
