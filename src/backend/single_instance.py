"""
Atomic single-instance launch lock (issue #155).

main.py's quit_running() handles the visible case: a fully-booted instance
owns the app bus name, so a new launch forwards "reopen" and exits. What it
cannot catch is two launches booting at the same moment (login autostart +
session restore, field incident 2026-07-16): neither owns the app name yet,
both pass the probe 3 ms apart, and both proceed to USB-reset and fight over
the decks.

RequestName with DO_NOT_QUEUE is serialized by the D-Bus daemon, so exactly
one of any number of concurrent launches becomes the primary owner of the
lock name -- there is no check-then-act window.

The lock is a SEPARATE bus name from the app id on purpose: this connection
must never own the app id itself, or GApplication's own registration later in
boot would find the name taken and demote the very process that holds it to a
remote instance.

`dbus` is imported lazily inside claim(): this module is imported by main.py
unconditionally, and dbus-python is only expected to exist on non-mac
platforms (main.py guards its own dbus imports behind IS_MAC; claim() is
called under the same guard).
"""

import time

from loguru import logger as log

# org.freedesktop.DBus.RequestName protocol constants. dbus-python exposes
# them too, but pinning the wire values here avoids depending on which
# dbus-python submodule re-exports them.
NAME_FLAG_DO_NOT_QUEUE = 4
REPLY_PRIMARY_OWNER = 1
REPLY_ALREADY_OWNER = 4

# The owning connection must stay referenced for the process lifetime: the
# daemon releases the name when the connection closes, and dbus-python closes
# it on garbage collection. Assigned only on a successful claim.
_lock_bus = None


def lock_name(app_id: str) -> str:
    return app_id + ".Lock"


def claim(app_id: str, bus=None, wait_seconds: float = 0.0) -> bool:
    """Try to become the single running instance.

    Returns True when this process now holds the lock and may proceed to
    touch hardware; False when another launch holds it (the caller should
    hand off to the owner and exit). `wait_seconds` > 0 keeps retrying that
    long before giving up -- used by --close-running, where the quitting
    instance needs a moment to release the name.

    Fails OPEN on a broken bus (request raises): a daemon that cannot answer
    RequestName also could not have let a competing launch claim the lock, and
    refusing to boot over it would regress the pre-lock behavior. The caller
    still logs the anomaly.
    """
    global _lock_bus
    import dbus

    if bus is None:
        bus = dbus.SessionBus()

    # monotonic, not wall clock: this loop runs exactly when login-time NTP
    # steps the wall clock, which would collapse (or stretch) the grace.
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            reply = bus.request_name(lock_name(app_id), NAME_FLAG_DO_NOT_QUEUE)
        except dbus.exceptions.DBusException as e:
            log.warning(f"Single-instance lock request failed ({e}); proceeding as owner (fail-open)")
            _lock_bus = bus
            return True
        if reply in (REPLY_PRIMARY_OWNER, REPLY_ALREADY_OWNER):
            _lock_bus = bus
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
