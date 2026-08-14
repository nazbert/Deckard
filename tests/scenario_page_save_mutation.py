"""
Page.save must persist a consistent snapshot.

A save while another thread mutates page.dict must not raise, and must leave
valid JSON on disk. Stripping "object" from action entries must not touch the
live dict.
"""

# Saves for one json_path serialize across Page objects, which two controllers
# showing one page hold separately.
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import threading
import time

from fixtures import FaultyFakeDeck, seed_page, start_watchdog

from src.backend.PageManagement import page_flush
from src.backend.PageManagement.Page import Page


class StubController:
    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()


def make_action(sentinel) -> dict:
    return {"id": "com_example::Thing", "settings": {"a": 1}, "object": sentinel}


def main() -> int:
    start_watchdog(60, "page_save_mutation")
    fixtures._install_integration_globals()

    path = seed_page("SaveMutation")
    page = Page(json_path=path, deck_controller=StubController("save-mut-1"))

    # A large live dict widens the serialization window. Every action carries
    # a live, non-serializable "object", as it does at runtime.
    sentinel = object()
    page.dict.setdefault("keys", {})
    for i in range(1500):
        page.dict["keys"][f"{i}x0"] = {
            "states": {"0": {"actions": [make_action(sentinel)]}}
        }

    # A. Save under concurrent mutation.
    stop = threading.Event()

    def mutator():
        # Batches, not single add and delete pairs, so the dict size differs
        # from its iteration-start size for most of each GIL slice. A lone
        # add and delete restores the size before the reader looks.
        i = 0
        while not stop.is_set():
            batch = [f"mut-{i}-{j}x9" for j in range(25)]
            for key in batch:
                page.dict["keys"][key] = {"states": {"0": {"actions": [make_action(sentinel)]}}}
            for key in batch:
                del page.dict["keys"][key]
            i += 1

    t = threading.Thread(target=mutator, daemon=True)
    t.start()
    try:
        for i in range(100):
            try:
                # save() marks the page. The serialization, and the
                # RuntimeError this pins, happen in the flush, asked for here
                # so every round checks the property.
                page.save()
                page_flush.get().flush_path(path)
            except RuntimeError as e:
                print(f"FAIL: save() raised under concurrent mutation on iteration {i}: {e}")
                return 1
    finally:
        stop.set()
        t.join(timeout=5)

    with open(path) as f:
        saved = json.load(f)  # a raise here means the file is not valid JSON
    if "keys" not in saved:
        print("FAIL: saved page lost its keys section")
        return 1

    # B. The live dict keeps its "object" entries.
    live_action = page.dict["keys"]["0x0"]["states"]["0"]["actions"][0]
    if "object" not in live_action:
        print("FAIL: save() stripped 'object' from the LIVE action dict (mutated original)")
        return 1
    if "object" in saved["keys"]["0x0"]["states"]["0"]["actions"][0]:
        print("FAIL: 'object' leaked into the serialized page")
        return 1

    # C. Same-path saves serialize across Page objects.
    path2 = seed_page("SaveShared")
    page_a = Page(json_path=path2, deck_controller=StubController("save-shared-a"))
    page_b = Page(json_path=path2, deck_controller=StubController("save-shared-b"))

    events = []
    ev_lock = threading.Lock()
    in_critical = threading.Event()

    def instrument(page_obj, name):
        # Hook the snapshot, which every flush takes under the save lock.
        # The backup is taken once per file per session, so a second flush
        # never reaches it and leaves no second critical section to observe.
        orig = page_obj.get_without_action_objects

        def probe():
            with ev_lock:
                events.append((name, "enter", time.monotonic()))
            in_critical.set()
            time.sleep(0.15)  # hold the critical section open
            snapshot = orig()
            with ev_lock:
                events.append((name, "exit", time.monotonic()))
            return snapshot

        page_obj.get_without_action_objects = probe

    instrument(page_a, "a")
    instrument(page_b, "b")

    def saver(page_obj, wait_for_the_other):
        # One pending record exists per path, so the second marker must
        # arrive while the first flush is already inside the critical
        # section. Otherwise the two coalesce into one write and leave no
        # ordering to observe. The gate on the first probe forces that order.
        if wait_for_the_other and not in_critical.wait(timeout=10):
            raise AssertionError("the first save never reached its critical section")
        page_obj.save()
        page_flush.get().flush_path(path2)

    threads = [threading.Thread(target=saver, args=(p, wait))
               for p, wait in ((page_a, False), (page_b, True))]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
        if th.is_alive():
            print("FAIL: concurrent same-path save hung")
            return 1

    # Critical sections may not overlap. Between one page's enter and exit
    # there must be no other page's enter.
    spans = {}
    for name, kind, ts in events:
        spans.setdefault(name, {})[kind] = ts
    a, b = spans.get("a", {}), spans.get("b", {})
    if not all(k in a and k in b for k in ("enter", "exit")):
        print(f"FAIL: instrumentation incomplete: {events}")
        return 1
    overlap = a["enter"] < b["exit"] and b["enter"] < a["exit"]
    if overlap:
        print("FAIL: same-path saves from two Page objects ran concurrently "
              f"(a={a}, b={b})")
        return 1

    with open(path2) as f:
        json.load(f)

    print("PASS: save survives concurrent mutation, never mutates the live dict, "
          "and same-path saves serialize across Page objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
