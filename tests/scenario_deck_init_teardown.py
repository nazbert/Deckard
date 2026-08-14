"""A DeckController whose __init__ fails must leave none of its threads behind.

Every leg runs through the real DeckManager._init_deck_controller_with_retry,
so the production arms decide the outcome and the thread census must return.
"""
import collections
import re
import threading
import time

import fixtures  # must be first; isolates DATA_PATH before import globals
import globals as gl

from StreamDeck.Transport.Transport import TransportError

from faulty_fake_deck import FaultyFakeDeck

from src.backend.DeckManagement.DeckManager import DeckManager

# Thread names the legs care about, i.e. the ones a DeckController starts.
CONTROLLER_THREAD_PREFIXES = ("MediaPlayerThread", "tick_actions", "action_cb", "load_")


class FlakySerialDeck(FaultyFakeDeck):
    """A deck whose serial read flakes, scriptable by construction window.

    open() runs once per construction attempt, before any serial read, so it
    doubles as the per-attempt reset and snapshots the threads that predate the
    attempt. The four constructor knobs pick the window and the retry arm.
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
        # One entry per flake, holding the controller-owned threads alive when
        # it was raised. A leg can then assert which window it hit.
        self.flake_contexts: list[set[str]] = []

    def open(self, *args, **kwargs):
        self._attempt += 1
        self._pre_attempt_idents = {t.ident for t in threading.enumerate()}
        self._pre_writer_reads = 0
        self._post_writer_reads = 0
        return super().open(*args, **kwargs)

    def _live_controller_threads(self) -> set[str]:
        """Controller threads started by this attempt.

        Anything older is an orphan from an earlier attempt or leg, and would
        make the window assertions below read the wrong construction.
        """
        return {t.name for t in threading.enumerate()
                if t.name.startswith(CONTROLLER_THREAD_PREFIXES)
                and t.ident not in self._pre_attempt_idents}

    def _writer_running_this_attempt(self) -> bool:
        return any(t.name.startswith("MediaPlayerThread")
                   and t.ident not in self._pre_attempt_idents
                   for t in threading.enumerate())

    def _flake(self):
        self.flake_contexts.append(self._live_controller_threads())
        raise self._exc("FaultyFakeDeck: injected flaky serial read")

    def get_serial_number(self):
        attempt_flakes = (self._attempts_to_fail is None
                          or self._attempt <= self._attempts_to_fail)
        if attempt_flakes and self._writer_running_this_attempt():
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
        """Stop injecting failures.

        A controller that did register can then tear down the ordinary way.
        """
        self._attempts_to_fail = 0


class WedgedTransportDeck(FlakySerialDeck):
    """A deck whose writes block forever instead of raising.

    A write that never returns holds the device lock of the wrapper, so
    anything the teardown writes, or waits on that lock for, never comes back.
    This separates a bounded teardown from a hung one.
    """

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
    """Live threads, with the pool-worker suffixes folded away.

    action_cb_3 and action_cb_7 are one kind, and their count varies with pool
    warm-up.
    """
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
    """The production retry helper, called unbound over the StubDeckManager.

    Both of its arms decide the outcome for real.
    """
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


def test_shallow_flake_before_ticker() -> None:
    before = census()
    # Flaking from the third pre-writer read kills the one that memoizes the
    # serial, so the load pool name re-reads it after the writer started and
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


def test_retry_round_registers_controller() -> None:
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


def test_generic_exception_tears_down() -> None:
    before = census()
    # A RuntimeError from the deck-settings read sits outside load_default_page,
    # so it is neither the boot-page failure the constructor swallows nor a
    # transport error. The helper logs it and gives up.
    deck = FlakySerialDeck(serial_number="init-teardown-generic", deck_type="Fake Deck",
                           exc=RuntimeError, post_writer_grace=0)
    controller = init_with_retry(deck)

    assert controller is None, "a non-transport failure must not register the deck"
    assert deck.flakes == 1, (
        f"the non-transport arm must not retry, but the deck flaked {deck.flakes} times")
    assert_census_returns(before, "generic exception")
    print("PASS: the give-up arm tears down as thoroughly as the retry arm")


def test_boot_page_failure_registers_deck() -> None:
    before = census()
    # A RuntimeError raised inside load_default_page takes the other arm of the
    # constructor. The page it wanted is still on disk and the next load retries
    # it, so the deck registers. The guard must not carry the registration off
    # with the failure.
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


def test_wedged_transport_spares_constructor() -> None:
    # Run last. The writer this leg strands never returns until the release
    # below, so there is no census to come back to in between.
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
    # A teardown step that blocks, such as a write to a failed transport or a
    # join on a wedged ticker, must fail loud here rather than park until the
    # per-scenario timeout of run_all.py.
    fixtures.start_watchdog(75, label="scenario_deck_init_teardown")

    # One successful controller first. It warms every lazily started global
    # thread, the cache budget, the timer wheel and the shared background pool,
    # so the census deltas of the legs contain deck threads and nothing else.
    warm = fixtures.make_headless_controller(serial="init-teardown-warm")
    fixtures.wait_until(lambda: warm.active_page is not None, timeout=5)
    fixtures.teardown(warm)

    test_deep_flake_releases_every_thread()
    test_shallow_flake_before_ticker()
    test_retry_round_registers_controller()
    test_generic_exception_tears_down()
    test_boot_page_failure_registers_deck()
    test_wedged_transport_spares_constructor()
    print("PASS: scenario_deck_init_teardown")


if __name__ == "__main__":
    main()
