"""
Unit-tier scenario for the FIFO transport lock
(issue #164, src/backend/DeckManagement/fair_lock.py).

FairLock replaces the stock threading.Lock the Stream Deck transport uses as
its per-device mutex. The stock lock is unfair: a writer that releases and
immediately re-acquires can out-race the library's HID read poll for many
cycles, which is what made dial input arrive coalesced under a video-write
burst. This is the regression net for the ordering guarantee that replaces
the write-rate cap that used to paper over it.

Covers:
  (a) service order equals acquisition order for N queued threads.
  (b) a hot acquire/release loop cannot overtake a waiter more than the one
      acquisition already in flight -- asserted as an overtake COUNT (a
      logical invariant) plus a wall-clock ceiling on the queued wait. Both
      are sampled from the instant the waiter's TICKET IS DRAWN, which is
      when it joins the order the lock promises to keep (see
      _TicketDrawProbe). A SECOND, looser ceiling is measured from the
      acquire() CALL, so the end-to-end wait a caller actually experiences
      stays covered: queue entry is not ordered by the ticket (see below),
      and a lock that starved the HID poll before it could draw would
      otherwise satisfy every draw-relative bound in this check.
      NOTE: with a single waiter, (b) cannot tell FIFO from LIFO -- one
      waiter is trivially both. Ordering across MULTIPLE waiters is (a)'s
      job; (b) only bounds how far one hot loop can run ahead of one waiter.
  (c) the context manager releases on the exception path.
  (d) non-blocking acquire, timeout, and release-when-unlocked semantics
      match what a threading.Lock stand-in has to provide, and a timed-out
      waiter never wedges the queue behind its abandoned ticket.
  (e) DeckController.__init__ installs the lock, and does it before open()
      starts the library's reader thread.
  (f) the install guard swaps a stock transport mutex, is idempotent, and
      no-ops on every shape it cannot prove safe (no transport device at
      all -- FakeDeck/RemoteDeck; a transport without a mutex attribute --
      library drift; a mutex that is currently held).
"""
import threading
import time

import fixtures  # noqa: F401  (isolated data dir + sys.path, house convention)

from src.backend.DeckManagement.fair_lock import FairLock

WATCHDOG_SECONDS = 60


def _wait_for_queue_depth(lock: FairLock, depth: int) -> None:
    """Blocks until `depth` tickets have been handed out. _next_ticket is
    bumped under the lock's condition at acquire() entry, so it is the exact
    'this thread has queued' signal -- polling a state flag in the worker
    instead would race the ticket draw and make the order under test
    non-deterministic."""
    assert fixtures.wait_until(lambda: lock._next_ticket >= depth, timeout=10.0), (
        f"only {lock._next_ticket} of {depth} tickets were drawn"
    )


def check_service_order_is_arrival_order() -> None:
    lock = FairLock()
    served = []

    lock.acquire()  # Held, so every worker below is forced to queue.

    workers = []
    for index in range(8):
        def _worker(index=index):
            with lock:
                served.append(index)

        t = threading.Thread(target=_worker, name=f"fifo-{index}", daemon=True)
        t.start()
        workers.append(t)
        # One ticket per started thread, drawn before the next thread starts:
        # arrival order is now known, not merely likely.
        _wait_for_queue_depth(lock, index + 2)

    lock.release()
    for t in workers:
        t.join(timeout=10.0)
        assert not t.is_alive(), "a queued waiter never got served"

    assert served == list(range(8)), f"served out of arrival order: {served}"
    assert not lock.locked(), "lock still owned after every waiter released"
    print("PASS: service order equals arrival order")


class _TicketDrawProbe:
    """Stands in for a FairLock's condition variable so the scenario can see
    the exact instant a thread's ticket is drawn.

    FairLock draws the ticket and calls `wait()` inside one `with self._cond`
    block, so a watched thread's FIRST wait() is its "I am now queued" edge,
    observed with no other thread able to draw or serve a ticket in between.
    Everything is delegated -- acquire()/release() run their real code.

    This exists because the obvious sampling point (read the counter just
    before calling acquire()) measures the wrong interval. FairLock orders
    threads by TICKET, and the ticket is drawn behind `self._cond`'s own
    mutex, which is a stock unfair threading.Lock: a hot loop that takes and
    drops that mutex twice per cycle can keep a would-be waiter out of the
    queue for milliseconds before it ever draws a ticket. Acquisitions in
    that window are not overtakes of a queued waiter -- nothing was queued
    yet -- but a before-acquire() sample counts them, which is how #186's
    "overtook the waiter 11 times" was reported on a lock that had not
    reordered anything (reproduced: 11 pre-queue acquisitions, 0 real
    overtakes). Sampling at the draw makes the bound exact instead of a
    fudge factor: see check_hot_loop_cannot_starve_a_waiter.
    """

    def __init__(self, cond, sample):
        self._cond = cond
        self._sample = sample
        self.watch = None      # thread whose ticket draw to catch
        self.snapshot = None   # (sample, monotonic) taken at that draw

    def __enter__(self):
        return self._cond.__enter__()

    def __exit__(self, *exc_info):
        return self._cond.__exit__(*exc_info)

    def notify_all(self):
        return self._cond.notify_all()

    def wait(self, timeout=None):
        # Only the first wait() of the watched thread's acquire(): a re-check
        # loop must not re-baseline the sample mid-wait.
        if self.snapshot is None and threading.current_thread() is self.watch:
            self.snapshot = (self._sample(), time.monotonic())
        return self._cond.wait(timeout)


def check_hot_loop_cannot_starve_a_waiter() -> None:
    HOLD_S = 0.0005
    RUN_S = 2.0

    lock = FairLock()
    acquisitions = 0
    stop = threading.Event()

    probe = _TicketDrawProbe(lock._cond, lambda: acquisitions)
    probe.watch = threading.current_thread()
    lock._cond = probe

    def _hot():
        nonlocal acquisitions
        while not stop.is_set():
            with lock:
                acquisitions += 1
                end = time.monotonic() + HOLD_S
                while time.monotonic() < end:
                    pass

    hot = threading.Thread(target=_hot, name="fair-lock-hot", daemon=True)
    hot.start()

    worst_latency = 0.0
    worst_call_latency = 0.0
    worst_overtakes = 0
    samples = 0
    queued_samples = 0
    deadline = time.monotonic() + RUN_S
    while time.monotonic() < deadline:
        probe.snapshot = None
        called_at = time.monotonic()
        with lock:
            served_at = time.monotonic()
            served = acquisitions
            drawn = probe.snapshot
        samples += 1
        # End-to-end: what the caller waited, ticket or no ticket.
        worst_call_latency = max(worst_call_latency, served_at - called_at)
        if drawn is not None:
            # Queued behind the hot loop: this sample is a real measurement
            # of what the ordering guarantee is worth. (`drawn is None` means
            # the lock was free at the draw and nothing had to be waited out,
            # so there is no overtaking to measure.)
            queued_samples += 1
            worst_overtakes = max(worst_overtakes, served - drawn[0])
            worst_latency = max(worst_latency, served_at - drawn[1])
        time.sleep(0.05)  # The library's 20Hz read poll cadence.

    stop.set()
    hot.join(timeout=10.0)

    # Vacuity first: if the poller never queued, every bound below is
    # trivially satisfied and the sample count is a red herring.
    assert queued_samples > 5, (
        f"the contention this check needs never happened: only "
        f"{queued_samples} of {samples} samples queued behind the hot loop, "
        f"so nothing here measured an ordering guarantee at all"
    )
    assert samples > 10, f"poller only got {samples} samples in {RUN_S}s"
    assert acquisitions > 100, (
        f"hot loop only managed {acquisitions} acquisitions -- FairLock "
        f"throughput collapsed"
    )
    # Exact, not a tolerance: once the poller holds ticket T, every hot
    # acquisition drawn afterwards holds a higher ticket and is served after
    # it, so the only one that may still land is the one already in flight
    # when T was drawn -- and only if it had not yet reached its `+= 1`.
    # An unfair lock loses this by orders of magnitude (the hot loop turns
    # over ~1000x per second here).
    assert worst_overtakes <= 1, (
        f"hot loop overtook the queued waiter {worst_overtakes} times -- "
        f"ordering is not FIFO"
    )
    # Loose ceiling: the read poll needs one slot per 50ms window. The real
    # figure is sub-millisecond; this only has to catch starvation.
    assert worst_latency < 0.05, (
        f"worst queued wait {worst_latency * 1000:.1f}ms exceeds one poll window"
    )
    # And the same bound end-to-end, from the acquire() CALL. Ordering is
    # dated from the ticket draw, but drawing a ticket means first winning
    # the condition's own mutex -- a stock unfair threading.Lock. A lock that
    # kept the HID poll from ever reaching its ticket would starve exactly
    # the way this module exists to prevent while satisfying every
    # draw-relative bound above, so the caller's whole wait is bounded too.
    # Two poll windows rather than one: the pre-draw stretch is scheduler-
    # governed, so it gets slack the ordered stretch does not need. Measured
    # worst under 32-core saturation is ~2ms, i.e. ~47x of headroom.
    assert worst_call_latency < 0.10, (
        f"worst end-to-end acquire {worst_call_latency * 1000:.1f}ms exceeds "
        f"two poll windows -- a waiter is being starved before it can even "
        f"draw a ticket"
    )
    print(
        f"PASS: hot loop ({acquisitions} acquisitions) never starved the "
        f"poller ({queued_samples} queued samples, worst {worst_overtakes} "
        f"overtakes, {worst_latency * 1000:.2f}ms queued / "
        f"{worst_call_latency * 1000:.2f}ms end-to-end)"
    )


def check_exception_path_releases() -> None:
    lock = FairLock()

    class _Boom(Exception):
        pass

    try:
        with lock:
            raise _Boom()
    except _Boom:
        pass
    else:
        raise AssertionError("__exit__ swallowed the exception")

    assert not lock.locked(), "lock still owned after an exception in the body"
    assert lock.acquire(timeout=1.0), "lock unusable after an exception in the body"
    lock.release()
    print("PASS: context manager releases on the exception path")


def check_lock_protocol_semantics() -> None:
    lock = FairLock()

    assert lock.acquire(blocking=False), "non-blocking acquire failed on a free lock"
    assert lock.locked()
    assert not lock.acquire(blocking=False), "non-blocking acquire succeeded while owned"

    t0 = time.monotonic()
    assert not lock.acquire(timeout=0.05), "timed acquire succeeded while owned"
    assert time.monotonic() - t0 < 5.0, "timed acquire ignored its timeout"

    # The abandoned ticket must not stall the queue behind it.
    lock.release()
    assert not lock.locked(), "abandoned ticket left the lock owned"
    assert lock.acquire(timeout=1.0), "abandoned ticket wedged the queue"
    lock.release()

    try:
        lock.release()
    except RuntimeError:
        pass
    else:
        raise AssertionError("releasing an unowned FairLock must raise")

    print("PASS: non-blocking, timeout and release-unlocked semantics hold")


class _StubTransport:
    def __init__(self, mutex=None):
        if mutex is not None:
            self.mutex = mutex


class _StubDeck:
    def __init__(self, device=None):
        self.device = device


def check_install_happens_before_open() -> None:
    """Integration tier: the swap has to be wired into DeckController.__init__
    AND land before open() starts the library's reader thread -- swapping a
    mutex that is already in use could put two threads in hidapi at once."""
    import globals as gl
    from faulty_fake_deck import FaultyFakeDeck

    # A deck with no transport at all (every FakeDeck) must still construct.
    plain = fixtures.make_headless_controller(serial="fairlock-noop")
    fixtures.teardown(plain)

    class _ProbeDeck(FaultyFakeDeck):
        """A FakeDeck carrying a transport shaped like the real one."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.device = _StubTransport(threading.Lock())
            self.mutex_type_at_open = None

        def open(self, *args, **kwargs):
            self.mutex_type_at_open = type(self.device.mutex)
            return super().open(*args, **kwargs)

    from src.backend.DeckManagement.DeckController import DeckController

    probe = _ProbeDeck(serial_number="fairlock-probe", deck_type="Fake Deck")
    controller = DeckController(gl.deck_manager, probe)
    gl.deck_manager.deck_controller.append(controller)
    try:
        assert probe.mutex_type_at_open is FairLock, (
            f"the transport mutex was {probe.mutex_type_at_open} when open() "
            f"started the reader thread -- the install is missing or too late"
        )
        assert isinstance(probe.device.mutex, FairLock), "the swap did not stick"
    finally:
        fixtures.teardown(controller)

    print("PASS: DeckController installs the FIFO lock before opening the deck")


def check_install_guards() -> None:
    from src.backend.DeckManagement.DeckController import _install_fair_transport_lock
    from faulty_fake_deck import FaultyFakeDeck

    stock = threading.Lock()
    deck = _StubDeck(_StubTransport(stock))
    assert _install_fair_transport_lock(deck), "stock transport mutex was not swapped"
    installed = deck.device.mutex
    assert isinstance(installed, FairLock), f"mutex is {type(installed).__name__}"

    # Idempotent: a second pass must not hand the transport a fresh lock
    # (which would be exactly the swap-while-held hazard the guard exists
    # to avoid, were this ever called twice).
    assert _install_fair_transport_lock(deck)
    assert deck.device.mutex is installed, "install replaced an existing FairLock"

    # No transport device: FakeDeck, RemoteDeck, anything non-HID.
    assert not _install_fair_transport_lock(_StubDeck(None)), (
        "install claimed success on a deck with no transport"
    )
    assert not _install_fair_transport_lock(
        FaultyFakeDeck(serial_number="fairlock-fake", deck_type="Fake Deck")
    ), "install claimed success on a FakeDeck"

    # Library drift: a transport whose mutex attribute is gone.
    assert not _install_fair_transport_lock(_StubDeck(_StubTransport())), (
        "install claimed success on a transport with no mutex"
    )

    # A held mutex must never be swapped out from under its holder.
    held = threading.Lock()
    held.acquire()
    held_deck = _StubDeck(_StubTransport(held))
    assert not _install_fair_transport_lock(held_deck), "install swapped a held mutex"
    assert held_deck.device.mutex is held, "held mutex was replaced anyway"
    held.release()

    print("PASS: install guard swaps a stock mutex and no-ops on every other shape")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_fair_lock")

    check_service_order_is_arrival_order()
    check_hot_loop_cannot_starve_a_waiter()
    check_exception_path_releases()
    check_lock_protocol_semantics()
    check_install_happens_before_open()
    check_install_guards()

    print("PASS: scenario_fair_lock")


if __name__ == "__main__":
    main()
