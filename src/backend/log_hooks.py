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

Central exception hooks.

A function decorated with @log.catch feeds its exceptions into loguru. An
uncaught exception on any other path reaches stderr alone, and a detached run
under autostart or flatpak loses it. install_exception_hooks() closes the four
surfaces that leak one, and _log_exc() rate-limits what they report.

Every hook routes through loguru, so a record reaches every sink that
config_logger() installed, which are logs/logs.log, stderr and the gl.logs
ring behind the About dialog. Loguru's default stderr sink catches a record
emitted before that call. Each hook resolves the sinks at call time, so no
re-install follows.

This module stays importable before globals, which the fixtures.py contract
of the test harness needs, so it imports stdlib and loguru only. log_redaction
is the one allowed sibling import, on the same contract, and
install_exception_hooks() installs its scrubbing patcher, so no hook routes an
unredacted traceback into a sink.
"""
import atexit
import faulthandler
import fcntl
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any

from loguru import logger as _LOG

from src.backend.log_redaction import install_log_redaction, scrub

# A bisect switch, read once at import, like SC_STRONG_CALLBACKS in
# src/Signals/weak_callbacks.py. Set to 1 it turns install_exception_hooks()
# and redirect_faulthandler() into no-ops, so an operator compares a field
# anomaly that involves the hooks against the un-hooked behaviour with an
# environment variable rather than a revert and a rebuild. It is a debugging
# knob and must not change behaviour mid-run, because the hooks are
# process-global and a mid-run flip leaves half of them installed.
#
# The test accepts "1" only, like that precedent. A truth test reads
# SC_NO_ERROR_HOOKS=false, no or off, which an operator writes to turn a
# switch off, as "on", and drops the whole safety net on the run that wanted
# to keep it.
_HOOKS_DISABLED = os.environ.get("SC_NO_ERROR_HOOKS") == "1"
_announced_disabled = False

_installed = False
# The same shape as sys.excepthook, which install() stores here.
_ExceptHook = Callable[[type[BaseException], BaseException, TracebackType | None], Any]
_prev_sys_hook: _ExceptHook = None  # type: ignore[assignment]  # late-init: install(); only read from _sys_hook, which install() wires up
# faulthandler stores the raw fd and not the file object, so this
# module-level reference must keep the file alive for the life of the
# process. Otherwise a fatal-signal dump writes into a recycled fd.
_fault_file = None


# Per-site rate limiting.
#
# A GLib idle or timeout source cancels itself on an exception, and GTK keeps
# a signal handler connected. A raising handler on a frequent signal, such as
# notify:: or a media-tick path at 20 to 30 Hz, reaches sys.excepthook on
# every emission, and one broken __del__ hits sys.unraisablehook once per
# object during a GC sweep. Without a throttle that writes about 30
# diagnose=True tracebacks a second into a logs.log that rotates every 3 days,
# and into the About-dialog ring, so the safety net's first big catch floods
# the log it reports to.
#
# One record per site per window, keyed by exception type and innermost
# frame. A further failure inside the window raises a count, which rides on
# the next record from that same site. The budget covers one site and never
# the whole process, so a storm on one signal handler cannot mask a one-shot
# failure elsewhere. A count that gets no next record is flushed rather than
# dropped, when the prune evicts its entry, when the process exits through
# atexit, and onto a terminal record. The guard may make a flood quiet, and
# never invisible.
#
# That key has a known bound. Two different failures raised from one line with
# one exception type, such as a raise ValueError(...) that several callers
# reach with different messages, share one budget and can mask each other for
# a window. The message stays out of the key, because a real message carries a
# varying id, path or counter, and a message-keyed guard then throttles
# nothing, which is the failure this guard prevents. The summary line
# therefore reads "N further failures at <site>" and never "N repeats",
# because the guard knows the site and does not know that the failures
# matched.
RATE_LIMIT_WINDOW_S = 5.0  # Module-level, so a scenario can shrink the window
_RATE_LIMIT_MAX_KEYS = 256
# _log_exc runs on the thread that failed. That is a worker through
# threading.excepthook, the main GLib thread through sys.excepthook, whichever
# thread triggered a GC sweep through sys.unraisablehook, and the dispatch
# loop thread through asyncio. The window test reads and writes shared state,
# so it needs a lock. An RLock, and not a Lock, because GC can fire on an
# allocation inside the guarded region, and a raising __del__ collected there
# re-enters this module on the same thread. A plain Lock deadlocks with itself
# inside the crash handler. No holder keeps this lock across the loguru
# call.
_rate_lock = threading.RLock()
# Every acquire in the hook path is bounded. The critical section runs for a
# few microseconds, so this timeout expires only when something is wedged.
# The answer then is to log the occurrence without a throttle, and never to
# park a failing thread on a lock inside its own crash handler. With an
# unbounded acquire, one deadlocked holder hangs every later hook, including
# the hook that reports the deadlock.
_RATE_LOCK_TIMEOUT_S = 0.5
# Maps a site key to [window start, suppressed, last hit]. An allowed record
# refreshes the window start, and every occurrence refreshes the last hit. The
# eviction reads the last hit; see _prune_locked.
_rate_state: dict[tuple, list] = {}


def _exc_site(exc_type, exc_value, exc_tb) -> tuple[tuple, str]:
    """Returns (key, printable label) for the code site that raised. The key
    holds the exception type and the innermost traceback frame. One handler
    that fails on every emission collapses onto one key, and one exception
    type raised from two places keeps two keys.

    The key holds the type name and not the type object, so it never pins a
    plugin's exception class, and through it that plugin's module, alive for
    the life of the dict. A call with no traceback, such as an asyncio
    message-only context or a synthesized error, falls back to the message
    text, so two unrelated failures of that shape keep separate budgets."""
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
    """The printable site for a key. It builds from the key alone, so a
    pending count still reports after its exception object is gone. The prune
    and the atexit flush hold keys and never exceptions."""
    type_name, where = key
    return f"{where[0]}:{where[1]} [{type_name}]"


def _emit_pending(key: tuple, count: int, reason: str) -> None:
    """Report a pending count that gets no next record to ride on. It follows
    the never-raise contract of _log_exc, with loguru first, __stderr__
    second, and a swallow last. It keeps the "Uncaught exception" prefix,
    because an incident reader greps for that string and these counts belong
    to the same story."""
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
    """Cap the dict. The caller holds _rate_lock and calls this only once the
    dict is oversized, so a broad storm across many distinct sites cannot turn
    the guard into an unbounded leak. Returns the pending counts of everything
    it dropped, and the caller reports them once it releases the lock. An
    evicted count gets no next record to ride on, and a silent discard lets
    the guard turn a flood into silence.

    The eviction reads the last hit and never the window start. An allowed
    record alone refreshes entry[0], so a site that storms right now looks old
    by that measure while a one-shot newcomer looks fresh. A sort on entry[0]
    evicts the sites this guard contains, and past the cap every hot site
    re-enters, and logs again, on its next hit, which removes the flood
    protection. Idle sites go first, then the least recently hit half, which
    bounds the dict. This lists the items up front, so a re-entrant hook at GC
    time that mutates the dict cannot turn a prune into a RuntimeError of
    "changed size during iteration"."""
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


def _flush_pending_counts() -> None:
    """The atexit hook. It reports every count that still waits for a next
    record to ride on.

    Without this hook a storm that stops, because a handler got disconnected,
    the page changed, or the process quits mid-window, loses its last window's
    failures, which this guard must never do. atexit needs no thread of its
    own, because it runs on the thread that shuts the interpreter down.

    The acquire takes a bounded wait and skips the flush on a failure, because
    a process exit must not hang on a wedged holder. The non-blocking flock in
    _scrub_fault_log follows the same rule. A diagnostic must not stop a
    shutdown."""
    if not _rate_state:
        return
    if not _rate_lock.acquire(timeout=2.0):
        return
    try:
        pending = [(key, entry[1]) for key, entry in list(_rate_state.items()) if entry[1]]
        _rate_state.clear()
    finally:
        _rate_lock.release()
    for key, count in pending:
        _emit_pending(key, count, "process exiting")


atexit.register(_flush_pending_counts)


def _rate_limit_bypass(key: tuple) -> int:
    """Take a site's pending count, clear it, and throttle nothing.

    The terminal path enters here. That record is the last one this site gets,
    so whatever its window swallowed must ride on it rather than wait for a
    next record that never comes."""
    if not _rate_lock.acquire(timeout=_RATE_LOCK_TIMEOUT_S):
        return 0
    try:
        entry = _rate_state.get(key)
        if entry is None:
            return 0
        pending, entry[1] = entry[1], 0
        entry[2] = time.monotonic()
        return pending
    finally:
        _rate_lock.release()


def _rate_limit(key: tuple) -> tuple[bool, int]:
    """Returns (suppress this occurrence, failures suppressed since the last
    record).

    The first hit of a site always logs at once. A throttle must never delay
    the record that says the failure exists, and only delays what follows."""
    now = time.monotonic()
    dropped: list[tuple[tuple, int]] = []
    if not _rate_lock.acquire(timeout=_RATE_LOCK_TIMEOUT_S):
        # Never wait without a bound inside a crash handler. With the guard's
        # state wedged, log this occurrence rather than park the failing
        # thread on a lock. The throttle saves work, and a hook that hangs
        # costs more than a log that repeats itself.
        return False, 0
    try:
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
    finally:
        _rate_lock.release()
    # Report outside the lock. A report is I/O, and a sink must not stall
    # every other thread's hook.
    for dropped_key, count in dropped:
        _emit_pending(dropped_key, count, "rate-limit state pruned")
    return suppress, suppressed


def _announce_disabled() -> None:
    """Emit one line, so a flagged run identifies itself in the logs. A
    reader must be able to tell that the switch was on.

    redirect_faulthandler() announces this, and install_exception_hooks()
    does not. main() installs the hooks before config_logger() opens logs.log
    and the About-dialog ring, so a line emitted there reaches stderr alone,
    and a detached run under autostart or flatpak loses it, which is the run
    this flag debugs. redirect_faulthandler() runs right after the sinks come
    up, so the line lands where an incident reader finds it."""
    global _announced_disabled
    if _announced_disabled:
        return
    _announced_disabled = True
    try:
        _LOG.warning(
            "SC_NO_ERROR_HOOKS=1: the crash-logging exception hooks and the "
            "faulthandler redirection are DISABLED for this run -- uncaught "
            "exceptions reach stderr only, and native crash dumps are not "
            "written to logs/faulthandler.log (log redaction is unaffected)"
        )
    except Exception:
        pass


def _is_terminal(exc_tb) -> bool:
    """True when the interpreter called sys.excepthook for an exception that
    unwound the whole program, and False when PyGObject's PyErr_Print called
    it for a GLib or GTK callback.

    This app reaches sys.excepthook mostly along the callback path, so the
    distinction decides the throttle. PyGObject routes every uncaught callback
    exception through it (see the module docstring), which is the storm at 20
    to 30 Hz that the rate limit exists for, so an exemption for the whole
    surface reopens that storm. An exemption for the terminal shape alone
    keeps a fatal exception out of a window that a hot handler opened, and
    costs nothing else.

    The shape test reads the frames. A terminal exception unwound through the
    entry script's module frame, so its outermost traceback frame is <module>
    in __main__. A callback invoked from C carries its own function frame
    there, whatever module defined it."""
    frame = getattr(exc_tb, "tb_frame", None)
    if frame is None:
        return False
    try:
        return (
            frame.f_code.co_name == "<module>"
            and frame.f_globals.get("__name__") == "__main__"
        )
    except Exception:
        return False


def _log_exc(kind: str, exc_type, exc_value, exc_tb, extra: str = "",
             rate_limit: bool = True) -> None:
    # The rate limit sits here rather than in each hook, so all four hook
    # surfaces get it. A failure inside the guard falls through to the log. It
    # may drop a repeat, and never an original.
    try:
        key, label = _exc_site(exc_type, exc_value, exc_tb)
        if rate_limit:
            suppress, suppressed = _rate_limit(key)
            if suppress:
                return
        else:
            suppressed = _rate_limit_bypass(key)
        if suppressed:
            # The text reads "since the last record" and not "in the last
            # 5s". The gap between two records from one site has no bound. A
            # site that goes quiet for an hour and fires again reports counts
            # an hour old, and a window-worded summary misreports exactly
            # that slow trickle.
            extra = (
                f"{extra} ({suppressed} further failures at {label} "
                f"since the last record)"
            )
    except Exception:
        pass
    # A hook must never raise and never recurse. When loguru itself fails,
    # through a sink error, the ring lock or interpreter teardown, fall back
    # to plain stderr. When that fails too, swallow the error. One lost
    # traceback costs less than a crash inside the crash handler.
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
        # Keep Ctrl-C quiet. Delegate to the hook installed before this one.
        _prev_sys_hook(exc_type, exc_value, exc_tb)
        return
    # A terminal exception passes the rate limit. It cannot flood, because
    # the process is on its way out, and it is the one record that must not
    # fall into a window that a hot callback opened. A callback-shaped call of
    # this same hook stays throttled; see _is_terminal.
    _log_exc("main", exc_type, exc_value, exc_tb,
             rate_limit=not _is_terminal(exc_tb))


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
    """The loop.set_exception_handler target for a long-lived loop; see
    event_dispatch._get_loop. Without it an unread task exception, or a
    failing call_soon callback, dies in asyncio's default stderr handler."""
    if _HOOKS_DISABLED:
        # SC_NO_ERROR_HOOKS covers this surface too, because it is one of
        # the four hooks. event_dispatch holds the wiring, and this module's
        # install path does not, so the opt-out lives here. This delegates
        # rather than returns, so the flag gives the un-hooked behaviour,
        # which is asyncio's own stderr handler.
        loop.default_exception_handler(context)
        return
    exc = context.get("exception")
    if exc is not None:
        _log_exc("asyncio", type(exc), exc, getattr(exc, "__traceback__", None))
    else:
        message = context.get("message") or "asyncio error"
        _log_exc("asyncio", RuntimeError, RuntimeError(message), None)


def install_exception_hooks() -> None:
    """Install the sys, threading and unraisable hooks. Idempotent. Call it
    before any code that can throw on a background thread or a GLib callback.

    This closes four surfaces. The main thread and every GLib or Gio callback,
    which covers idle_add, timeout_add, signal handlers and Gio actions,
    because PyGObject routes their uncaught exceptions through PyErr_Print,
    which calls sys.excepthook. A plain threading.Thread target, through
    threading.excepthook. An error inside __del__, a weakref finalizer or a GC
    sweep, through sys.unraisablehook. The plugin-dispatch asyncio loop,
    through asyncio_exception_handler, which event_dispatch._get_loop wires up.

    One blind spot stays. An exception inside a ThreadPoolExecutor task sits
    on its Future and never reaches threading.excepthook, so a submit site
    must attach a done-callback, as main_loop.run_in_background and
    DeckController._log_callback_exception do.

    It also installs the redaction patcher. These hooks route a full traceback
    into the sinks, so they must never fire without the scrubbing layer.
    main()'s boot path depends on that pairing, and scenario_log_redaction
    asserts it.

    SC_NO_ERROR_HOOKS=1 turns the hook installs into a no-op and leaves
    sys.excepthook, threading.excepthook and sys.unraisablehook as the
    interpreter set them. The redaction patcher stays outside that opt-out.
    This call is its only boot-path install site, so a skip ships a run with
    no PII scrubbing on any log line, which turns a debugging switch into a
    privacy defect. The flag disables the hooks it names, and nothing
    else."""
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
    """Scrub the faulthandler dumps of earlier sessions, in place.

    faulthandler writes a dump at the C level, straight to the stored fd, so
    the dump still lands while the interpreter is wedged. The loguru redaction
    patcher therefore never sees it, and a traceback frame path such as
    File "/home/<user>/..." reaches disk raw. A live intercept would break the
    wedged-interpreter guarantee, so this scrubs the file at boot, right
    before the next boot marker appends, because a reader opens this file
    after a restart. One known limitation stays. A dump written during the
    current session stays raw on disk until the next boot.

    This streams line by line, because a dump is line-oriented and every
    scrub() pattern fits one line, so a years-old multi-boot file cannot
    balloon memory. The scrubbed content goes to a unique mkstemp temp file in
    the same directory, and then copies back over the original in place. It
    must not use os.replace. A replace swaps the inode, which strands the
    registered faulthandler fd of every running instance on the unlinked old
    file. A booting secondary, and this runs before the single-instance gate,
    then redirects the primary's later crash and SIGQUIT dumps into a file
    that nothing finds. A same-inode rewrite keeps a live fd valid and leaves
    the mode and the ownership alone, because nothing re-creates the file. It
    costs one case. "r+" needs write permission, so this skips a log that
    somebody chmod'd read-only, and logs the warning below. os.replace instead
    swaps such a file.

    The flock does not block. On contention this skips the scrub, because the
    only legitimate holder is another boot that scrubs this same file to the
    same result, and a wait lets a wedged holder, under SIGSTOP or a stalled
    disk, block every later launch. The flock is advisory and excludes another
    scrubber only. A dump that another instance appends at the C level during
    the writeback window lands past the read snapshot and truncate() drops it.
    A replace instead loses the same dump to the unlinked inode and strands
    the fd for good. A scrub of faulthandler content is idempotent, because a
    frame path and a boot marker are fixed points, and scrub() is not
    idempotent in general. A second boot's scrub therefore changes no content,
    and the unchanged check below turns it into no write at all. The file is
    rewritten only when a line changed, so the partial-writeback window, from
    a crash or an ENOSPC mid-copy, opens only on a boot that had raw PII to
    redact. Any failure logs and returns, because a scrub problem must not
    block startup."""
    if not os.path.exists(path):
        return
    tmp_path = None
    try:
        with open(path, "r+", encoding="utf-8", errors="replace") as log_file:
            try:
                fcntl.flock(log_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another boot scrubs this file right now, to the same
                # result. Never wait.
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
    """Re-point the import-time stderr faulthandler of main.py at
    <directory>/faulthandler.log, so a native crash dump and a SIGQUIT dump
    survive a detached run.

    This stays apart from install_exception_hooks(), because gl.DATA_PATH does
    not resolve at import time, since --data or the static settings file can
    set it, and because a short-lived CLI call that returns before
    config_logger() must not touch the running app's files. Any failure falls
    back to the stderr enable(), because a missing dump target must not block
    startup. Idempotent.

    SC_NO_ERROR_HOOKS=1 skips the redirection and leaves the import-time
    faulthandler.enable() of main.py pointed at stderr, which is the un-hooked
    arrangement, and it opens and scrubs no logs/faulthandler.log. The flag
    also announces itself here; see _announce_disabled() for why the
    announcement lands here and not at install time."""
    global _fault_file
    if _HOOKS_DISABLED:
        _announce_disabled()
        return
    if _fault_file is not None:
        return
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "faulthandler.log")
        # A dump from an earlier session skipped the redaction layer, because
        # faulthandler writes at the C level to an fd. Scrub those dumps
        # before this code opens the append fd, so this boot's marker lands
        # after the rewritten content. The scrub keeps the inode, so it stays
        # safe while another instance holds a registered fd on the file. A
        # dump from this session stays raw until the next boot; see
        # _scrub_fault_log for why.
        _scrub_fault_log(path)
        f = open(path, "a", buffering=1)
        # A reader opens a crash dump after a restart, so this appends a boot
        # marker and never truncates, and the evidence of the previous crash
        # survives the boot.
        f.write(f"\n===== boot {datetime.now().isoformat()} pid={os.getpid()} =====\n")
        faulthandler.enable(file=f, all_threads=True)
        faulthandler.register(signal.SIGQUIT, file=f, all_threads=True, chain=False)
        _fault_file = f
    except (AttributeError, ValueError, OSError):
        pass
