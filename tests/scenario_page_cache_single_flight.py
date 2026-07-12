"""
Scenario: concurrent get_page() cache misses must construct exactly one Page
(issue #55 -- cache-miss double construction built twin Pages whose actions
hold live event/signal registrations).

Also covers the guard's failure path: a load for a nonexistent path must not
strand concurrent waiters (the in-flight entry is popped and its event set in
a `finally`, so waiters re-check and become the builder themselves).
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import threading
import time

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog

import src.backend.PageManagement.PageManagerBackend as pmb


class StubController:
    """Just enough controller for Page construction over an empty page json
    (Available_Identifiers needs .deck; load_action_objects compares
    .active_page)."""

    def __init__(self, serial: str = "single-flight-1"):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def main() -> int:
    start_watchdog(30, "page_cache_single_flight")
    fixtures._install_integration_globals()

    path = seed_page("SingleFlight")
    controller = StubController()

    # Count constructions and widen the cache-miss window so every thread
    # reaches the miss path before the first construction completes.
    construct_count = [0]
    count_lock = threading.Lock()
    real_page = pmb.Page

    class SlowPage(real_page):
        def __init__(self, json_path, deck_controller, *args, **kwargs):
            with count_lock:
                construct_count[0] += 1
            time.sleep(0.2)
            super().__init__(json_path, deck_controller, *args, **kwargs)

    pmb.Page = SlowPage
    try:
        n_threads = 6
        barrier = threading.Barrier(n_threads)
        results = [None] * n_threads

        def worker(i: int):
            barrier.wait()
            results[i] = gl.page_manager.get_page(path, controller)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            if t.is_alive():
                print("FAIL: get_page worker did not finish (deadlock?)")
                return 1

        distinct = {id(r) for r in results}
        if None in results:
            print(f"FAIL: get_page returned None for an existing page: {results}")
            return 1
        if len(distinct) != 1:
            print(f"FAIL: concurrent get_page returned {len(distinct)} distinct Page objects")
            return 1
        if construct_count[0] != 1:
            print(f"FAIL: Page constructed {construct_count[0]} times (expected 1)")
            return 1

        # A later call must hit the cache, not construct again.
        again = gl.page_manager.get_page(path, controller)
        if again is not results[0] or construct_count[0] != 1:
            print("FAIL: follow-up get_page did not reuse the cached Page")
            return 1
    finally:
        pmb.Page = real_page

    # Failure path: nonexistent page. Two racing callers must both get None
    # and neither may hang on the other's in-flight entry.
    missing = path + ".does-not-exist.json"
    barrier = threading.Barrier(2)
    missing_results = [object(), object()]

    def miss_worker(i: int):
        barrier.wait()
        missing_results[i] = gl.page_manager.get_page(missing, controller)

    threads = [threading.Thread(target=miss_worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        if t.is_alive():
            print("FAIL: get_page(missing) worker hung -- waiter stranded on failed load")
            return 1
    if missing_results != [None, None]:
        print(f"FAIL: get_page(missing) returned {missing_results} (expected [None, None])")
        return 1

    print("PASS: concurrent get_page constructs once; failed loads strand no waiters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
