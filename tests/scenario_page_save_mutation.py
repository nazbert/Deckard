"""
Scenario: Page.save() must persist a consistent snapshot (issue #55).

Three properties:
  A. save() while another thread mutates page.dict must not raise
     (`RuntimeError: dictionary changed size during iteration` used to abort
     the dump and lose the save) and must always leave valid JSON on disk.
  B. Stripping "object" from action entries must not mutate the LIVE dict --
     the shallow copy() meant `del action["object"]` hit the originals.
  C. Saves for the same json_path must serialize ACROSS Page objects (two
     controllers showing one page each hold their own Page; the old
     per-object semaphore never ordered their writes).
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import threading
import time

from fixtures import FaultyFakeDeck, seed_page, start_watchdog

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

    # Populate a big live dict so the serialization window is wide, with a
    # live (non-serializable) "object" on every action like at runtime.
    sentinel = object()
    page.dict.setdefault("keys", {})
    for i in range(1500):
        page.dict["keys"][f"{i}x0"] = {
            "states": {"0": {"actions": [make_action(sentinel)]}}
        }

    # --- A: save under concurrent mutation ------------------------------
    stop = threading.Event()

    def mutator():
        # Batches (not single add/del pairs) so the dict's size differs from
        # its iteration-start size for most of each GIL slice -- a lone
        # add+del restores the size before the reader usually gets to look.
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
                page.save()
            except RuntimeError as e:
                print(f"FAIL: save() raised under concurrent mutation on iteration {i}: {e}")
                return 1
    finally:
        stop.set()
        t.join(timeout=5)

    with open(path) as f:
        saved = json.load(f)  # raises -> harness failure, which is the point
    if "keys" not in saved:
        print("FAIL: saved page lost its keys section")
        return 1

    # --- B: live dict keeps its "object" entries -------------------------
    live_action = page.dict["keys"]["0x0"]["states"]["0"]["actions"][0]
    if "object" not in live_action:
        print("FAIL: save() stripped 'object' from the LIVE action dict (mutated original)")
        return 1
    if "object" in saved["keys"]["0x0"]["states"]["0"]["actions"][0]:
        print("FAIL: 'object' leaked into the serialized page")
        return 1

    # --- C: same-path saves serialize across Page objects ----------------
    path2 = seed_page("SaveShared")
    page_a = Page(json_path=path2, deck_controller=StubController("save-shared-a"))
    page_b = Page(json_path=path2, deck_controller=StubController("save-shared-b"))

    events = []
    ev_lock = threading.Lock()

    def instrument(page_obj, name):
        orig = page_obj.make_backup

        def probe():
            with ev_lock:
                events.append((name, "enter", time.monotonic()))
            time.sleep(0.15)  # hold the critical section open
            orig()
            with ev_lock:
                events.append((name, "exit", time.monotonic()))

        page_obj.make_backup = probe

    instrument(page_a, "a")
    instrument(page_b, "b")

    barrier = threading.Barrier(2)

    def saver(page_obj):
        barrier.wait()
        page_obj.save()

    threads = [threading.Thread(target=saver, args=(p,)) for p in (page_a, page_b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
        if th.is_alive():
            print("FAIL: concurrent same-path save hung")
            return 1

    # Critical sections may not overlap: between one page's enter and exit
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
