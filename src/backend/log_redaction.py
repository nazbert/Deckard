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
username, home directory layout, or credentials embedded in URLs, query
params, Authorization headers or settings/header dicts.

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

Wiring: log_hooks.install_exception_hooks() calls install_log_redaction() --
the hooks are exactly what route full tracebacks into the sinks, so they
must never fire without the scrubbing layer in place. main()'s boot path
gets redaction through that piggyback, and scenario_log_redaction asserts
the coupling (it installs ONLY the hooks), so removing the wiring fails the
harness.

What is redacted vs preserved (scrub() is pure + stdlib-only, unit-testable
without loguru; all patterns compiled once at import):

  * the home directory (expanduser/realpath/$HOME variants, boundary-guarded
    on BOTH sides so `/home/nazareth`, `/var/home/naz` and `/home/naz.old`
    are never clipped or mangled) -> `~`. Paths stay readable:
    `/home/x/dev/App/src/y.py` -> `~/dev/App/src/y.py`.
  * the username, ONLY as a path segment (`/run/media/<user>/..`, including
    dot-suffix forms like `/home/<user>.old`) or in `user@host` -- never as
    a bare word, so common-word usernames don't shred ordinary prose.
  * URL credentials: `scheme://user[:pass]@host` -> `scheme://***@host`;
    hosts and paths are preserved (store-fetch URLs stay debuggable).
  * secret assignments AND dict/JSON/YAML fields for an unambiguous key
    vocabulary (`token`, `access_token`, `api_key`, `password`, `secret`,
    ...): `token=v`, `token = v`, `token: v`, `'token': 'v'`,
    `"token": "v"` all redact the value. Ambiguous names (`key=`, `sig=`,
    `auth=`) only in URL-query position (`?`/`&`-anchored) -- "key" is deck
    vocabulary here, so `key=3` and `{'key': 3}` survive untouched.
  * Authorization headers: `Authorization: Basic <b64>` (Basic decodes
    straight to user:pass), `Bearer <token>` in any case, quoted JSON
    header dumps, and raw `Authorization: <value>` forms.

Import discipline: like log_hooks, stdlib + loguru only (loguru itself only
inside install_log_redaction()), nothing from src/ or globals.py -- must be
importable before `globals` (fixtures.py contract), and importable BY
log_hooks without weakening log_hooks' own import contract.
"""
import getpass
import os
import re
import traceback

_installed = False

_USER_TOKEN = "<user>"

# Characters that legitimately follow a complete path in log text (slash,
# whitespace, quotes, and the punctuation that ends paths in prose/reprs).
# "." is deliberately absent: "/home/naz.old" must not half-match as home.
_AFTER_PATH = r"[]\s/\"'`:;,()[{}<>|=&]"
# A username path segment may additionally be followed by "." -- suffix
# forms like "/home/<user>.old" keep the suffix visible but hide the name.
_AFTER_SEGMENT = r"[].\s/\"'`:;,()[{}<>|=&]"

# Key names that are unambiguously secrets wherever they appear. `key`,
# `sig` and `auth` are NOT here (deck/debug vocabulary; URL-query rule
# only). `authorization` is owned by the header rule so scheme words
# ("Basic ...") aren't half-eaten as values.
_SECRET_KEYS = (
    r"(?:access|refresh|id|auth)[_-]?token|token|"
    r"api[_-]?key|apikey|client[_-]?secret|secret|"
    r"password|passwd|pwd|signature"
)


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


def _colon_replacement(match: re.Match) -> str:
    """Rebuild `'name': 'value'` as `'name': '***'`, preserving whatever
    quoting the original used (dict repr, JSON, YAML-ish, none)."""
    value_quote = match.group(4) or ""
    return (
        f"{match.group(1)}{match.group(2)}{match.group(1)}"
        f"{match.group(3)}{value_quote}***{value_quote}"
    )


def _compile_rules() -> list[tuple]:
    rules: list[tuple] = []

    # URL userinfo: scheme://user:pass@host and scheme://user@host.
    # Bounded quantifiers keep a pathological "@"-free wall of text cheap.
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}:[^/\s@]{0,256}@"), "***@"))
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}@"), "***@"))

    # Authorization headers, quoted or not, with or without a scheme word:
    #   Authorization: Basic dXNlcjpwYXNz   -> Authorization: Basic ***
    #   "Authorization": "Bearer eyJ..."    -> "Authorization": "Bearer ***"
    #   Proxy-Authorization: rawtokenvalue  -> Proxy-Authorization: ***
    # Must run BEFORE the generic rules so the scheme word survives intact
    # ("Basic" alone is common prose -- it is only matched in this header
    # context, never standalone).
    rules.append((
        re.compile(
            r"(?i)\b((?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
            r"(?:(?:basic|bearer|digest|token)[ \t]+)?)"
            r"[a-z0-9._~+/=-]{4,}"
        ),
        r"\1***",
    ))

    # Standalone bearer tokens outside an Authorization header ("bearer"
    # is not prose vocabulary, unlike "basic").
    rules.append((re.compile(r"(?i)\b(bearer[ \t]+)[a-z0-9._~+/=-]{4,}"), r"\1***"))

    # secret=value: whitespace tolerated around "=", quoted or bare values.
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])(" + _SECRET_KEYS + r"|authorization)"
            r"[ \t]*=[ \t]*"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^&\s\"'<>)\]]+)"
        ),
        r"\1=***",
    ))

    # Ambiguous names, URL-query position only.
    rules.append((
        re.compile(r"(?i)([?&](?:key|sig|auth))=[^&\s\"'<>)\]]+"),
        r"\1=***",
    ))

    # secret: value -- dict reprs, JSON dumps, YAML-ish config dumps:
    #   {'access_token': 'eyJ...'} / {"api_key": "sk-..."} / token: abc
    # (the HomeAssistant settings/headers-dump scenario). The value branch's
    # scheme-word lookahead keeps already-scrubbed "token: Bearer ***" from
    # collapsing into "token: *** ***".
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])(['\"]?)(" + _SECRET_KEYS + r")\1"
            r"([ \t]*:[ \t]*)"
            r"(?:(['\"])[^'\"\r\n]*\4|(?!(?:basic|bearer|digest)\b)[^&\s,'\"()\[\]{}<>]+)"
        ),
        _colon_replacement,
    ))

    # Home directory -> "~". Boundary-guarded on BOTH sides: the lookbehind
    # stops mid-path matches ("/var/home/naz", "/mnt/backup/home/naz" fall
    # through to the username-segment rule instead of being mangled); the
    # lookahead requires a real path terminator, so "/home/nazareth" and
    # "/home/naz.old" are never clipped to "~..." (the segment rule below
    # hides their username instead).
    for home in _home_candidates():
        rules.append((
            re.compile(
                r"(?<![\w.-])" + re.escape(home) + r"(?=" + _AFTER_PATH + r"|$)"
            ),
            "~",
        ))

    # Username: only as a full path segment (dot-suffix forms included) or
    # the user part of user@host.
    user = _username()
    if user:
        escaped = re.escape(user)
        rules.append((
            re.compile(r"(?<=/)" + escaped + r"(?=" + _AFTER_SEGMENT + r"|$)"),
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
    # Fast path: every rule needs one of these characters, except the bare
    # bearer form ("auth hdr BEARER XYZ" has none of them) -- checked
    # case-folded, and only after the cheap char probes all miss.
    if (
        "/" not in text and "@" not in text and "=" not in text
        and ":" not in text and "bearer" not in text.lower()
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
    changes, compose there, don't stack installs here.

    Called by log_hooks.install_exception_hooks() -- that is how main()'s
    boot path gets redaction; direct calls stay safe and idempotent."""
    global _installed
    if _installed:
        return
    from loguru import logger
    logger.configure(patcher=redact_record)
    _installed = True
