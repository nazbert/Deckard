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
must attach a done-callback (the GtkHelper.run_in_background /
DeckController._log_callback_exception convention).

Import discipline: this module must stay importable before `globals` (the
test harness's fixtures.py contract) -- stdlib + loguru only, nothing from
src/ or globals.py. log_redaction is the one allowed sibling import: it
follows the same stdlib+loguru-only contract, and install_exception_hooks()
installs its scrubbing patcher (issue #105) so these hooks can never route
an unredacted traceback into the sinks.
"""
import faulthandler
import os
import signal
import sys
import tempfile
import threading
from datetime import datetime

from loguru import logger as _LOG

from src.backend.log_redaction import install_log_redaction, scrub

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
    piggyback -- scenario_log_redaction asserts the coupling."""
    global _installed, _prev_sys_hook
    install_log_redaction()
    if _installed:
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
    is single-line) so a years-old multi-boot file cannot balloon memory,
    and rewrites via a per-process tmp + os.replace so a crash mid-scrub
    cannot destroy the previous crash's evidence. The tmp is a unique
    mkstemp name in the SAME directory: same-dir keeps os.replace atomic,
    and the unique name means two near-simultaneous boots (redirect_
    faulthandler runs before the DBus single-instance probe) cannot collide
    on a shared tmp path. The source mode is copied onto the tmp before the
    replace so a user who locked the log down (0600) does not silently get
    it widened to the umask default. Best-effort: any failure is logged and
    swallowed -- a scrub problem must never block startup."""
    if not os.path.exists(path):
        return
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix="faulthandler.", suffix=".scrub"
        )
        with open(path, "r", errors="replace") as src, os.fdopen(fd, "w") as dst:
            for line in src:
                dst.write(scrub(line))
        # mkstemp creates 0600; keep the existing log's mode so a locked-down
        # dump is not silently widened by the scrub (the Page.py idiom).
        os.chmod(tmp_path, os.stat(path).st_mode & 0o777)
        os.replace(tmp_path, path)
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
    never block startup. Idempotent."""
    global _fault_file
    if _fault_file is not None:
        return
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "faulthandler.log")
        # Previous sessions' dumps bypassed the #105 redaction layer
        # (C-level fd writes); scrub them BEFORE opening the append fd, so
        # faulthandler attaches to the rewritten file rather than a
        # replaced, unlinked inode. Dumps from THIS session stay raw until
        # the next boot -- see _scrub_fault_log for why that is accepted.
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
