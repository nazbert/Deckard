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

Central exception hooks (issue #80, deep-audit-2026-07-10 RD-01).

Only @log.catch-decorated functions feed exceptions into loguru; an uncaught
exception on any other path reaches stderr only and is lost the moment the
app runs detached (autostart/flatpak) -- the "tracebacks bypass loguru" hole.
install_exception_hooks() closes four surfaces in one place:

  * main thread AND every GLib/Gio callback (idle_add, timeout_add, signal
    handlers, Gio actions): PyGObject routes their uncaught exceptions
    through PyErr_Print, which calls sys.excepthook;
  * plain threading.Thread targets, via threading.excepthook;
  * __del__/weakref-finalizer/GC-time errors, via sys.unraisablehook;
  * the plugin-dispatch asyncio loop, via asyncio_exception_handler (wired
    in event_dispatch._get_loop).

All of them route through loguru, which fans out to every sink
config_logger() has installed (logs/logs.log, stderr, the gl.logs ring
behind the About dialog). Before config_logger() runs, loguru's default
stderr sink catches them; no re-install is needed afterwards because the
hooks resolve the logger's sinks at call time.

Note the pool blind spot: exceptions inside ThreadPoolExecutor tasks are
stored on their Future and NEVER reach threading.excepthook -- submit sites
must attach a done-callback (the main_loop.run_in_background /
DeckController._log_callback_exception convention).

Pressure valve (issue #91): _log_exc() rate-limits per failing SITE -- one
record per (exception type, innermost frame) per RATE_LIMIT_WINDOW_S, with
the suppressed count reported on that site's next record. A raising GTK
signal handler on a hot path would otherwise put a full diagnose=True
traceback into every sink on every emission.

Known bound of that key: two DIFFERENT failures raised from the same line
with the same exception type -- one `raise ValueError(...)` reached by
several callers, with different messages -- share one budget and can mask
each other for a window. Folding the message into the key was considered and
rejected: real messages carry varying ids, paths and counters, so a
message-keyed guard degenerates into no throttling at all, which is the
exact failure mode this exists to prevent. The suppressed-count line is
therefore worded "N further failures at <site>", never "N repeats": the
guard knows the site, and does not know that the failures were identical.

Kill switch (issue #92): SC_NO_ERROR_HOOKS=1 makes install_exception_hooks()
and redirect_faulthandler() no-ops, so a field anomaly suspected to involve
the hooks (double logging, exit-path interaction, a hook firing where it
shouldn't) can be A/B'd against pre-#80 behavior with an env var instead of
a revert + rebuild. See install_exception_hooks() for the one thing the flag
deliberately does NOT disable.

Import discipline: this module must stay importable before `globals` (the
test harness's fixtures.py contract) -- stdlib + loguru only, nothing from
src/ or globals.py. log_redaction is the one allowed sibling import: it
follows the same stdlib+loguru-only contract, and install_exception_hooks()
installs its scrubbing patcher (issue #105) so these hooks can never route
an unredacted traceback into the sinks.
"""
import faulthandler
import fcntl
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime

from loguru import logger as _LOG

from src.backend.log_redaction import install_log_redaction, scrub

# Issue #92 bisect switch, read once at import -- like SC_STRONG_CALLBACKS
# (src/Signals/weak_callbacks.py) this is a debugging knob, not something that
# may change behavior mid-run (the hooks are process-global; a mid-run flip
# would leave half of them installed).
#
# STRICTLY "1", matching that precedent and #92's spec. A truthiness test
# would read SC_NO_ERROR_HOOKS=false / no / off -- what an operator writes to
# turn a switch OFF -- as "on", silently dropping the entire #80 safety net
# on the run that was trying to keep it.
_HOOKS_DISABLED = os.environ.get("SC_NO_ERROR_HOOKS") == "1"
_announced_disabled = False

_installed = False
_prev_sys_hook = None
# faulthandler stores the raw fd, not the file object: this module-level
# reference must keep the file alive for the life of the process, or a
# fatal-signal dump would write into a recycled fd.
_fault_file = None


# --- Per-site rate limiting (issue #91) ------------------------------------
#
# GLib idle/timeout sources self-cancel on exception, but GTK signal handlers
# are NOT disconnected: a raising handler on a frequent signal (notify::, a
# 20-30Hz media-tick path) reaches sys.excepthook on EVERY emission, and one
# broken __del__ hits sys.unraisablehook once per object during a GC sweep.
# Unthrottled that is ~30 diagnose=True tracebacks per second into a
# 3-day-rotated logs.log and into the About-dialog ring -- the safety net's
# first big catch would DoS the log it reports to.
#
# One record per site per window; further failures within the window are
# counted and reported as a summary riding on the NEXT record from that same
# site. Deliberately per-SITE, never global: a storm on one signal handler
# must never mask a different, one-shot failure somewhere else. A count that
# will never get a next record -- its entry evicted by the prune -- is
# flushed at that moment instead (_emit_pending), never dropped: the guard
# may make a flood quiet, never invisible.
RATE_LIMIT_WINDOW_S = 5.0  # module-level so scenarios can shrink the window
_RATE_LIMIT_MAX_KEYS = 256
# _log_exc runs on whatever thread failed: any worker (threading.excepthook),
# the main/GLib thread (sys.excepthook), whichever thread happened to trigger
# a GC sweep (sys.unraisablehook), and the dispatch loop thread (asyncio). The
# window test is a read-modify-write over shared state, so it needs a lock.
# RLock, not Lock: GC can fire on an allocation INSIDE the guarded region, and
# a raising __del__ collected there re-enters this module on the same thread
# -- a plain Lock would self-deadlock inside the crash handler. The lock is
# never held across the loguru call.
_rate_lock = threading.RLock()
# site key -> [window start, suppressed, last hit]. The window start is
# refreshed only when a record is ALLOWED; the last hit on every occurrence.
# Eviction needs the second one -- see _prune_locked.
_rate_state: dict[tuple, list] = {}


def _exc_site(exc_type, exc_value, exc_tb) -> tuple[tuple, str]:
    """(key, printable label) for the code site that raised: exception type
    plus the INNERMOST traceback frame. The same handler failing on every
    emission collapses onto one key, while the same exception type raised
    from two different places keeps two.

    The type NAME, not the type object: a key must not pin a plugin's
    exception class (and through it, its module) alive for the life of the
    dict. Tracebackless calls (asyncio message-only contexts, synthesized
    errors) fall back to the message text, so two unrelated tb-less failures
    still get independent budgets instead of silencing each other."""
    type_name = getattr(exc_type, "__name__", None) or str(exc_type)
    tb = exc_tb
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is not None:
        where = (tb.tb_frame.f_code.co_filename, tb.tb_lineno)
    else:
        where = ("<no-traceback>", str(exc_value)[:120])
    return (type_name, where), _label_for_key((type_name, where))


def _label_for_key(key: tuple) -> str:
    """Printable site for a key. Rebuildable from the key alone, so a pending
    count can still be reported after its exception object is long gone (the
    prune and atexit flushes hold keys, never exceptions)."""
    type_name, where = key
    return f"{where[0]}:{where[1]} [{type_name}]"


def _emit_pending(key: tuple, count: int, reason: str) -> None:
    """Report a pending count that will never get a next record to ride on.
    Same never-raise contract as _log_exc: loguru first, __stderr__ second,
    swallow last. Carries the "Uncaught exception" prefix on purpose -- an
    incident reader greps for that string, and these counts are part of the
    same story."""
    message = (
        f"Uncaught exception [rate-limit]: {count} further failures at "
        f"{_label_for_key(key)} since the last record ({reason})"
    )
    try:
        _LOG.warning(message)
    except Exception:
        try:
            print(message, file=sys.__stderr__)
        except Exception:
            pass


def _prune_locked(now: float) -> list[tuple[tuple, int]]:
    """Cap the dict; called with _rate_lock held, only once it is oversized --
    a BROAD storm (many distinct sites) must not turn the guard itself into an
    unbounded leak. Returns the pending counts of everything it dropped, for
    the caller to report once the lock is released: an evicted count has no
    next record to ride on, and silently discarding it would let the guard
    turn a flood into silence.

    Eviction is by LAST HIT, never by window start. entry[0] is refreshed only
    when a record is allowed, so a site that is storming right now looks OLD by
    that measure while a one-shot newcomer looks fresh -- sorting on it evicted
    exactly the sites the guard exists to contain, and past the cap every hot
    site was re-admitted (and re-logged) on its next hit, collapsing the flood
    protection to nothing. Idle sites go first, then the least-recently-hit
    half, which bounds the dict unconditionally. Items are list()ed up front so
    a GC-time re-entrant hook mutating the dict cannot turn a prune into a
    'changed size during iteration' RuntimeError."""
    dropped: list[tuple[tuple, int]] = []

    def evict(key: tuple) -> None:
        entry = _rate_state.pop(key, None)
        if entry is not None and entry[1]:
            dropped.append((key, entry[1]))

    for key, entry in list(_rate_state.items()):
        if now - entry[2] >= RATE_LIMIT_WINDOW_S:
            evict(key)
    if len(_rate_state) > _RATE_LIMIT_MAX_KEYS:
        by_last_hit = sorted(list(_rate_state.items()), key=lambda kv: kv[1][2])
        for key, _entry in by_last_hit[: len(by_last_hit) // 2]:
            evict(key)
    return dropped


def _rate_limit(key: tuple) -> tuple[bool, int]:
    """(suppress this occurrence?, failures suppressed since the last record).

    The first hit of a site is always logged immediately -- throttling must
    never delay the record that says the failure exists; only what follows."""
    now = time.monotonic()
    dropped: list[tuple[tuple, int]] = []
    with _rate_lock:
        entry = _rate_state.get(key)
        if entry is not None and now - entry[0] < RATE_LIMIT_WINDOW_S:
            entry[1] += 1
            entry[2] = now
            suppress, suppressed = True, 0
        else:
            suppress = False
            suppressed = entry[1] if entry is not None else 0
            _rate_state[key] = [now, 0, now]
            if len(_rate_state) > _RATE_LIMIT_MAX_KEYS:
                dropped = _prune_locked(now)
    # Outside the lock: reporting is I/O, and a sink must never be able to
    # stall every other thread's hook.
    for dropped_key, count in dropped:
        _emit_pending(dropped_key, count, "rate-limit state pruned")
    return suppress, suppressed


def _announce_disabled() -> None:
    """Emit exactly one line so a flagged run self-identifies in the logs --
    an incident switch nobody can tell was on is half a switch.

    Deliberately announced from redirect_faulthandler() rather than from
    install_exception_hooks(): main() installs the hooks BEFORE
    config_logger() opens logs.log and the About-dialog ring, so a line
    emitted there would be stderr-only -- lost on exactly the detached
    (autostart/flatpak) runs this flag exists to debug. redirect_faulthandler()
    is called right after the sinks come up, so the line lands where an
    incident reader will actually find it."""
    global _announced_disabled
    if _announced_disabled:
        return
    _announced_disabled = True
    try:
        _LOG.warning(
            "SC_NO_ERROR_HOOKS=1: the issue-#80 exception hooks and the "
            "faulthandler redirection are DISABLED for this run -- uncaught "
            "exceptions reach stderr only, and native crash dumps are not "
            "written to logs/faulthandler.log (log redaction is unaffected)"
        )
    except Exception:
        pass


def _log_exc(kind: str, exc_type, exc_value, exc_tb, extra: str = "") -> None:
    # Rate limiting sits HERE rather than in the individual hooks, so all four
    # #80 surfaces inherit it (issue #91). Any failure inside the guard falls
    # through to logging: it may drop repeats, never an original.
    try:
        key, label = _exc_site(exc_type, exc_value, exc_tb)
        suppress, suppressed = _rate_limit(key)
        if suppress:
            return
        if suppressed:
            # "since the last record", not "in the last 5s": the gap between
            # two records from one site is unbounded (a site that goes quiet
            # for an hour and fires again reports counts an hour old), so a
            # window-worded summary would be a lie on exactly the slow
            # trickle it is meant to describe.
            extra = (
                f"{extra} ({suppressed} further failures at {label} "
                f"since the last record)"
            )
    except Exception:
        pass
    # A hook must never raise or recurse. If loguru itself fails (sink error,
    # ring lock, interpreter teardown) fall back to plain stderr; if even
    # that fails, swallow -- losing one traceback beats crashing the process
    # from inside its own crash handler.
    try:
        _LOG.opt(exception=(exc_type, exc_value, exc_tb)).critical(
            f"Uncaught exception [{kind}]{extra}"
        )
    except Exception:
        try:
            import traceback
            traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.__stderr__)
        except Exception:
            pass


def _sys_hook(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        # Stock quiet Ctrl-C: delegate to whatever hook was installed before us.
        _prev_sys_hook(exc_type, exc_value, exc_tb)
        return
    _log_exc("main", exc_type, exc_value, exc_tb)


def _thread_hook(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    _log_exc(
        "thread", args.exc_type, args.exc_value, args.exc_traceback,
        extra=f" in thread {name!r}",
    )


def _unraisable_hook(unraisable) -> None:
    _log_exc(
        "unraisable", unraisable.exc_type, unraisable.exc_value,
        unraisable.exc_traceback,
        extra=f" ({unraisable.err_msg or 'in __del__/GC'})",
    )


def asyncio_exception_handler(loop, context) -> None:
    """loop.set_exception_handler target for long-lived loops (see
    event_dispatch._get_loop): an un-retrieved task exception or failing
    call_soon callback otherwise dies in asyncio's default stderr handler."""
    if _HOOKS_DISABLED:
        # SC_NO_ERROR_HOOKS covers this surface too (issue #92): it is one of
        # the four #80 hooks, and the wiring lives in event_dispatch, not in
        # a call this module's install path owns -- so the opt-out has to be
        # here. Delegating (rather than returning) is what makes the flag
        # "pre-#80 behavior exactly": asyncio's own stderr handler.
        loop.default_exception_handler(context)
        return
    exc = context.get("exception")
    if exc is not None:
        _log_exc("asyncio", type(exc), exc, getattr(exc, "__traceback__", None))
    else:
        message = context.get("message") or "asyncio error"
        _log_exc("asyncio", RuntimeError, RuntimeError(message), None)


def install_exception_hooks() -> None:
    """Install sys/threading/unraisable hooks. Idempotent; call before any
    code that can throw on a background thread or GLib callback.

    Also installs the issue-#105 redaction patcher: these hooks are what
    route full tracebacks into the sinks, so they must never fire without
    the scrubbing layer in place. main()'s boot path relies on this
    piggyback -- scenario_log_redaction asserts the coupling.

    SC_NO_ERROR_HOOKS=1 (issue #92) turns the hook installs into a no-op --
    sys.excepthook/threading.excepthook/sys.unraisablehook are left exactly
    as the interpreter set them. The redaction patcher is deliberately NOT
    part of that opt-out: this call is its ONLY boot-path install site, so
    skipping it would silently ship a run with no PII scrubbing on ANY log
    line, turning a debugging switch into a privacy regression. The flag
    disables the hooks it is named after, nothing else."""
    global _installed, _prev_sys_hook
    install_log_redaction()
    if _HOOKS_DISABLED or _installed:
        return
    _prev_sys_hook = sys.excepthook
    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    _installed = True


def _scrub_fault_log(path: str) -> None:
    """Scrub PREVIOUS sessions' faulthandler dumps in place (issue #122).

    faulthandler writes its dumps at the C level straight to the stored fd
    -- by design, so they still land when the interpreter is wedged -- which
    means the issue-#105 loguru patcher never sees them, and traceback frame
    paths (File "/home/<user>/...") reach disk raw. A live intercept is
    impossible without breaking that wedged-interpreter guarantee, so the
    file is scrubbed here instead, at boot, right before the next boot
    marker is appended: the sharing scenario only ever reads this file after
    a restart. Residual risk (accepted, see #122): a dump written during the
    CURRENT session stays raw on disk until the next boot.

    Streams line-by-line (dumps are line-oriented and every scrub() pattern
    is single-line) so a years-old multi-boot file cannot balloon memory:
    the scrubbed content goes to a unique same-dir mkstemp tmp first, then
    is copied back over the original IN PLACE. In place, not os.replace
    (issue #159): replace swaps the inode, which strands every already-
    RUNNING instance's registered faulthandler fd on the unlinked old file
    -- a booting secondary (this runs before the single-instance gate) then
    silently redirected the primary's future crash/SIGQUIT dumps into a
    file nothing can find. Same-inode rewrite keeps live fds valid, and
    mode/ownership untouched (nothing is re-created); the one coverage
    trade is that "r+" needs WRITE permission, so a manually chmod'd
    read-only log is skipped (warning below) where os.replace would have
    swapped it.

    Concurrency: the flock is NON-blocking -- on contention the scrub is
    skipped outright, because the only legitimate holder is another boot
    scrubbing this same file to the same result, and waiting would let a
    wedged holder (SIGSTOP, stalled disk) block every subsequent launch.
    The flock is advisory: it excludes other scrubbers only. A dump another
    instance appends at the C level DURING the writeback window lands past
    the read snapshot and is lost to truncate() -- accepted; the old code
    lost the same dump to the unlinked inode AND stranded the fd forever.
    Scrubbing is idempotent for faulthandler content (frame paths, boot
    markers -- all fixed points; scrub() is NOT idempotent in general, see
    issue #162), so a second boot's re-scrub is a content no-op -- and the
    unchanged-detection below turns it into a no-WRITE as well: the file is
    only rewritten when a line actually changed, so the partial-writeback
    window (crash/ENOSPC mid-copy) exists only on boots that had raw PII to
    redact. Any failure is logged and swallowed -- a scrub problem must
    never block startup."""
    if not os.path.exists(path):
        return
    tmp_path = None
    try:
        with open(path, "r+", encoding="utf-8", errors="replace") as log_file:
            try:
                fcntl.flock(log_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another boot is scrubbing this exact file right now; its
                # result is what ours would be. Never wait.
                return
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(path), prefix="faulthandler.", suffix=".scrub"
            )
            changed = False
            with os.fdopen(fd, "w+", encoding="utf-8") as dst:
                for line in log_file:
                    scrubbed = scrub(line)
                    if scrubbed != line:
                        changed = True
                    dst.write(scrubbed)
                if changed:
                    dst.seek(0)
                    log_file.seek(0)
                    shutil.copyfileobj(dst, log_file)
                    log_file.truncate()
        os.unlink(tmp_path)
        tmp_path = None
    except Exception as e:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        try:
            _LOG.warning(f"could not scrub faulthandler.log ({e}); continuing boot")
        except Exception:
            pass


def redirect_faulthandler(directory: str) -> None:
    """Re-point the import-time stderr faulthandler (main.py:40) at
    <directory>/faulthandler.log so native crashes / SIGQUIT dumps survive
    detached runs.

    Separate from install_exception_hooks() because gl.DATA_PATH cannot be
    resolved at import time (it can come from --data or the static settings
    file), and the short-lived CLI invocations that return before
    config_logger() must not touch the running app's files. Any failure
    falls back silently to the stderr enable() -- a missing dump target must
    never block startup. Idempotent.

    SC_NO_ERROR_HOOKS=1 (issue #92) skips the redirection entirely, leaving
    main.py's import-time faulthandler.enable() pointed at stderr: the
    pre-#80 arrangement, and no logs/faulthandler.log is opened or scrubbed.
    This is also where the flag announces itself -- see _announce_disabled()
    for why the announcement is here and not at install time."""
    global _fault_file
    if _HOOKS_DISABLED:
        _announce_disabled()
        return
    if _fault_file is not None:
        return
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "faulthandler.log")
        # Previous sessions' dumps bypassed the #105 redaction layer
        # (C-level fd writes); scrub them BEFORE opening the append fd, so
        # this boot's marker lands after the rewritten content. The scrub is
        # in-place (same inode -- issue #159), so it is also safe while
        # other instances hold registered fds on the file. Dumps from THIS
        # session stay raw until the next boot -- see _scrub_fault_log for
        # why that is accepted.
        _scrub_fault_log(path)
        f = open(path, "a", buffering=1)
        # Crash dumps are only read after a restart: append + boot markers,
        # never truncate, so the previous crash's evidence survives boot.
        f.write(f"\n===== boot {datetime.now().isoformat()} pid={os.getpid()} =====\n")
        faulthandler.enable(file=f, all_threads=True)
        faulthandler.register(signal.SIGQUIT, file=f, all_threads=True, chain=False)
        _fault_file = f
    except (AttributeError, ValueError, OSError):
        pass
