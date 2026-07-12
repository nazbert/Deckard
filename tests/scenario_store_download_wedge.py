"""
Scenario: an exception during a store install/uninstall/update must not wedge
all store downloads for the session (issue #7 / B-07).

perform_download_threaded set currently_downloading=True, ran the operation,
and reset the flag only on the success path -- @log.catch ate any exception,
the reset never ran, and every later download click sat in a
`while currently_downloading: sleep(0.1)` poll forever (spinner spinning).
The check-then-set on the flag was also non-atomic.

Checks, against the real StorePreview.perform_download_threaded:
  1. A raising install() leaves currently_downloading False afterwards.
  2. A download AFTER the failure completes (pre-fix: hangs in the poll --
     detected via a bounded join on a worker thread).
  3. Two concurrent downloads never overlap (the lock serializes; observed
     via a max-concurrency counter inside install()).
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time
import types

from fixtures import start_watchdog

from src.windows.Store.Preview import StorePreview


def make_preview(store, install_state=0, install=None):
    """__new__ bypass (house pattern): the widget/GTK half of StorePreview is
    irrelevant to the download-serialization contract under test."""
    p = StorePreview.__new__(StorePreview)
    p.store_page = types.SimpleNamespace(store=store)
    p.install_state = install_state
    p.show_install_spinner = lambda *a, **k: None
    if install is not None:
        p.install = install
    return p


def make_store():
    return types.SimpleNamespace(
        currently_downloading=False,
        download_lock=threading.Lock(),
    )


def main() -> int:
    start_watchdog(30, "store_download_wedge")

    store = make_store()

    # 1) raising install must not latch the flag
    def boom():
        raise RuntimeError("install exploded")

    p_fail = make_preview(store, install=boom)
    p_fail.perform_download_threaded()  # @log.catch swallows the raise
    if store.currently_downloading:
        print("FAIL(1): raising install left currently_downloading latched "
              "True -- every later download would poll forever")
        return 1
    print("PASS: raising install resets currently_downloading")

    # 2) the next download must actually run, promptly
    ran = threading.Event()
    p_ok = make_preview(store, install=lambda: ran.set())
    t = threading.Thread(target=p_ok.perform_download_threaded, daemon=True)
    t.start()
    t.join(timeout=3)
    if t.is_alive() or not ran.is_set():
        print("FAIL(2): download after a failed install never ran "
              "(wedged in the currently_downloading poll)")
        return 1
    print("PASS: downloads still run after a failed install")

    # 3) concurrent clicks serialize
    active = [0]
    max_active = [0]
    counter_lock = threading.Lock()

    def slow_install():
        with counter_lock:
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
        time.sleep(0.15)
        with counter_lock:
            active[0] -= 1

    threads = [
        threading.Thread(
            target=make_preview(store, install=slow_install).perform_download_threaded,
            daemon=True,
        )
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        if t.is_alive():
            print("FAIL(3): concurrent downloads deadlocked")
            return 1
    if max_active[0] != 1:
        print(f"FAIL(3): {max_active[0]} installs ran concurrently (expected 1)")
        return 1
    print("PASS: concurrent download clicks serialize")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
