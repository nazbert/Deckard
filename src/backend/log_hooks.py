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
from datetime import datetime

from loguru import logger as _LOG

from src.backend.log_redaction import install_log_redaction, scrub

# Issue #92 bisect switch, read once at import -- like SC_STRONG_CALLBACKS
# this is a debugging knob, not something that may change behavior mid-run
# (the hooks are process-global; a mid-run flip would leave half of them
# installed). Any value except "" and "0" enables it, so the documented
# SC_NO_ERROR_HOOKS=1 and the reflexive =true both work.
_HOOKS_DISABLED = os.environ.get("SC_NO_ERROR_HOOKS", "").strip() not in ("", "0")

_installed = False
_prev_sys_hook = None
# faulthandler stores the raw fd, not the file object: this module-level
# reference must keep the file alive for the life of the process, or a
# fatal-signal dump would write into a recycled fd.
_fault_file = None


def _log_exc(kind: str, exc_type, exc_value, exc_tb, extra: str = "") -> None:
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
    pre-#80 arrangement, and no logs/faulthandler.log is opened or scrubbed."""
    global _fault_file
    if _HOOKS_DISABLED or _fault_file is not None:
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
