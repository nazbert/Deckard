"""Uniqueness, decided once, before this launch does anything exclusive.

WHY THIS IS A MODULE

main.py cannot be imported by anything -- its module body re-execs the process
and runs the rename migration against the real user directories -- so every
uniqueness decision that lived there was untestable, and the shape of the bugs
showed it (a flat five-second sleep standing in for "the other instance let go
of the name", a hand-off poll covering a window that no longer exists). What
stays in main.py is reading argv, wiring, and leaving the process; the decision
itself lives here, where a scenario can drive it against a real bus daemon.

THE ORDER IS THE DESIGN

The application name is claimed by GApplication's own ``register()`` and by
nothing else in the tree. Everything expensive or exclusive -- migrations,
plugin load, opening the decks -- runs after that call returns, on the primary
only, so a launch that loses the race performs nothing that could collide with
the winner. ``establish()`` therefore does four things, in this order:

1. ``--close-running`` first, because registration is ONE-SHOT per process: an
   application that registered as a remote can never re-register as the
   primary, so asking the running instance to quit and waiting for it to let
   go has to happen before this process registers anything. This is the one
   hand-rolled step that survives, and it is a probe plus a wait -- it never
   requests the name itself.
2. ``publish()`` BEFORE ``register()``. The objects go up on the shared session
   connection while the name is still unowned, so the instant the daemon grants
   the name, the object set behind it is already addressable: name-owned
   implies objects-published, by construction, with no window in between for a
   client to fall into. (GDBus keeps registrations per object path AND
   interface, so the application's own org.gtk.* interfaces and the API's
   interface coexist at one path in either order.)
3. ``register()``, failing OPEN where failing open is safe. A launch with no
   usable session bus still boots -- windowed, with the API and uniqueness
   degraded and logged -- rather than dying halfway through startup. What it
   never does is decide it is the primary while another process owns the name
   (see _registration_failed): registering as a REMOTE takes an answer from
   the owner, an owner that has registered but not yet started dispatching its
   main loop gives none until it does, and past the bus timeout the honest
   outcome is to stop, not to open the decks the other instance is holding.
4. The verdict: a remote hands off to the primary and exits; the primary shoos
   a pre-rename instance off the Stream Deck before it opens any deck itself.

WHAT REGISTERING ALSO DOES

``register()`` emits the application's ``startup``, so the toolkit's own
startup chain runs here -- before the globals exist, since that is the point of
registering first. The app overrides nothing on that path; anything that ever
does has to hold that constraint or move.

It also makes this process reachable before it is responsive: a launch that
arrives while this one is still booting joins it as a remote, and joining takes
an answer that only arrives once this process starts dispatching its main loop.
That launch therefore waits out the boot (bounded by the bus's 25s default,
measured) and then presents the window, instead of failing early.

THE UPGRADE WINDOW

A build that predates this ordering owns nothing until its main loop starts,
so a launch of this build that lands while one of those is still booting sees
an unowned name and becomes the primary: two primaries, briefly, on one
machine. It needs a version change and a launch inside one boot window to
happen, and the next start of either build ends it.

WHY THE FLAGS ARE SET BEFORE REGISTERING

``g_application_set_flags`` asserts on an application that is already
registered -- it emits a CRITICAL and keeps the old flags -- so NON_UNIQUE can
only be set while registration has not happened (or has failed). That is why
the busless case is detected by this module's own bus probe up front instead of
by catching an error out of ``register()``: GApplication answers an unreachable
session bus by quietly proceeding as a non-unique application, returning True
and raising nothing at all.

WHY THIS MODULE IS NOT ON THE FLOOR IMPORT LIST

It imports ``gi`` at module level, and its only consumer is main.py plus the
scenarios that drive it. The floor check covers modules whose design claim is
"any layer may import this, on a bare interpreter"; this one's claim is
narrower -- it runs in the one process that already has the toolkit loaded --
so it is checked for 3.13 syntax by compileall like everything else, and no
more. The API module makes the same trade for the same reason.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Callable, Protocol

from gi.repository import Gio, GLib

from loguru import logger as log

import appinfo
from src.backend.cli_forward import DBUS_CALL_TIMEOUT_MS

# How long --close-running waits for the instance it asked to quit to drop the
# application name, and how often it asks. The wait is bounded because the
# alternative is a launch that hangs on an instance which never exits; 10s is
# the same grace the retiring launch lock used, and it covers a teardown that
# has to flush pages and terminate plugin backends first.
CLOSE_GRACE_SECONDS = 10.0
RELEASE_POLL_SECONDS = 0.2

# How long a single dispatch probe waits for its answer. Short because it is
# asked repeatedly on the poll cadence, and a dispatching instance answers it
# in microseconds.
DISPATCH_PROBE_TIMEOUT_MS = 1000


class Decision(Enum):
    """What this launch is."""

    #: This process owns the application name: boot.
    PRIMARY = "primary"
    #: No usable session bus, so nobody owns anything: boot anyway, degraded.
    PRIMARY_UNREGISTERED = "primary-unregistered"
    #: Another process owns the name: hand off to it and exit.
    REMOTE = "remote"


class LaunchAborted(Exception):
    """This launch ends here, with nothing started and a non-zero exit.

    Raised rather than returned because these are not kinds of launch: the
    Decision values all mean "carry on, this way", and these mean "stop". The
    caller prints the message and exits.
    """


class CloseRunningFailed(LaunchAborted):
    """--close-running was asked for and the running instance is still there.

    A launch that was told to close what is running, and did not, must not
    report success -- and must certainly not go on to boot a second instance
    alongside the one it failed to close.
    """


class HandoffFailed(LaunchAborted):
    """Another instance owns the name and registration could not join it.

    Registering as a remote is not a local operation: it needs an answer from
    the process that owns the name, which a mid-boot instance only gives once
    its main loop starts dispatching. Past the bus timeout this launch has no
    way to become the remote it is -- and booting instead would put a second
    instance on the same decks, which is the one outcome worse than failing.
    """


class Application(Protocol):
    """What the gate needs from the application it decides for.

    Structural on purpose: a scenario drives ``establish()`` with a plain
    ``Gio.Application`` and a test-scoped id, which is the real registration
    machinery without a display or any of the app's own construction.
    """

    def get_application_id(self) -> str | None: ...

    def register(self) -> bool: ...

    def get_is_remote(self) -> bool: ...

    def get_flags(self) -> Gio.ApplicationFlags: ...

    def set_flags(self, flags: Gio.ApplicationFlags, /) -> None: ...


def object_path_for(app_id: str) -> str:
    """The object path an application with `app_id` exports its actions at.

    GApplication derives the path from the id by exactly this rule, and so does
    appinfo for the app's own id -- deriving it here means a test-scoped id
    brings its own path along instead of needing a second table kept in step.
    """
    return "/" + app_id.replace(".", "/")


def name_has_owner(session_bus: Gio.DBusConnection, name: str) -> bool:
    """Is `name` owned on the session bus right now?

    Asking the bus daemon (and with NO_AUTO_START) is what keeps a probe a
    probe: addressing a well-known name directly would D-Bus-activate it.
    """
    return session_bus.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        GLib.Variant("(s)", (name,)),
        GLib.VariantType("(b)"),
        Gio.DBusCallFlags.NO_AUTO_START,
        DBUS_CALL_TIMEOUT_MS,
        None
    ).unpack()[0]


def activate_action(session_bus: Gio.DBusConnection, name: str, object_path: str,
                    action: str, parameter: GLib.Variant | None = None) -> None:
    """Invoke one of the running instance's GActions over org.gtk.Actions."""
    session_bus.call_sync(
        name,
        object_path,
        "org.gtk.Actions",
        "Activate",
        GLib.Variant("(sava{sv})", (action, [] if parameter is None else [parameter], {})),
        None,
        Gio.DBusCallFlags.NO_AUTO_START,
        DBUS_CALL_TIMEOUT_MS,
        None
    )


def is_no_reply(error: GLib.Error) -> bool:
    """The peer took the call and never answered.

    Two distinct shapes carry that meaning: a NoReply the bus hands back, and
    the timeout GDBus raises client-side when the reply never lands. Both
    leave the caller in the same position, so both are matched here.
    """
    if Gio.DBusError.get_remote_error(error) == "org.freedesktop.DBus.Error.NoReply":
        return True
    return error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT)


def _session_bus() -> Gio.DBusConnection | None:
    """The shared session connection, or None if there is no bus to be had.

    The same connection the application registers on and the API publishes on,
    which is what lets step 2 and step 3 of establish() be about one thing.
    """
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as e:
        log.warning(f"No session bus available ({e}); this launch cannot tell "
                    f"whether another instance is running")
        return None


def _wait_for_release(session_bus: Gio.DBusConnection, name: str,
                      grace_seconds: float) -> bool:
    """Poll until `name` is unowned. True if it was released within the grace.

    monotonic, not wall clock: this loop runs at login, which is exactly when
    NTP steps the wall clock and would collapse (or stretch) the grace.
    """
    deadline = time.monotonic() + grace_seconds
    while True:
        try:
            if not name_has_owner(session_bus, name):
                return True
        except GLib.Error as e:
            # A probe that cannot complete reads as "nobody home", the same way
            # a failed probe does everywhere else here: continuing is the safer
            # outcome than refusing over a bus hiccup.
            log.debug(f"Could not probe {name}: {e}")
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RELEASE_POLL_SECONDS)


def _is_dispatching(session_bus: Gio.DBusConnection, app_id: str) -> bool:
    """Is the owner of `app_id` running its main loop, right now?

    Asks the OWNER'S ACTION GROUP, not the process. GDBus answers
    org.freedesktop.DBus.Peer.Ping (and Introspect) from its own worker thread,
    so an instance that has registered but not yet reached its main loop
    answers both instantly -- measured, and useless as a liveness question.
    DescribeAll is dispatched by the very action group a quit request is
    delivered to, so an answer means more than "the process exists": it means a
    quit sent now will be acted on rather than queued behind the rest of a boot.
    """
    try:
        session_bus.call_sync(
            app_id,
            object_path_for(app_id),
            "org.gtk.Actions",
            "DescribeAll",
            None,
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            DISPATCH_PROBE_TIMEOUT_MS,
            None
        )
        return True
    except GLib.Error as e:
        log.debug(f"The running instance is not dispatching yet: {e}")
        return False


def _wait_until_dispatching(session_bus: Gio.DBusConnection, app_id: str) -> bool:
    """Poll until the owner answers, or the grace runs out."""
    deadline = time.monotonic() + CLOSE_GRACE_SECONDS
    while True:
        if _is_dispatching(session_bus, app_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RELEASE_POLL_SECONDS)


def _close_running_instance(session_bus: Gio.DBusConnection, app_id: str) -> None:
    """Ask whatever owns `app_id` to quit, and wait for it to let go.

    Raises CloseRunningFailed if it is still there after the grace -- and
    leaves it UNASKED if it never got far enough to hear the question.

    The order matters more than it looks. An instance that is still booting
    owns the name but dispatches nothing, so a quit sent at it is not refused:
    it waits in the incoming queue and is acted on the moment that instance
    starts its main loop, which is typically after this process has already
    given up and exited. That leaves nobody running at all -- worse than either
    outcome the flag can honestly produce. So the question is only asked once
    the answer can be heard, and a target that never starts answering is left
    alone and reported.

    Each wait gets its own grace: an instance that took most of one to start
    dispatching must not then be asked to quit and declared a failure a moment
    later, which is the same "asked, dying, reported as never started" shape.
    """
    log.info("Checking if another instance is running")
    try:
        running = name_has_owner(session_bus, app_id)
    except GLib.Error as e:
        log.debug(f"Could not probe for a running instance: {e}")
        return
    if not running:
        # Expected path when --close-running is passed to a fresh boot.
        log.info("No other instance running, continuing")
        return

    if not _wait_until_dispatching(session_bus, app_id):
        raise CloseRunningFailed(
            f"The running instance is still starting up and did not begin "
            f"answering within {CLOSE_GRACE_SECONDS:.0f}s; it was left running "
            f"and nothing was started"
        )

    log.info("Closing running instance")
    try:
        activate_action(session_bus, app_id, object_path_for(app_id), "quit")
    except GLib.Error as e:
        if is_no_reply(e):
            # An instance that quits ends its process inside the handler, so
            # the reply never comes. That is what success looks like from here,
            # not a failure -- and it is why this no longer ends the launch on
            # its own: the release poll below is the only thing that knows.
            log.debug(f"The running instance did not answer the quit request: {e}")
        else:
            log.warning(f"Could not ask the running instance to quit: {e}")

    if not _wait_for_release(session_bus, app_id, CLOSE_GRACE_SECONDS):
        raise CloseRunningFailed(
            f"The running instance was asked to quit and had not exited "
            f"{CLOSE_GRACE_SECONDS:.0f}s later; nothing was started"
        )


def _shoo_pre_rename_instance(session_bus: Gio.DBusConnection) -> None:
    """Ask a still-running pre-rename instance to quit, before any deck opens.

    A build from before the rename still owns the old bus name, which this
    launch's own name says nothing about -- so without this it would go on to
    open decks that instance still holds. Probe with NameHasOwner, and never
    address the old name directly: a plain call to a well-known name activates
    it, which for the old id could START an upstream install via its D-Bus
    service file -- the very race this exists to prevent. NameHasOwner==False
    (the normal case) is also the effective sunset: once nothing owns the old
    name, this is a single cheap no-op round trip per launch.

    Primary-only, and after registration: a launch that is handing off to
    another instance has no business shooing anything.
    """
    try:
        if not name_has_owner(session_bus, appinfo.OLD_APP_ID):
            return
    except GLib.Error as e:
        log.debug(f"Could not probe the pre-rename bus name: {e}")
        return
    log.warning("Pre-rename StreamController instance detected on the session bus; asking it to quit")
    try:
        activate_action(session_bus, appinfo.OLD_APP_ID, appinfo.OLD_DBUS_OBJECT_PATH, "quit")
    except GLib.Error as e:
        if not is_no_reply(e):
            log.error(f"Could not close the pre-rename instance: {e}")
            return
        # An instance that quits inside the handler never replies -- the shape
        # success takes here, and the only reason to go on to the poll below.
        log.debug(f"The pre-rename instance did not answer the quit request: {e}")
    # Bounded poll for it to drop the name, rather than a flat sleep.
    _wait_for_release(session_bus, appinfo.OLD_APP_ID, CLOSE_GRACE_SECONDS)


def _registration_failed(app: Application, session_bus: Gio.DBusConnection | None,
                         app_id: str | None, error: GLib.Error) -> Decision:
    """Decide what a failed ``register()`` means, and never guess "primary".

    Becoming the REMOTE of an application that is already running is the case
    that fails here in practice: joining the owner takes a round trip to it,
    and an owner that has registered but not yet reached its main loop answers
    nothing until it does. Past the bus's own timeout the launch is out of
    options -- but the one thing it must not do is decide it is the primary,
    because the instance it could not reach is holding the decks.

    A failure with the name unowned is a different animal entirely (a daemon
    refusing the request, say): nothing is running, so booting degraded is
    strictly better than not booting.
    """
    owner = False
    if session_bus is not None and app_id is not None:
        try:
            owner = name_has_owner(session_bus, app_id)
        except GLib.Error as probe_error:
            log.debug(f"Could not probe for the name's owner: {probe_error}")
    if owner:
        raise HandoffFailed(
            f"Another instance is running but did not answer in time ({error}); "
            f"nothing was started"
        )
    log.error(f"Could not register the application on the session bus ({error}); "
              f"continuing without single-instance handling")
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    return Decision.PRIMARY_UNREGISTERED


def establish(app: Application, *, publish: Callable[[], None],
              close_running: bool) -> Decision:
    """Decide what this launch is, and leave the application registered.

    `publish` is called once, before registration, and is expected to contain
    its own failures (the app's API service does); nothing here interprets its
    result. `close_running` is passed in rather than read from the parsed
    arguments so this module holds no process state at all.
    """
    session_bus = _session_bus()
    app_id = app.get_application_id()

    if close_running and session_bus is not None and app_id is not None:
        _close_running_instance(session_bus, app_id)

    if session_bus is None:
        # Say what is true before registering, because it cannot be said after:
        # an application registered without a bus reports itself as the primary
        # and would look indistinguishable from one that owns the name.
        app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)

    # Objects first: from the moment the name below is granted, everything
    # behind it is already there to be called.
    publish()

    try:
        app.register()
    except GLib.Error as e:
        return _registration_failed(app, session_bus, app_id, e)

    if session_bus is None:
        log.warning("Started without a session bus: this launch cannot be "
                    "reached by the CLI and does not exclude a second instance")
        return Decision.PRIMARY_UNREGISTERED

    if app.get_is_remote():
        # The objects published above went onto this connection's unique name,
        # which nobody addresses; the process is about to exit and takes the
        # whole connection, objects included, with it.
        log.info("Another instance owns the application name; handing off to it")
        return Decision.REMOTE

    log.info("This launch owns the application name")
    # Primary arm only: a launch that reached PRIMARY_UNREGISTERED with a
    # working bus (a daemon that refused the name request) boots without ever
    # shooing a pre-rename instance, and would share the decks with one.
    _shoo_pre_rename_instance(session_bus)
    return Decision.PRIMARY
