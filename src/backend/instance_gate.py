"""Uniqueness, decided once, before this launch does anything exclusive.

Why this is a module

Nothing can import main.py, because its module body re-execs the process and
runs the rename migration against the real user directories, so every
uniqueness decision that lived there was untestable. The bugs showed it, with
a flat five-second sleep in place of "the other instance let go of the name",
and a hand-off poll over a window that does not exist. main.py keeps the
argv read, the wiring and the exit. The decision lives here, where a scenario
drives it against a real bus daemon.

The order is the design

GApplication's own register() claims the application name, and nothing else in
the tree does. Everything expensive or exclusive, which is the migrations, the
plugin load and the deck open, runs after that call returns, on the primary
alone, so a launch that loses the race performs nothing that collides with the
winner. establish() therefore does four things, in this order.

1. --close-running first, because a process registers once. An application
   that registered as a remote can never register as the primary, so the ask
   to quit, and the wait for the release, must happen before this process
   registers anything. This is the one hand-rolled step that stays, and it is
   a probe and a wait. It never requests the name.
2. publish() before register(). The objects go up on the shared session
   connection while the name is still unowned, so the moment the daemon grants
   the name, the object set behind it already answers. Name-owned then implies
   objects-published, and no window opens for a client to fall into. GDBus
   keeps a registration per object path and interface, so the application's
   own org.gtk.* interfaces and the API's interface share one path in either
   order.
3. register(), which fails open where that is safe. A launch with no usable
   session bus still boots, windowed, with the API and uniqueness degraded and
   logged, rather than die halfway through startup. It never decides that it
   is the primary while another process owns the name (see
   _registration_failed). A registration as a remote takes an answer from the
   owner, an owner that registered and does not yet dispatch its main loop
   gives none until it does, and past the bus timeout the honest outcome is to
   stop rather than open the decks the other instance holds.
4. The verdict. A remote hands off to the primary and exits. The primary sends
   a pre-rename instance off the Stream Deck before it opens any deck itself.

Why nothing here claims the name first

A claim on the application name up front, before the expensive work, settles a
race earlier, and this module does not take that shape. A connection that owns
the application name makes GApplication's own register() find the name taken,
and register() then demotes the process that holds it to a remote instance, so
a pre-claim defeats the mechanism it means to help. A claim on a different
name works, and that is the second owner of uniqueness this module replaced.
It meant two names to keep in step, and a winner that held one of them while
the other stayed unowned for a whole boot. What stays is an ask rather than a
claim. A NameHasOwner probe and a release poll own nothing, which leaves
register() as the one claim in the tree and its verdict as the only one.

What registering also does

register() emits the application's startup signal, so the toolkit's own
startup chain runs here, before the globals exist, which is the point of
registering first. The app overrides nothing on that path, and anything that
starts to override it must hold that constraint or move.

It also makes this process reachable before it answers. A launch that arrives
while this one still boots joins it as a remote, and that join takes an answer
that arrives once this process starts to dispatch its main loop. That launch
therefore waits out the boot, bounded by the measured 25s default of the bus,
and then presents the window, rather than fail early.

The upgrade window

A build that predates this ordering owns nothing until its main loop starts,
so a launch of this build that lands while such a build still boots sees an
unowned name and becomes the primary. Two primaries then run on one machine
for a moment. It needs a version change and a launch inside one boot window,
and the next start of either build ends it.

Why the flags are set before registering

g_application_set_flags asserts on an application that already registered. It
emits a CRITICAL and keeps the old flags, so NON_UNIQUE can only be set while
registration has not happened, or has failed. This module therefore probes the
bus itself up front, rather than catch an error out of register().
GApplication answers an unreachable session bus by proceeding as a non-unique
application, and it returns True and raises nothing.

Why this module is not on the floor import list

It imports gi at module level, and only main.py and the scenarios that drive
it consume it. The floor check covers a module that claims "any layer may
import this, on a bare interpreter". This module claims less, because it runs
in the one process that already loaded the toolkit, so compileall checks it
for 3.13 syntax like every other module, and nothing more. The API module
makes the same trade for the same reason.
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

    #: This process owns the application name, so it boots.
    PRIMARY = "primary"
    #: No usable session bus, so nothing owns anything. Boot degraded.
    PRIMARY_UNREGISTERED = "primary-unregistered"
    #: Another process owns the name. Hand off to it and exit.
    REMOTE = "remote"


class LaunchAborted(Exception):
    """This launch ends here, with nothing started and a non-zero exit.

    This raises rather than returns, because these are no kinds of launch.
    Every Decision value means "carry on, this way", and these mean "stop".
    The caller prints the message and exits.
    """


class CloseRunningFailed(LaunchAborted):
    """--close-running was asked for and the running instance is still there.

    A launch that was told to close what is running, and did not, must not
    report success, and must not boot a second instance next to the one it
    failed to close.
    """


class HandoffFailed(LaunchAborted):
    """Another instance owns the name and registration could not join it.

    A registration as a remote is no local operation. It needs an answer from
    the process that owns the name, and a mid-boot instance gives that answer
    once its main loop starts to dispatch. Past the bus timeout this launch
    cannot become the remote it is. A boot instead puts a second instance on
    the same decks, which costs more than a failure.
    """


class Application(Protocol):
    """What the gate needs from the application it decides for.

    A structural protocol, so a scenario drives establish() with a plain
    Gio.Application and a test-scoped id, which is the real registration
    machinery without a display and without the app's own construction.
    """

    def get_application_id(self) -> str | None: ...

    def register(self) -> bool: ...

    def get_is_remote(self) -> bool: ...

    def get_flags(self) -> Gio.ApplicationFlags: ...

    def set_flags(self, flags: Gio.ApplicationFlags, /) -> None: ...


def object_path_for(app_id: str) -> str:
    """The object path where an application with app_id exports its actions.

    GApplication derives the path from the id by this rule, and appinfo
    derives it the same way for the app's own id. A derivation here lets a
    test-scoped id bring its own path, rather than need a second table kept in
    step.
    """
    return "/" + app_id.replace(".", "/")


def name_has_owner(session_bus: Gio.DBusConnection, name: str) -> bool:
    """Does anything own name on the session bus right now?

    This asks the bus daemon, and passes NO_AUTO_START, which keeps the probe
    a probe. A call addressed to a well-known name activates it over D-Bus.
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

    Two shapes carry that meaning, a NoReply the bus hands back, and
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
    """Poll until nothing owns name. True when the owner released it inside
    the grace.

    This reads the monotonic clock and not the wall clock. The loop runs at
    login, which is when NTP steps the wall clock and collapses or stretches
    the grace.
    """
    deadline = time.monotonic() + grace_seconds
    while True:
        try:
            if not name_has_owner(session_bus, name):
                return True
        except GLib.Error as e:
            # A probe that cannot complete reads as "nobody home", like every
            # other failed probe here. A continue costs less than a refusal
            # over one bus error.
            log.debug(f"Could not probe {name}: {e}")
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RELEASE_POLL_SECONDS)


def _is_dispatching(session_bus: Gio.DBusConnection, app_id: str) -> bool:
    """Does the owner of app_id dispatch its main loop right now?

    This asks the owner's action group, and not the process. GDBus answers
    org.freedesktop.DBus.Peer.Ping, and Introspect, from its own worker thread,
    so an instance that registered and has not reached its main loop answers
    both at once, which measurement confirms, and neither answer says whether
    it is live. The same action group that receives a quit request dispatches
    DescribeAll, so an answer says more than "the process exists". It says
    that a quit sent now gets acted on rather than queued behind a boot.
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
    """Ask whatever owns app_id to quit, and wait for it to let go.

    Raises CloseRunningFailed when the instance is still there after the
    grace, and leaves it unasked when it never got far enough to hear the
    question.

    The order carries weight. An instance that still boots owns the name and
    dispatches nothing, so it refuses no quit sent at it. That quit waits in
    the incoming queue and gets acted on once that instance starts its main
    loop, which usually comes after this process gave up and exited. Nothing
    then runs at all, which costs more than either outcome the flag can
    produce. So this asks only once the target can hear, and it leaves a target
    that never answers alone and reports it.

    Each wait gets its own grace. An instance that took most of one grace to
    start dispatching must not then get a quit and a failure report a moment
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
        # Two kinds of instance answer nothing, and this code cannot tell
        # them apart. One still boots, and one is up with a blocked main loop.
        # The handling suits both, because an instance that cannot hear the
        # question must not die from it, so the message names both rather than
        # assert the one it cannot know.
        raise CloseRunningFailed(
            f"The running instance did not answer within "
            f"{CLOSE_GRACE_SECONDS:.0f}s -- it is still starting up, or it is "
            f"stuck; it was left running and nothing was started"
        )

    log.info("Closing running instance")
    try:
        activate_action(session_bus, app_id, object_path_for(app_id), "quit")
    except GLib.Error as e:
        if is_no_reply(e):
            # An instance that quits ends its process inside the handler, so
            # no reply comes. That reads as success here rather than a
            # failure, and it is why this branch does not end the launch. Only
            # the release poll below knows the outcome.
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

    A build from before the rename owns the old bus name, and this launch's
    own name says nothing about that name, so without this call it opens decks
    that the other instance holds. Probe with NameHasOwner, and never address
    the old name directly. A plain call to a well-known name activates it, and
    for the old id that starts an upstream install through its D-Bus service
    file, which is the race this call prevents. A NameHasOwner of False, the
    normal case, also ends this cost. Once nothing owns the old name, this
    costs one cheap round trip per launch.

    The primary alone runs this, and only after registration. A launch that
    hands off to another instance must send nothing away.
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
        # An instance that quits inside the handler never replies. That is
        # what success looks like here, and the reason to run the poll below.
        log.debug(f"The pre-rename instance did not answer the quit request: {e}")
    # Bounded poll for it to drop the name, rather than a flat sleep.
    _wait_for_release(session_bus, appinfo.OLD_APP_ID, CLOSE_GRACE_SECONDS)


def _registration_failed(app: Application, session_bus: Gio.DBusConnection | None,
                         app_id: str | None, error: GLib.Error) -> Decision:
    """Decide what a failed register() means, and never guess "primary".

    The case that fails here is a launch that becomes the remote of a running
    application. A join of the owner takes a round trip to it, and an owner
    that registered and has not reached its main loop answers nothing until it
    does. Past the bus's own timeout the launch has no options left, and it
    must not decide that it is the primary, because the instance it could not
    reach holds the decks.

    A failure while nothing owns the name is another case, such as a daemon
    that refuses the request. Nothing runs then, so a degraded boot beats no
    boot.
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

    This calls publish once, before registration. publish must contain its
    own failures, as the app's API service does, and nothing here reads its
    result. The caller passes close_running rather than let this module read
    the parsed arguments, so this module holds no process state.
    """
    session_bus = _session_bus()
    app_id = app.get_application_id()

    if close_running and session_bus is not None and app_id is not None:
        _close_running_instance(session_bus, app_id)

    if session_bus is None:
        # Record this before the registration, because afterwards nothing
        # can. An application registered without a bus reports itself as the
        # primary, and looks the same as one that owns the name.
        app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)

    # Publish the objects first. From the moment the daemon grants the name
    # below, everything behind it already answers a call.
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
        # The objects published above sit on this connection's unique name,
        # which nothing addresses. This process exits next and takes the whole
        # connection, and its objects, with it.
        log.info("Another instance owns the application name; handing off to it")
        return Decision.REMOTE

    log.info("This launch owns the application name")
    # The primary arm alone runs this. A launch that reached
    # PRIMARY_UNREGISTERED with a working bus, after a daemon refused the name
    # request, boots without a check for a pre-rename instance, and would then
    # share the decks with one.
    _shoo_pre_rename_instance(session_bus)
    return Decision.PRIMARY
