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

Log redaction. This scrubs PII from every log record before a sink sees it,
so a user shares logs.log without leaking a username, a home directory
layout, or a credential inside a url, a query parameter, an Authorization
header, or a settings or header dict.

This installs a core-level loguru patcher rather than a per-sink wrapper.
Logger._log applies core.patcher to the record before it fans out to
handler.emit (loguru 0.7 _logger.py), so one install covers every sink that
config_logger() adds, which are logs/logs.log, stderr, the gl.logs ring
behind the About dialog and the enqueued plugins.log sink, and it keeps
covering them across a log.remove() and log.add() cycle. A wrapper around the
file sink instead loses loguru's path-sink rotation, because a callable sink
does not rotate, and every sink needs its own wrapper.

A patcher cannot scrub {exception}. Each sink formats the raw (type, value,
tb) tuple itself at emit time, and a traceback frame path is the main leak,
because the central exception hooks route a full traceback into logs.log. So
for a record that carries an exception, redact_record() formats the traceback
itself with the stdlib, including a chained one, scrubs it, folds it into the
message, and clears record["exception"], so no sink formats the raw frames.
That has one side effect. The diagnose=True local-variable dumps of loguru
stop reaching the sinks, and a variable value is the worst PII in a shareable
log.

log_hooks.install_exception_hooks() calls install_log_redaction(). The hooks
route a full traceback into the sinks, so they must never fire without the
scrubbing layer. main()'s boot path gets redaction through that call, and
scenario_log_redaction asserts the pairing, because it installs the hooks
alone, so a removal of the call fails the harness.

What this redacts and what it keeps. scrub() is pure and stdlib-only, so a
unit test runs it without loguru, and every pattern compiles once at import.

The home directory, in its expanduser, realpath and $HOME spellings, becomes
"~". A guard on both sides keeps /home/nazareth, /var/home/naz and
/home/naz.old whole. A path stays readable, so /home/x/dev/App/src/y.py
becomes ~/dev/App/src/y.py.

The username, as a path segment such as /run/media/<user>/.., which includes
a dot-suffix form such as /home/<user>.old, and as the user part of
user@host. Never as a bare word, so a common-word username leaves ordinary
prose whole.

A url credential. scheme://user:pass@host and scheme://user@host become
scheme://***@host, and the host and the path stay, so a store-fetch url stays
readable.

A secret assignment, and a dict, JSON or YAML field, for an unambiguous key
vocabulary of token, access_token, api_key, password, secret and the like.
token=v, token = v, token: v, 'token': 'v' and "token": "v" all lose the
value. An ambiguous name, which is key=, sig= or auth=, redacts in url-query
position alone, anchored to a ? or an &. "key" is deck vocabulary here, so
key=3 and {'key': 3} stay whole.

An Authorization header. That covers Authorization: Basic <b64>, where Basic
decodes straight to user:pass, Bearer <token> in any case, a quoted JSON
header dump, and a raw Authorization: <value> form.

Like log_hooks, this module imports stdlib and loguru only, and it imports
loguru inside install_log_redaction() alone. It imports nothing from src/ or
globals.py, so it stays importable before globals, which the fixtures.py
contract needs, and importable by log_hooks without weakening the import
contract of log_hooks.
"""
import getpass
import os
import re
import traceback

_installed = False

_USER_TOKEN = "<user>"

# The characters that follow a complete path in log text, which are a slash,
# whitespace, a quote, and the punctuation that ends a path in prose or in a
# repr. A "." stays out, so "/home/naz.old" does not half-match as home.
_AFTER_PATH = r"[]\s/\"'`:;,()[{}<>|=&]"
# A username path segment may also carry a "." after it. A suffix form such
# as "/home/<user>.old" keeps the suffix and hides the name.
_AFTER_SEGMENT = r"[].\s/\"'`:;,()[{}<>|=&]"

# The key names that name a secret wherever they appear. key, sig and auth
# stay out, because they are deck and debug vocabulary, and the url-query
# rule covers them. The header rule owns authorization, so a scheme word such
# as "Basic" survives rather than reads as part of a value.
_SECRET_KEYS = (
    r"(?:access|refresh|id|auth)[_-]?token|token|"
    r"api[_-]?key|apikey|client[_-]?secret|secret|"
    r"password|passwd|pwd|signature"
)


def _home_candidates() -> list[str]:
    """Every spelling of the home directory that a path can carry. That is
    expanduser, $HOME, and the realpath form of each, such as a /home
    symlinked to /var/home on an ostree system. Longest first, so a nested
    variant wins."""
    homes: list[str] = []
    for candidate in (os.path.expanduser("~"), os.environ.get("HOME")):
        if not candidate:
            continue
        candidate = candidate.rstrip("/")
        # A "/" or an "" turns every absolute path into "~...". Refuse both.
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
    """Rebuild 'name': 'value' as 'name': '***', and keep the quoting the
    original used, from a dict repr, JSON, YAML or none."""
    value_quote = match.group(4) or ""
    return (
        f"{match.group(1)}{match.group(2)}{match.group(1)}"
        f"{match.group(3)}{value_quote}***{value_quote}"
    )


def _compile_rules() -> list[tuple]:
    rules: list[tuple] = []

    # Url userinfo, which is scheme://user:pass@host and scheme://user@host.
    # The bounded quantifiers keep a wall of text without an "@" cheap.
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}:[^/\s@]{0,256}@"), "***@"))
    rules.append((re.compile(r"(?<=://)[^/\s:@]{1,128}@"), "***@"))

    # Authorization headers, quoted or bare, with a scheme word or without.
    #   Authorization: Basic dXNlcjpwYXNz   -> Authorization: Basic ***
    #   "Authorization": "Bearer eyJ..."    -> "Authorization": "Bearer ***"
    #   Proxy-Authorization: rawtokenvalue  -> Proxy-Authorization: ***
    # These run before the generic rules, so the scheme word survives.
    # "Basic" alone is common prose, and only this header context matches it.
    #
    # Two rules, and not one rule with an optional scheme group. The raw-value
    # form needs the scheme optional, and an optional group backtracks. As one
    # pattern, a second scrub of "Authorization: Basic ***" fails to match the
    # value class against "***", backtracks past the scheme group, and takes
    # the word "Basic" as the value, which gives "Authorization: *** ***".
    # scrub() then is not idempotent, and a pipeline that scrubs twice, such
    # as the boot scrub over an already scrubbed file, or a line that passes
    # the loguru patcher and a later scrub, mangles its headers. Split in two,
    # the no-scheme rule carries a guard that the with-scheme rule must not
    # have.
    _AUTH_HEADER = r"(?i)\b((?:proxy-)?authorization[\"']?[ \t]*[:=][ \t]*[\"']?"
    _AUTH_SCHEME = r"(?:basic|bearer|digest|token)"
    _AUTH_VALUE = r"[a-z0-9._~+/=-]{4,}"

    # With a scheme word the scheme stays and the credential after it goes.
    rules.append((
        re.compile(_AUTH_HEADER + _AUTH_SCHEME + r"[ \t]+)" + _AUTH_VALUE),
        r"\1***",
    ))
    # Without a scheme word the value must not be a bare scheme word. Bare
    # means that no further value character follows, so a real credential that
    # starts with those letters, such as "tokenvalue" or "basicauth123", still
    # redacts.
    rules.append((
        re.compile(
            _AUTH_HEADER + r")"
            + r"(?!" + _AUTH_SCHEME + r"(?![a-z0-9._~+/=-]))"
            + _AUTH_VALUE
        ),
        r"\1***",
    ))

    # A bearer token outside an Authorization header. "bearer" is no prose
    # vocabulary, and "basic" is.
    rules.append((re.compile(r"(?i)\b(bearer[ \t]+)[a-z0-9._~+/=-]{4,}"), r"\1***"))

    # secret=value. Whitespace may surround the "=", and the value may carry
    # quotes.
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])(" + _SECRET_KEYS + r"|authorization)"
            r"[ \t]*=[ \t]*"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^&\s\"'<>)\]]+)"
        ),
        r"\1=***",
    ))

    # An ambiguous name, in url-query position only.
    rules.append((
        re.compile(r"(?i)([?&](?:key|sig|auth))=[^&\s\"'<>)\]]+"),
        r"\1=***",
    ))

    # secret: value, in a dict repr, a JSON dump or a YAML config dump, such
    # as {'access_token': 'eyJ...'}, {"api_key": "sk-..."} or token: abc, which
    # the HomeAssistant settings and headers dump produces. The scheme-word
    # lookahead in the value branch keeps an already scrubbed
    # "token: Bearer ***" out of "token: *** ***".
    rules.append((
        re.compile(
            r"(?i)(?<![\w-])(['\"]?)(" + _SECRET_KEYS + r")\1"
            r"([ \t]*:[ \t]*)"
            r"(?:(['\"])[^'\"\r\n]*\4|(?!(?:basic|bearer|digest)\b)[^&\s,'\"()\[\]{}<>]+)"
        ),
        _colon_replacement,
    ))

    # The home directory becomes "~", with a guard on both sides. The
    # lookbehind stops a mid-path match, so "/var/home/naz" and
    # "/mnt/backup/home/naz" fall through to the username-segment rule. The
    # lookahead needs a real path terminator, so "/home/nazareth" and
    # "/home/naz.old" never clip to "~...", and the segment rule below hides
    # their username.
    for home in _home_candidates():
        rules.append((
            re.compile(
                r"(?<![\w.-])" + re.escape(home) + r"(?=" + _AFTER_PATH + r"|$)"
            ),
            "~",
        ))

    # The username, as a full path segment, which includes a dot-suffix form,
    # or as the user part of user@host.
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
    """Return text with home paths, usernames and credentials redacted. Pure,
    thread-safe, and free of loguru."""
    if not text:
        return text
    # A fast path. Every rule needs one of these characters, except the bare
    # bearer form, which carries none of them, so the case-folded check for
    # it runs after every cheap character probe misses.
    if (
        "/" not in text and "@" not in text and "=" not in text
        and ":" not in text and "bearer" not in text.lower()
    ):
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_record(record) -> None:
    """The loguru patcher. It scrubs the message. When an exception rides
    along, from opt(exception=...), from @log.catch or from the central
    exception hooks, it replaces that exception with a scrubbed traceback
    formatted by the stdlib and folded into the message. It clears
    record["exception"] first, so no sink formats the raw frames even when the
    traceback formatting fails. It must never raise, because a patcher
    exception reaches every logging call site."""
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
        # Log the record unredacted rather than lose it or crash the caller.
        # scrub() on a str does not raise, and this guards an unusual record
        # shape.
        pass


def install_log_redaction() -> None:
    """Install redact_record as loguru's core patcher. Idempotent. It calls
    logger.configure(patcher=...), which replaces an earlier core patcher.
    Nothing else in this codebase sets one. If something sets one later,
    compose the two there rather than stack installs here.

    log_hooks.install_exception_hooks() calls this, which is how main()'s boot
    path gets redaction. A direct call stays safe and idempotent."""
    global _installed
    if _installed:
        return
    from loguru import logger
    logger.configure(patcher=redact_record)
    _installed = True
