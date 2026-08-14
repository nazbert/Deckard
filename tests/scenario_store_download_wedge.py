"""
An exception during a store install must not wedge every later download.

The real StorePreview.perform_download_threaded takes a lock, so the
currently_downloading flag clears whatever the operation did, a download
after a failure completes promptly, and two concurrent downloads never
overlap.
"""
import fixtures  # noqa: F401  (import first: sets up the isolated data dir)

import threading
import time
import types

from fixtures import start_watchdog

from src.windows.Store.Preview import StorePreview


def make_preview(store, install_state=0, install=None):
    """A __new__ bypass. The GTK half of StorePreview has no part in the
    download-serialization contract under test."""
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

    # 1. A raising install must not latch the flag.
    def boom():
        raise RuntimeError("install exploded")

    p_fail = make_preview(store, install=boom)
    p_fail.perform_download_threaded()  # @log.catch swallows the raise
    if store.currently_downloading:
        print("FAIL(1): raising install left currently_downloading latched "
              "True -- every later download would poll forever")
        return 1
    print("PASS: raising install resets currently_downloading")

    # 2. The next download must run, and promptly.
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

    # 3. Concurrent clicks serialize.
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
