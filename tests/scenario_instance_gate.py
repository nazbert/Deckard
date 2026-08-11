"""
Pins the uniqueness gate: who owns the application name, when the objects
behind it appear, and what a launch that does not own it does instead.

Everything here runs against a REAL dbus-daemon of this scenario's own (the
owned-daemon harness from scenario_api_lifecycle_publish -- never
Gio.TestDBus, whose teardown waits out a 30s timeout for a shared connection
that has dispatched calls) and REAL Gio.Applications. Registration is the
mechanism under test, so nothing about it is stubbed; what is scoped down is
the identity, so every leg owns a name of its own and no leg can be answered
by the developer's running Deckard.

Contention needs several processes, not several objects: a second
GApplication in this interpreter shares the same bus connection, and the
daemon answers a request for a name that connection already owns with
ALREADY_OWNER -- a passing test that proved nothing. Every contending party is
therefore tests/instance_gate_child.py, one process each.

Legs:
  1. ORDERING -- publish() runs before register(): inside the publish callback
     the name is provably still unowned, and the moment it IS owned the
     published object answers an external client. This is the closure pin: it
     goes red if the two steps are swapped, because then the name is already
     owned when publish runs.
  2. RACE -- eight children reach establish() at one wall-clock instant, twice
     over. Exactly one is the primary each time; the rest are plain remotes. No
     second mechanism decides this: it is the daemon serializing one
     RequestName. Repeated because a check-then-act gate does not lose every
     round -- it loses often enough to be caught, not reliably enough for one.
  3. FORWARDING -- a remote child's activate() arrives at the primary's
     activate signal. This is the whole hand-off: the second launch presents
     the first one's window through the framework instead of a hand-rolled
     action forward.
  4. CLOSE-RUNNING -- against an instance that answers "quit" by ending its
     process, the gate observes the RELEASE (not a reply -- there is none) and
     then registers.
  5. ... against one that is still BOOTING: it owns the name and dispatches
     nothing, so the quit is held back until it can be acted on rather than
     queued behind the boot and delivered after this launch has given up.
  6. ... against one that never starts dispatching at all: the grace expires
     with the instance UNASKED and still running, which is the only honest
     outcome (the alternative kills it and leaves nothing).
  7. ... against one that dispatches but ignores "quit": it was asked, it did
     not go, and the launch fails rather than booting a second instance.
  8. FAIL-OPEN -- with no reachable bus at all, the launch still boots:
     PRIMARY_UNREGISTERED with NON_UNIQUE set, rather than dying at a bus it
     cannot have.
  9. REGISTRATION FAILURE -- the two arms of an error out of register():
     nobody owns the name, so boot degraded; somebody does, so stop.
 10. OLD-NAME GUARD -- a still-running pre-rename instance is asked to quit and
     waited for, on the primary path only, before this launch opens any deck.
 11. PARKED REQUESTS -- a launch that parked `--change-page` requests and then
     turned out to be the remote hands them to the instance that owns the name
     instead of exiting with them still in its own memory.

The fail-open leg runs in a child pointed at a dead bus address rather than by
killing the daemon this scenario owns: the legs share one daemon, and a leg
that tore it down would decide what the others could still do.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH before globals)

import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

from gi.repository import Gio, GLib  # noqa: E402

import globals as gl  # noqa: E402

import appinfo  # noqa: E402
import src.api as api  # noqa: E402
from cli_args import argparser  # noqa: E402
from src.backend import cli_forward, instance_gate  # noqa: E402

from scenario_api_lifecycle_publish import (  # noqa: E402
    Observer, _die_with_parent, pump, pump_until, start_private_bus,
    stop_private_bus,
)
# The running instance as a recorder, shared rather than copied: what leg 11
# needs is the object the forwarding rules are already pinned against.
from scenario_cli_forward_all import Recorder  # noqa: E402

WATCHDOG_SECONDS = 180

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "instance_gate_child.py")

# One name per leg: a leg that reused another's would inherit its owner, and a
# leg that used the app's own id could be answered by the developer's running
# Deckard if the daemon isolation ever broke.
ID_ORDERING = "io.github.nazbert.DeckardGateOrdering"
ID_RACE = "io.github.nazbert.DeckardGateRace"
ID_FORWARD = "io.github.nazbert.DeckardGateForward"
ID_CLOSE = "io.github.nazbert.DeckardGateClose"
ID_DEAF = "io.github.nazbert.DeckardGateDeaf"
ID_MIDBOOT = "io.github.nazbert.DeckardGateMidBoot"
ID_SILENT = "io.github.nazbert.DeckardGateSilent"
ID_FAILOPEN = "io.github.nazbert.DeckardGateFailOpen"
ID_BLOCKED = "io.github.nazbert.DeckardGateBlocked"
ID_OLDGUARD = "io.github.nazbert.DeckardGateOldGuard"
ID_PARKED = "io.github.nazbert.DeckardGateParked"

RACE_CONTENDERS = 8

# One command carrying both kinds, each naming its own deck, so a request lost
# on the way out cannot hide behind last-write-wins parking.
PARKED_ARGV = [
    "--change-page", "gate-deck-a", "Alpha",
    "--change-page", "gate-deck-b", "Beta",
    "--change-state", "gate-deck-c", "Gamma", "1,2", "3",
]
PARKED_FORWARDS = [
    ("page", "gate-deck-a", "Alpha"),
    ("page", "gate-deck-b", "Beta"),
    ("state", "gate-deck-c", "Gamma", "1,2", 3),
]


# ===================================================================== #
# Children
# ===================================================================== #

def spawn(mode: str, app_id: str, **env_extra) -> subprocess.Popen:
    env = dict(os.environ)
    env["DECKARD_GATE_MODE"] = mode
    env["DECKARD_GATE_APP_ID"] = app_id
    # Inside this scenario's own temp data dir, so a child that ends in
    # os._exit (the quit modes do, exactly as the app does) leaves nothing
    # behind that the harness does not already delete.
    env["DECKARD_GATE_DATA"] = gl.DATA_PATH
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.Popen(
        [sys.executable, CHILD], stdout=subprocess.PIPE, text=True,
        env=env, preexec_fn=_die_with_parent,
    )


def wait_for_line(proc: subprocess.Popen, expected: str,
                  timeout: float = 30.0) -> None:
    """Block until the child prints `expected` (it flushes every line)."""
    deadline = time.monotonic() + timeout
    while True:
        line = (proc.stdout.readline() or "").strip()
        if line == expected:
            return
        if not line and proc.poll() is not None:
            raise AssertionError(
                f"child exited (rc={proc.returncode}) before printing "
                f"{expected!r}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"child never printed {expected!r}")


def records(proc: subprocess.Popen, timeout: float = 60.0) -> dict[str, str]:
    """Wait for the child to exit and return everything it said, keyed by the
    first word of each line."""
    out, _ = proc.communicate(timeout=timeout)
    said = {}
    for line in out.splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            said[key] = value
    return said


def kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


# ===================================================================== #
# Bus helpers, deliberately not the gate's own
# ===================================================================== #

def has_owner(connection: Gio.DBusConnection, name: str) -> bool:
    """Ask the daemon directly, over the observing connection.

    Hand-rolled rather than instance_gate.name_has_owner so the assertions
    about the module do not run through the module.
    """
    return connection.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "NameHasOwner", GLib.Variant("(s)", (name,)), GLib.VariantType("(b)"),
        Gio.DBusCallFlags.NO_AUTO_START, 5000, None,
    ).unpack()[0]


def wait_for_owner(connection: Gio.DBusConnection, name: str,
                   timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not has_owner(connection, name):
        if time.monotonic() >= deadline:
            raise AssertionError(f"{name} was never owned")
        time.sleep(0.02)


# ===================================================================== #
# Legs
# ===================================================================== #

def leg_ordering(observer: Observer) -> None:
    """publish() before register(): the name never appears before the objects."""
    app = Gio.Application(application_id=ID_ORDERING)
    seen: list[bool] = []

    def publish():
        # The load-bearing assertion of the whole design: at the moment the
        # objects go up, nothing owns the name yet -- so no client can have
        # been told this instance exists and then find nothing behind it.
        seen.append(has_owner(observer.connection, ID_ORDERING))
        api.start_dbus_service()

    decision = instance_gate.establish(app, publish=publish, close_running=False)

    assert decision is instance_gate.Decision.PRIMARY, decision
    assert seen == [False], (
        f"the application name was ALREADY owned when publish() ran ({seen}): "
        f"registration happened first, which reopens the window where a "
        f"client sees an instance whose objects are not there yet"
    )
    assert has_owner(observer.connection, ID_ORDERING), (
        "the gate returned PRIMARY without the name being owned"
    )

    # And the other half of "name owned implies objects published": an external
    # client, addressing the well-known name the instant it exists, reaches the
    # object that went up before it.
    observer.destination = ID_ORDERING
    data_path = observer.get_property(api.DBUS_OBJECT_PATH, api.TOP_IFACE,
                                      "DataPath")
    assert data_path == gl.DATA_PATH, (
        f"the published object answered {data_path!r} instead of "
        f"{gl.DATA_PATH!r}"
    )
    api.stop_dbus_service()
    print("  PASS: objects are published before the name is owned")


def contend(app_id: str) -> list[str]:
    """One round of simultaneous launches; their verdicts.

    The barrier is inside the child, immediately before establish() and not
    around a pre-warmed connection: the same child code serves the busless leg,
    where there is no connection to warm and a warm-up that raised would change
    what that leg tests. Connecting and authenticating therefore lands inside
    the measured window, which spreads the contenders by a millisecond or two
    -- enough that a check-then-act gate sometimes gets away with it, which is
    why this runs more than once.
    """
    start_at = time.time() + 1.0
    children = [
        spawn("establish", app_id, DECKARD_GATE_START_AT=start_at,
              DECKARD_GATE_HOLD=2.0)
        for _ in range(RACE_CONTENDERS)
    ]
    try:
        return [records(child).get("VERDICT") for child in children]
    finally:
        for child in children:
            kill(child)


def leg_race(observer: Observer) -> None:
    """Simultaneous launches; exactly one of them is the instance, every time."""
    for round_number in (1, 2):
        app_id = f"{ID_RACE}{round_number}"
        verdicts = contend(app_id)

        primaries = [v for v in verdicts if v == "primary"]
        remotes = [v for v in verdicts if v == "remote"]
        assert len(primaries) == 1, (
            f"round {round_number}: {len(primaries)} of {RACE_CONTENDERS} "
            f"simultaneous launches think they are the instance ({verdicts}) "
            f"-- two of them would open the same decks"
        )
        assert len(remotes) == RACE_CONTENDERS - 1, verdicts
        assert not has_owner(observer.connection, app_id), (
            "the winner's name outlived its process"
        )
    print(f"  PASS: {RACE_CONTENDERS} simultaneous launches x2 rounds, exactly "
          f"one primary each")


def leg_forwarding(observer: Observer) -> None:
    """A remote's activate() lands on the primary's activate signal."""
    app = Gio.Application(application_id=ID_FORWARD)
    activations: list[float] = []
    app.connect("activate", lambda _app: activations.append(time.monotonic()))

    decision = instance_gate.establish(app, publish=lambda: None,
                                       close_running=False)
    assert decision is instance_gate.Decision.PRIMARY, decision
    assert not activations, "registering must not activate anything"

    child = spawn("activate", ID_FORWARD)
    try:
        pump_until(lambda: bool(activations), 30.0,
                   "the remote launch's activation never reached the primary")
        said = records(child)
    finally:
        kill(child)

    assert said.get("VERDICT") == "remote", said
    assert said.get("ACTIVATED") == "", said
    print("  PASS: a second launch presents the running instance through the "
          "framework")


def leg_close_running(observer: Observer) -> None:
    """--close-running against an instance that quits: release, then register."""
    child = spawn("primary-quit", ID_CLOSE)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_CLOSE)

        app = Gio.Application(application_id=ID_CLOSE)
        started = time.monotonic()
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=True)
        took = time.monotonic() - started

        assert decision is instance_gate.Decision.PRIMARY, decision
        said = records(child)
        assert said.get("QUIT-RECEIVED") == "", (
            f"the running instance was never asked to quit: {said}")
        assert child.returncode == 0, child.returncode
        assert took < instance_gate.CLOSE_GRACE_SECONDS, (
            f"waiting for the release took {took:.1f}s -- the poll is not "
            f"reacting to the release, it is waiting out its grace"
        )
    finally:
        kill(child)
    print(f"  PASS: --close-running closed the instance and took over "
          f"({took:.2f}s)")


def leg_close_running_mid_boot(observer: Observer) -> None:
    """--close-running against an instance that is still starting up.

    It owns the name and dispatches nothing, exactly as a booting instance
    does. The quit must be held back until it can be heard: sent into that
    window it would sit in the queue, be acted on when the loop finally
    started, and kill the instance a moment after this launch had already
    given up -- leaving nothing running at all.
    """
    dispatch_delay = 2.0
    child = spawn("primary-quit", ID_MIDBOOT,
                  DECKARD_GATE_DISPATCH_DELAY=dispatch_delay)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_MIDBOOT)

        app = Gio.Application(application_id=ID_MIDBOOT)
        started = time.monotonic()
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=True)
        took = time.monotonic() - started

        assert decision is instance_gate.Decision.PRIMARY, decision
        said = records(child)
        assert said.get("QUIT-RECEIVED") == "", (
            f"the instance was never asked once it could answer: {said}")
        assert child.returncode == 0, child.returncode
        assert took >= dispatch_delay, (
            f"the launch took over after {took:.2f}s, before the instance was "
            f"dispatching ({dispatch_delay}s) -- the quit went into its queue, "
            f"not to a loop that could act on it"
        )
    finally:
        kill(child)
    print(f"  PASS: --close-running waits for a booting instance to answer, "
          f"then closes it ({took:.2f}s)")


def leg_close_running_never_answers(observer: Observer) -> None:
    """An instance that never starts dispatching is left alone, not killed.

    "Never asked" is asserted at the WIRE, not at the action handler. The
    handler runs on a main context this child never reaches, so a child that
    was sent a quit and a child that was not print exactly the same nothing --
    an assertion on that is true by construction and would survive the quit
    being sent again. The child's connection filter runs on the GDBus worker
    thread instead and reports what ARRIVED, dispatched or not.
    """
    child = spawn("primary-quit", ID_SILENT, DECKARD_GATE_DISPATCH_DELAY=300)
    grace = instance_gate.CLOSE_GRACE_SECONDS
    try:
        wait_for_line(child, "READY")
        app = Gio.Application(application_id=ID_SILENT)
        instance_gate.CLOSE_GRACE_SECONDS = 1.5
        started = time.monotonic()
        try:
            instance_gate.establish(app, publish=lambda: None,
                                    close_running=True)
        except instance_gate.CloseRunningFailed as e:
            took = time.monotonic() - started
            assert "still starting up" in str(e), e
            assert "stuck" in str(e), (
                f"the message must name the wedged case it cannot rule out: {e}")
            assert "left running" in str(e), (
                f"the message must not read as though the quit was sent: {e}")
        else:
            raise AssertionError(
                "--close-running took over from an instance it never managed "
                "to speak to"
            )
        assert 1.5 <= took < 6.0, f"the grace was not honoured ({took:.1f}s)"
        assert child.poll() is None, (
            "the instance was killed by a quit it could not answer -- the "
            "outcome this ordering exists to prevent"
        )
        assert has_owner(observer.connection, ID_SILENT), (
            "the instance lost its name, so it did not survive this leg")
    finally:
        instance_gate.CLOSE_GRACE_SECONDS = grace
        kill(child)
    said = records(child)
    assert "WIRE-ACTIVATE" not in said, (
        f"a quit reached the instance at the wire: it would have sat in the "
        f"queue and killed it whenever its loop finally started, a moment "
        f"after this launch had already given up -- {said}"
    )
    assert "QUIT-RECEIVED" not in said, said
    print(f"  PASS: an instance that never answers is left running, unasked "
          f"(nothing on the wire, {took:.2f}s)")


def leg_close_running_refused(observer: Observer) -> None:
    """--close-running against an instance that ignores it: fail, don't boot."""
    child = spawn("primary-deaf", ID_DEAF)
    grace = instance_gate.CLOSE_GRACE_SECONDS
    try:
        wait_for_line(child, "READY")
        app = Gio.Application(application_id=ID_DEAF)

        # Shortened for the run, having pinned the shipped value above: what is
        # under test is that the wait is BOUNDED and ends in a failure, not how
        # many seconds it is.
        instance_gate.CLOSE_GRACE_SECONDS = 1.5
        started = time.monotonic()
        try:
            instance_gate.establish(app, publish=lambda: None,
                                    close_running=True)
        except instance_gate.CloseRunningFailed as e:
            took = time.monotonic() - started
            # This one WAS asked -- it dispatches, it just has no such action.
            # The message has to say so, or the two failures read alike.
            assert "asked to quit" in str(e), e
            assert "had not exited" in str(e), e
        else:
            raise AssertionError(
                "--close-running reported success against an instance that is "
                "still running (and this launch would now boot a second one)"
            )
        assert 1.5 <= took < 6.0, f"the grace was not honoured ({took:.1f}s)"
        assert has_owner(observer.connection, ID_DEAF), (
            "the deaf instance lost its name, so this leg proved nothing")
    finally:
        instance_gate.CLOSE_GRACE_SECONDS = grace
        kill(child)
    said = records(child)
    # This instance WAS asked, and the wire says so. Which is also what makes
    # the leg above's silence mean anything: the same witness, watching the
    # same thing, reports an arrival here.
    assert said.get("WIRE-ACTIVATE") == "quit", (
        f"the quit never reached this instance, so it did not refuse anything "
        f"-- and the witness the unasked leg trusts reports nothing: {said}"
    )
    print(f"  PASS: --close-running that cannot close fails after its grace "
          f"(asked at the wire, {took:.2f}s)")


def leg_fail_open() -> None:
    """No bus at all: boot anyway, degraded and honest about it."""
    child = spawn("establish", ID_FAILOPEN,
                  DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent/deckard-gate")
    try:
        said = records(child)
    finally:
        kill(child)
    assert said.get("VERDICT") == "primary-unregistered", (
        f"a launch with no session bus must still boot: {said}")
    assert said.get("NON_UNIQUE") == "True", (
        f"the application was left claiming to be unique with no bus to be "
        f"unique on: {said}")
    assert said.get("PUBLISHED") == "1", (
        f"the API publish was skipped on the busless path: {said}")
    print("  PASS: a launch with no session bus boots non-unique")


def leg_old_name_guard(observer: Observer) -> None:
    """The pre-rename instance is shooed off the deck before this one boots."""
    # Answers the quit and then takes a moment to exit, so the release is
    # something the guard has to WAIT for -- an instance that vanished inside
    # the handler would let a guard with no poll at all pass this leg.
    teardown = 0.6
    child = spawn("old-name", ID_OLDGUARD, DECKARD_GATE_QUIT_DELAY=teardown)
    try:
        wait_for_line(child, "READY")
        # Also proof that this scenario is talking to its own daemon: the old
        # name is owned HERE, on the bus the gate is about to probe.
        assert has_owner(observer.connection, appinfo.OLD_APP_ID), (
            "the stand-in never took the pre-rename name")

        app = Gio.Application(application_id=ID_OLDGUARD)
        started = time.monotonic()
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=False)
        took = time.monotonic() - started

        assert decision is instance_gate.Decision.PRIMARY, decision
        said = records(child)
        assert said.get("QUIT-RECEIVED") == "", (
            f"the pre-rename instance was never asked to quit: {said}")
        assert not has_owner(observer.connection, appinfo.OLD_APP_ID), (
            "the gate returned before the pre-rename instance let go, so this "
            "launch would open decks that instance still holds"
        )
        assert teardown <= took < instance_gate.CLOSE_GRACE_SECONDS, (
            f"the guard returned after {took:.2f}s: it must wait for the "
            f"release (>= {teardown}s here) and must not sleep out its bound "
            f"({instance_gate.CLOSE_GRACE_SECONDS}s)"
        )
    finally:
        kill(child)
    print(f"  PASS: a pre-rename instance is quit and waited for ({took:.2f}s)")


def leg_parked_requests_follow_the_handoff(observer: Observer) -> None:
    """A launch that parked its requests and then lost the race hands them on.

    The two decisions happen in this order and cannot happen in the other: an
    invocation parks BEFORE it can know whether it will be the instance,
    because parked requests exist for the boot that would follow. So a
    `--change-page` invocation can find the name unowned, park, get as far as
    registering, and only there be told it is a remote -- holding requests in a
    process that is about to exit having opened nothing.

    They used to leave with it. No error, nothing applied, exit 0: the shape a
    real deck found by firing a page change at the same moment as a launch.

    The instance is a child that owns the name and dispatches, so the verdict
    below is a real registration against a real owner rather than an arranged
    one -- and the requests are parked by the production path, not written into
    the parking by hand.
    """
    child = spawn("primary-deaf", ID_PARKED)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_PARKED)

        parked = cli_forward.forward_cli_requests(
            argparser.parse_args(PARKED_ARGV), Recorder(running=False))
        assert not parked.handled and not parked.failures, parked
        assert gl.api_page_requests and gl.api_state_requests, (
            "nothing was parked, so the loss this leg is about cannot happen")

        app = Gio.Application(application_id=ID_PARKED)
        decision = instance_gate.establish(app, publish=lambda: None,
                                           close_running=False)
        assert decision is instance_gate.Decision.REMOTE, decision

        refusal = "Page 'Beta' not found. Available pages: Alpha"
        instance = Recorder(running=True, answers={"gate-deck-b": refusal})
        failures = cli_forward.forward_parked_requests(instance)

        assert instance.forwards() == PARKED_FORWARDS, (
            f"the requests this launch parked never reached the instance that "
            f"owns the name -- they die here, silently, when nothing sends "
            f"them: {instance.forwards()}"
        )
        assert failures == [refusal], (
            f"what the instance says about a request has to come back to the "
            f"terminal that typed it, on this path as on any other: {failures}"
        )
        assert not gl.api_page_requests and not gl.api_state_requests, (
            f"the parking still holds requests that have been handed over: "
            f"{gl.api_page_requests} / {gl.api_state_requests}"
        )
    finally:
        kill(child)
        gl.api_page_requests.clear()
        gl.api_state_requests.clear()
    print("  PASS: requests parked by the launch that lost the race are handed "
          "to the one that won")


class RefusingApp:
    """An application whose registration fails, for the two arms below.

    A stub because the failure it stands for is GIO's to produce and takes 25
    seconds of real time to reach (measured: a registered primary that has not
    started dispatching answers a joining remote's registration only when it
    does, and the bus gives up at its default timeout). What is under test is
    the decision taken on that error, which is this module's.
    """

    def __init__(self, app_id: str):
        self._app_id = app_id
        self.flags = Gio.ApplicationFlags.DEFAULT_FLAGS

    def get_application_id(self):
        return self._app_id

    def register(self):
        raise GLib.Error.new_literal(Gio.io_error_quark(),
                                     "Timeout was reached",
                                     int(Gio.IOErrorEnum.TIMED_OUT))

    def get_is_remote(self):
        raise AssertionError("a registration that failed has no verdict to give")

    def get_flags(self):
        return self.flags

    def set_flags(self, flags):
        self.flags = flags


def leg_registration_failure_arms(observer: Observer) -> None:
    """A registration that fails means opposite things depending on the name."""
    child = spawn("primary-quit", ID_BLOCKED)
    try:
        wait_for_line(child, "READY")
        assert has_owner(observer.connection, ID_BLOCKED)

        # The name is owned: whatever went wrong, this launch is the SECOND
        # one. Booting would put it on the decks the first one holds.
        blocked = RefusingApp(ID_BLOCKED)
        try:
            instance_gate.establish(blocked, publish=lambda: None,
                                    close_running=False)
        except instance_gate.HandoffFailed as e:
            assert "did not answer" in str(e), e
        else:
            raise AssertionError(
                "a launch that could not join the running instance decided it "
                "was the instance instead"
            )
        assert not (blocked.flags & Gio.ApplicationFlags.NON_UNIQUE), (
            "the second launch was quietly made non-unique, which is the "
            "double-instance this gate exists to prevent"
        )
    finally:
        kill(child)

    # Nobody owns the name: nothing is running, so a failure here is about this
    # process alone and booting degraded beats not booting.
    lonely = RefusingApp("io.github.nazbert.DeckardGateNobody")
    decision = instance_gate.establish(lonely, publish=lambda: None,
                                       close_running=False)
    assert decision is instance_gate.Decision.PRIMARY_UNREGISTERED, decision
    assert lonely.flags & Gio.ApplicationFlags.NON_UNIQUE, lonely.flags
    print("  PASS: a failed registration boots only when nothing owns the name")


def run_legs(bus_address: str) -> None:
    observer = Observer(bus_address, ID_ORDERING)
    try:
        assert instance_gate.CLOSE_GRACE_SECONDS == 10.0, (
            "the shipped grace changed; every timing assertion below is "
            "written against 10s"
        )
        leg_ordering(observer)
        leg_race(observer)
        leg_forwarding(observer)
        leg_close_running(observer)
        leg_close_running_mid_boot(observer)
        leg_close_running_never_answers(observer)
        leg_close_running_refused(observer)
        leg_fail_open()
        leg_registration_failure_arms(observer)
        leg_old_name_guard(observer)
        leg_parked_requests_follow_the_handoff(observer)
    finally:
        api.stop_dbus_service()
        observer.connection.close_sync(None)
        pump(0.1)


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, "scenario_instance_gate")

    bus_proc, bus_address = start_private_bus()
    assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == bus_address, (
        "this scenario is not pointed at its own daemon, and everything it "
        "does -- including asking a pre-rename instance to quit -- would go to "
        "the developer's real session bus"
    )
    try:
        run_legs(bus_address)
    finally:
        stop_private_bus(bus_proc)

    print("PASS: scenario_instance_gate")


if __name__ == "__main__":
    main()
