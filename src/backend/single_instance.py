"""
Atomic single-instance launch lock.

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

RequestName goes out as a plain synchronous call_sync, not via
Gio.bus_own_name(): claim() must return a verdict inline on the boot path,
before reset_all_decks(), and bus_own_name only reports acquired/lost through
main-loop callbacks that nothing is running to dispatch yet.
"""

import time

from gi.repository import Gio, GLib

from loguru import logger as log

# org.freedesktop.DBus.RequestName protocol constants. GDBus binds the
# request flags but not the reply codes, so the wire values are pinned here.
NAME_FLAG_DO_NOT_QUEUE = 4
REPLY_PRIMARY_OWNER = 1
REPLY_ALREADY_OWNER = 4

# The owning connection must stay referenced for the process lifetime: the
# daemon releases the name when the connection closes. The shared session
# connection Gio.bus_get_sync() hands out lives as long as the process and is
# never closed by GDBus, so on the real boot path this reference is the
# documentation of that requirement; it also pins an injected private
# connection (tests). Assigned only on a successful claim.
#
# GApplication registers the app id over this same shared connection later
# in boot -- deliberate: the lock name and the app id are distinct names on
# one connection (owning the lock never demotes the GApplication primary,
# verified by scenario_single_instance_lock), and both drop together when
# the process's connection goes away.
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

    if bus is None:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    # monotonic, not wall clock: this loop runs exactly when login-time NTP
    # steps the wall clock, which would collapse (or stretch) the grace.
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            reply = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "RequestName",
                GLib.Variant("(su)", (lock_name(app_id), NAME_FLAG_DO_NOT_QUEUE)),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NO_AUTO_START,
                5000,
                None,
            ).unpack()[0]
        except GLib.Error as e:
            log.warning(f"Single-instance lock request failed ({e}); proceeding as owner (fail-open)")
            _lock_bus = bus
            return True
        if reply in (REPLY_PRIMARY_OWNER, REPLY_ALREADY_OWNER):
            _lock_bus = bus
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
