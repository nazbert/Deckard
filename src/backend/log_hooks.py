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
src/ or globals.py.
"""
import faulthandler
import os
import signal
import sys
import threading
from datetime import datetime

from loguru import logger as _LOG

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
    code that can throw on a background thread or GLib callback."""
    global _installed, _prev_sys_hook
    if _installed:
        return
    _prev_sys_hook = sys.excepthook
    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    _installed = True


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
        f = open(os.path.join(directory, "faulthandler.log"), "a", buffering=1)
        # Crash dumps are only read after a restart: append + boot markers,
        # never truncate, so the previous crash's evidence survives boot.
        f.write(f"\n===== boot {datetime.now().isoformat()} pid={os.getpid()} =====\n")
        faulthandler.enable(file=f, all_threads=True)
        faulthandler.register(signal.SIGQUIT, file=f, all_threads=True, chain=False)
        _fault_file = f
    except (AttributeError, ValueError, OSError):
        pass
