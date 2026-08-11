"""
Integration scenario: a DeckController whose __init__ fails after it has
started its threads must leave none of them behind.

DeckController.__init__ starts the media writer, then builds the action and
load pools and the ticker, then reads deck settings and loads the boot page.
Everything after the writer starts is fallible -- the boot-storm flaky serial
read, a settings read, the page load -- and until this scenario's fix the
exception simply escaped: `_init_deck_controller_with_retry` never receives the
instance, so nobody holds the writer, the ticker or the pools, and a deck that
fails three rounds of retries strands three sets of them for the life of the
process.

Four legs, each driven through the REAL retry helper
(`DeckManager._init_deck_controller_with_retry`, called unbound over the
harness's StubDeckManager) so the production arms decide the outcome:

  (a) deep flake -- the serial read inside load_default_page fails on every
      attempt (the transport arm: close, reopen, retry, give up). The live
      thread census must come back to its pre-attempt shape after the whole
      sequence, and no media writer may outlive its attempt.
  (b) shallow flake -- the same failure one window earlier, before the ticker
      and the pools exist, so the teardown runs against a barely-started
      controller.
  (c) a retry that succeeds on round 2 still registers a working controller:
      round 1's teardown must not poison round 2.
  (d) a NON-transport failure takes the retry helper's other arm (log and
      return None, no retry) and must tear down just as thoroughly.
  (e) the other side of the constructor's own split: a non-transport failure
      raised INSIDE load_default_page is a page problem, not a device one, so
      the deck must still register. Red if that arm is ever made fatal.
  (f) the hostile shape: a deck whose writes block forever instead of raising.
      Giving up on it must stay bounded, because on the boot path the thread
      the constructor runs on is the process's main thread -- there is no
      window and no quit path yet to unwedge it with.

Legs (a) and (b) are red before the fix -- (a) strands a writer, a ticker and
the action-pool workers per abandoned attempt, (b) a writer per attempt.

The fault injection is anchored on the media writer actually running rather
than on a call index: FlakySerialDeck flakes its serial read only once a
MediaPlayerThread started by THIS attempt is alive, which is precisely the
window the guard covers. If the constructor ever stops reaching that window,
no flake is raised at all, the helper returns a controller, and the legs fail
loudly instead of quietly testing nothing.
"""
import collections
import re
import threading
import time

import fixtures  # must be first: isolates DATA_PATH before `import globals`
import globals as gl

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

from src.backend.DeckManagement.DeckManager import DeckManager

# Thread names the legs care about, i.e. the ones a DeckController starts.
CONTROLLER_THREAD_PREFIXES = ("MediaPlayerThread", "tick_actions", "action_cb", "load_")


class FlakySerialDeck(FaultyFakeDeck):
    """A deck whose serial read flakes, scriptable by construction window.

    `open()` is called once per construction attempt, before any serial read,
    so it doubles as the per-attempt reset: it snapshots the threads that
    predate the attempt, which is what makes "a writer started by THIS
    attempt" distinguishable from an orphan left by an earlier one.

    Knobs:
      * `pre_writer_flakes_from` -- 1-based index of the pre-writer serial read
        at which to start flaking. The read that memoizes the serial sits in
        that stretch, and failing it is what pushes the next read (the load
        pool's name) past the writer's start with nothing memoized.
      * `post_writer_grace` -- how many serial reads to let through once the
        writer is running before flaking. 0 fails at the deck-settings read
        just after the ticker starts; 1 lets that one through and fails inside
        load_default_page instead.
      * `attempts_to_fail` -- flake only during the first N attempts (None:
        every attempt).
      * `exc` -- the exception class to raise; TransportError picks the retry
        arm, anything else picks the log-and-give-up arm.
    """

    def __init__(self, *args, exc=TransportError, pre_writer_flakes_from=None,
                 post_writer_grace=0, attempts_to_fail=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._exc = exc
        self._pre_writer_flakes_from = pre_writer_flakes_from
        self._post_writer_grace = post_writer_grace
        self._attempts_to_fail = attempts_to_fail
        self._pre_attempt_idents: set[int] = set()
        self._attempt = 0
        self._pre_writer_reads = 0
        self._post_writer_reads = 0
        # One entry per flake: the controller-owned threads that were alive
        # when it was raised. Lets a leg assert WHICH window it hit.
        self.flake_contexts: list[set[str]] = []

    def open(self, *args, **kwargs):
        self._attempt += 1
        self._pre_attempt_idents = {t.ident for t in threading.enumerate()}
        self._pre_writer_reads = 0
        self._post_writer_reads = 0
        return super().open(*args, **kwargs)

    def _live_controller_threads(self) -> set[str]:
        """Controller threads started by THIS attempt -- anything older is an
        orphan from an earlier attempt (or an earlier leg) and would make the
        window assertions below read the wrong construction."""
        return {t.name for t in threading.enumerate()
                if t.name.startswith(CONTROLLER_THREAD_PREFIXES)
                and t.ident not in self._pre_attempt_idents}

    def _this_attempts_writer_is_running(self) -> bool:
        return any(t.name.startswith("MediaPlayerThread")
                   and t.ident not in self._pre_attempt_idents
                   for t in threading.enumerate())

    def _flake(self):
        self.flake_contexts.append(self._live_controller_threads())
        raise self._exc("FaultyFakeDeck: injected flaky serial read")

    def get_serial_number(self):
        attempt_flakes = (self._attempts_to_fail is None
                          or self._attempt <= self._attempts_to_fail)
        if attempt_flakes and self._this_attempts_writer_is_running():
            self._post_writer_reads += 1
            if self._post_writer_reads > self._post_writer_grace:
                self._flake()
        elif attempt_flakes and self._pre_writer_flakes_from is not None:
            self._pre_writer_reads += 1
            if self._pre_writer_reads >= self._pre_writer_flakes_from:
                self._flake()
        return super().get_serial_number()

    @property
    def flakes(self) -> int:
        return len(self.flake_contexts)

    def stop_flaking(self) -> None:
        """No more injected failures, so a controller that DID register can be
        torn down through the ordinary path."""
        self._attempts_to_fail = 0


class WedgedTransportDeck(FlakySerialDeck):
    """A deck whose writes block forever instead of raising, from the moment the
    constructor's failure is raised. This is what separates a bounded teardown
    from a hung one: a write that never returns holds the wrapper's device lock,
    so anything the teardown does that writes to the deck -- or that waits on
    that lock -- never comes back."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.block_writes = False
        self.release = threading.Event()

    def _flake(self):
        self.block_writes = True
        super()._flake()

    def set_key_image(self, key, image):
        if self.block_writes:
            self.release.wait()
        super().set_key_image(key, image)


def census() -> collections.Counter:
    """Live threads, with pool-worker suffixes folded away (`action_cb_3` and
    `action_cb_7` are one kind, and their count varies with pool warm-up)."""
    return collections.Counter(re.sub(r"_\d+$", "", t.name) for t in threading.enumerate())


def census_delta(before: collections.Counter) -> dict:
    now = census()
    now.subtract(before)
    return {name: n for name, n in now.items() if n}


def assert_census_returns(before: collections.Counter, label: str) -> None:
    settled = fixtures.wait_until(lambda: not census_delta(before), timeout=10)
    assert settled, (
        f"{label}: threads outlived their failed deck init -- {census_delta(before)}. "
        "A constructor that raises after starting the writer must release it."
    )
    stale = [t.name for t in threading.enumerate() if t.name.startswith("MediaPlayerThread")]
    assert not stale, f"{label}: a media writer outlived its attempt: {stale}"


def init_with_retry(deck, attempts: int = 3):
    """The production retry helper, called unbound over the harness's
    StubDeckManager -- both of its arms decide the outcome for real."""
    return DeckManager._init_deck_controller_with_retry(
        gl.deck_manager, deck, attempts=attempts, retry_delay=0.05)


def test_deep_flake_releases_every_thread() -> None:
    before = census()
    deck = FlakySerialDeck(serial_number="init-teardown-deep", deck_type="Fake Deck",
                           post_writer_grace=1)
    controller = init_with_retry(deck)

    assert controller is None, "a deck that flakes every attempt must not register"
    assert deck.flakes >= 3, (
        f"expected one flake per attempt, got {deck.flakes} -- the constructor never "
        "reached the window this leg exists to cover"
    )
    assert any("tick_actions" in ctx for ctx in deck.flake_contexts), (
        "no attempt failed with the ticker running; this leg is meant to fail in the "
        f"page-load window, contexts were {deck.flake_contexts}"
    )
    assert_census_returns(before, "deep flake")
    print("PASS: a transport flake in the guarded tail leaves no thread behind")


def test_shallow_flake_before_the_ticker() -> None:
    before = census()
    # Flaking from the third pre-writer read kills the one that memoizes the
    # serial, so the load pool's name re-reads it -- after the writer started,
    # before the ticker and the pools exist.
    deck = FlakySerialDeck(serial_number="init-teardown-shallow", deck_type="Fake Deck",
                           pre_writer_flakes_from=3)
    controller = init_with_retry(deck)

    assert controller is None, "a deck that flakes every attempt must not register"
    assert deck.flakes >= 3, f"expected at least one flake per attempt, got {deck.flakes}"
    post_writer = [ctx for ctx in deck.flake_contexts
                   if any(name.startswith("MediaPlayerThread") for name in ctx)]
    assert post_writer, f"no flake landed past the writer's start: {deck.flake_contexts}"
    assert all("tick_actions" not in ctx for ctx in post_writer), (
        "this leg is meant to fail before the ticker starts, so the teardown runs "
        f"against a barely-started controller; contexts were {post_writer}"
    )
    assert_census_returns(before, "shallow flake")
    print("PASS: the teardown tolerates a controller with no ticker or pools yet")


def test_successful_retry_round_registers_a_working_controller() -> None:
    before = census()
    deck = FlakySerialDeck(serial_number="init-teardown-retry", deck_type="Fake Deck",
                           post_writer_grace=1, attempts_to_fail=1)
    controller = init_with_retry(deck)

    assert controller is not None, "round 2 must register the deck -- round 1's teardown poisoned it"
    assert deck.flakes >= 1, "round 1 was supposed to fail"
    assert controller.serial_number() == "init-teardown-retry"
    assert controller.media_player.is_alive(), "the surviving controller has no writer"
    assert fixtures.wait_until(lambda: controller.active_page is not None, timeout=5), (
        "the surviving controller never loaded its boot page")
    assert deck.ops_by_name("set_key_image"), "the surviving controller never wrote to its deck"

    gl.deck_manager.deck_controller.append(controller)
    fixtures.teardown(controller)
    assert_census_returns(before, "successful retry")
    print("PASS: a failed round 1 does not poison the round that succeeds")


def test_generic_exception_arm_also_tears_down() -> None:
    before = census()
    # RuntimeError from the deck-settings read: outside load_default_page, so
    # it is not the boot-page failure the constructor deliberately swallows,
    # and not a transport error either -- the helper logs it and gives up.
    deck = FlakySerialDeck(serial_number="init-teardown-generic", deck_type="Fake Deck",
                           exc=RuntimeError, post_writer_grace=0)
    controller = init_with_retry(deck)

    assert controller is None, "a non-transport failure must not register the deck"
    assert deck.flakes == 1, (
        f"the non-transport arm must not retry, but the deck flaked {deck.flakes} times")
    assert_census_returns(before, "generic exception")
    print("PASS: the give-up arm tears down as thoroughly as the retry arm")


def test_a_boot_page_failure_still_registers_the_deck() -> None:
    before = census()
    # A RuntimeError raised INSIDE load_default_page: the constructor's other
    # arm. The page it wanted is still on disk and the next load retries it, so
    # the deck registers -- the guard must not carry the registration off with
    # the failure.
    deck = FlakySerialDeck(serial_number="init-teardown-register", deck_type="Fake Deck",
                           exc=RuntimeError, post_writer_grace=1)
    controller = init_with_retry(deck)

    assert controller is not None, (
        "a boot page that fails is not a device failure -- the deck must still register")
    assert deck.flakes >= 1, "the boot page load was supposed to fail"
    assert controller.media_player.is_alive(), "the registered controller has no writer"
    deck.stop_flaking()

    gl.deck_manager.deck_controller.append(controller)
    fixtures.teardown(controller)
    assert_census_returns(before, "boot page failure")
    print("PASS: a boot page that fails still leaves the deck registered")


def test_a_wedged_transport_cannot_wedge_the_constructor() -> None:
    # Last, deliberately: the writer this leg strands never returns until the
    # release below, so there is no census to come back to in between.
    deck = WedgedTransportDeck(serial_number="init-teardown-wedged", deck_type="Fake Deck",
                               post_writer_grace=1)
    start = time.monotonic()
    controller = init_with_retry(deck, attempts=1)
    elapsed = time.monotonic() - start
    deck.release.set()

    assert controller is None, "a deck whose writes never land must not register"
    assert elapsed < 10.0, (
        f"giving up on a deck whose writes block forever took {elapsed:.1f}s. On the boot "
        "path this runs on the process's main thread, before there is a window or a quit "
        "path -- the teardown must never write to the deck or wait on its lock"
    )
    print(f"PASS: a wedged transport cannot wedge the constructor ({elapsed:.2f}s)")


def main() -> None:
    # A teardown step that blocks (a write to a deck whose transport has already
    # failed, a join on a wedged ticker) must fail loud here rather than park
    # until run_all.py's per-scenario timeout.
    fixtures.start_watchdog(75, label="scenario_deck_init_teardown")

    # One successful controller first: it warms every lazily-started global
    # thread (the cache budget, the timer wheel, the shared background pool)
    # so the legs' census deltas contain deck threads and nothing else.
    warm = fixtures.make_headless_controller(serial="init-teardown-warm")
    fixtures.wait_until(lambda: warm.active_page is not None, timeout=5)
    fixtures.teardown(warm)

    test_deep_flake_releases_every_thread()
    test_shallow_flake_before_the_ticker()
    test_successful_retry_round_registers_a_working_controller()
    test_generic_exception_arm_also_tears_down()
    test_a_boot_page_failure_still_registers_the_deck()
    test_a_wedged_transport_cannot_wedge_the_constructor()
    print("PASS: scenario_deck_init_teardown")


if __name__ == "__main__":
    main()
