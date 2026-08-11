"""
Scenario: one page file, one dict -- shared by every deck showing it.

Two decks pointed at the same page used to hold two Page objects with two
independent dicts, kept in agreement by writing the file and reading it back
into each of them. The round trip WAS the agreement, so every editing path had
to remember to trigger it, and the ones that forgot left one deck showing a
page the other had stopped agreeing with. The dict is now held once, by a
document the page manager hands out per page file, and the agreement is an
identity instead of a protocol. This scenario holds that:

  1. IDENTITY. Two controllers on one path get one dict object -- whichever
     way their Page was minted (straight construction, or the page manager's
     cache miss). Two different pages get two.

  2. NO ROUND TRIP. An edit made through one Page is readable through its
     sibling with nothing written and nothing read: no atomic write, and the
     file on disk still says what it said before.

  3. ALIASING SURVIVES A REFRESH. The writers that go around the document (an
     import, a whole-settings write) put bytes in the file and ask for a
     re-read. That re-read must land in the dict every Page is already
     holding, not in a replacement -- a holder of `page.dict` taken before it
     must see the new content through the reference it already has.

  4. A REFRESH NEVER BLANKS A SECTION. It writes the new content in first and
     removes what the new content dropped second, so a reader crossing it can
     see a stale section one moment longer but can never see a section both
     versions have go missing. Checked by reading from another thread across
     tens of thousands of refreshes -- clearing first fails this in the first
     handful.

  5. THE HEAL IS REACHED. A document loads through the page manager, so a
     corrupt primary is substituted from pages/backups/ exactly as it is for
     any other read of a page file.

  6. THE DICT CANNOT BE REPLACED. `page.dict = {...}` raises rather than
     handing that Page a private copy its siblings cannot see.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
import threading

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog

from src.backend.PageManagement import page_flush
from src.backend.PageManagement.Page import Page


WRITES: list[str] = []


class StubController:
    """What Page construction and the page cache dereference, no more."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None

    def serial_number(self) -> str:
        return self.deck.get_serial_number()

    def load_page(self, page, *args, **kwargs) -> None:
        self.active_page = page


class FrozenScheduler:
    """A timer source that arms and never fires.

    The deferral's clock must not run under a scenario that asserts nothing
    was written: a trailing timer going off mid-check would write the page
    for reasons that have nothing to do with what is being tested.
    """

    def __init__(self):
        self.armed = 0

    def schedule(self, delay_s, callback):
        self.armed += 1
        return self.armed

    def cancel(self, handle) -> None:
        pass


def install_flush_recorder() -> None:
    """A fresh flush seam whose writes are counted.

    Every caller reaches the seam through page_flush.get(), so replacing the
    singleton covers the whole process.
    """
    page_flush._flush = page_flush.PageFlush(scheduler=FrozenScheduler(),
                                             clock=lambda: 0.0)
    real_write = page_flush.atomic_write_json

    def recording_write(path, data):
        WRITES.append(path)
        real_write(path, data)

    page_flush.atomic_write_json = recording_write
    WRITES.clear()


def read_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def write_file(path: str, data: dict) -> None:
    """A page file written by somebody other than the page: an importer, a
    settings write, the asset sweep."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def check_identity() -> int:
    path = seed_page("Shared")
    ctrl_a, ctrl_b = StubController("doc-a"), StubController("doc-b")

    page_a = Page(json_path=path, deck_controller=ctrl_a)
    ctrl_a.active_page = page_a
    # The other route in: a page-manager cache miss, which is how every deck
    # in the app actually gets its Page.
    page_b = gl.page_manager.get_page(path, ctrl_b)
    ctrl_b.active_page = page_b

    if page_a is page_b:
        print("FAIL: the two controllers were handed the same Page object")
        return 1
    if page_a.dict is not page_b.dict:
        print("FAIL: two Page objects on one path hold different dicts")
        return 1
    if page_a.dict is not gl.page_manager.get_document(path).data:
        print("FAIL: a Page's dict is not the document's dict")
        return 1

    other_path = seed_page("Other")
    page_other = Page(json_path=other_path, deck_controller=ctrl_a)
    if page_other.dict is page_a.dict:
        print("FAIL: two different pages share one dict")
        return 1

    # --- 2: the edit crosses with no file involved ----------------------
    before = read_file(path)
    page_a.dict.setdefault("keys", {})["7x7"] = {"states": {"0": {}}}
    if page_b.dict.get("keys", {}).get("7x7") is None:
        print("FAIL: an edit through one Page is invisible to its sibling")
        return 1
    if WRITES:
        print(f"FAIL: sharing an edit wrote to disk: {WRITES}")
        return 1
    if read_file(path) != before:
        print("FAIL: the page file changed while nothing asked for a write")
        return 1

    # ...and back the other way, through the page's own setters rather than
    # the raw dict.
    page_b.set_background("/some/wallpaper.png")
    if page_a.dict.get("background", {}).get("path") != "/some/wallpaper.png":
        print("FAIL: a setter on one Page is invisible to its sibling")
        return 1
    if WRITES:
        print(f"FAIL: a save wrote inline instead of arming the flush: {WRITES}")
        return 1

    # The counter-pressure for both assertions above: a write this scenario
    # DOES ask for has to show up in them, or "nothing was written" is a
    # statement about the recorder rather than about the code.
    page_a.flush()
    if WRITES != [path]:
        print(f"FAIL: the write recorder missed an explicit flush: {WRITES}")
        return 1
    if read_file(path).get("keys", {}).get("7x7") is None:
        print("FAIL: the flush did not carry the shared edit to the file")
        return 1

    print("PASS: two controllers on one page share one dict, and an edit "
          "crosses between them with no file involved")
    return 0


def check_refresh_preserves_aliasing() -> int:
    path = seed_page("Refreshed")
    ctrl_a, ctrl_b = StubController("refresh-a"), StubController("refresh-b")
    page_a = Page(json_path=path, deck_controller=ctrl_a)
    page_b = Page(json_path=path, deck_controller=ctrl_b)
    ctrl_a.active_page, ctrl_b.active_page = page_a, page_b

    # A reference taken the way every widget and render read takes one.
    held = page_a.dict
    held.setdefault("keys", {})["stale"] = {"states": {"0": {}}}

    write_file(path, {"keys": {"fresh": {"states": {"0": {}}}},
                      "settings": {"auto-change": {"enable": True}}})
    gl.page_manager.refresh_document(path)

    if page_a.dict is not held or page_b.dict is not held:
        print("FAIL: the refresh replaced the dict instead of refilling it")
        return 1
    if "fresh" not in held.get("keys", {}):
        print(f"FAIL: the refresh did not reach the held reference: {held}")
        return 1
    if held.get("settings", {}).get("auto-change", {}).get("enable") is not True:
        print(f"FAIL: a section the file added never arrived: {held}")
        return 1

    # A section the new content drops goes, so a refresh is a refresh and not
    # a merge.
    write_file(path, {"keys": {"fresh": {"states": {"0": {}}}}})
    gl.page_manager.refresh_document(path)
    if "settings" in held:
        print(f"FAIL: a section the file dropped survived the refresh: {held}")
        return 1

    print("PASS: a refresh refills the dict every Page is holding, additions "
          "and removals alike")
    return 0


class ProbedContent(dict):
    """New page content that looks at the document while it is being applied.

    dict.update() copies from a plain dict entirely in C, with nothing to hook
    from Python -- which is exactly why the refresh is safe, and exactly why
    the ORDER of its two steps needs a hook to be checked at all. Overriding
    __iter__ takes update() off that fast path and onto the mapping protocol,
    where it asks for keys() first and inserts afterwards; a probe in keys()
    therefore runs at the one moment the two possible orders differ. With the
    new content written in first, every old section is still there. With the
    dict cleared first, there is nothing there at all.
    """

    def __init__(self, content: dict, probe):
        super().__init__(content)
        self._probe = probe

    def __iter__(self):
        # Present only to move update() onto the mapping protocol -- see
        # above. Removing it makes the probe below unreachable.
        return super().__iter__()

    def keys(self):
        self._probe()
        return list(super().keys())


def check_refresh_never_blanks_a_section() -> int:
    """The reader's view across a refresh: stale, never missing.

    Driven at the document rather than through a file, because what is being
    pinned is the order of the two mutations inside it -- new content first,
    dropped sections second -- and a disk read per round would buy nothing but
    a slower loop.
    """
    path = seed_page("Concurrent")
    document = gl.page_manager.get_document(path)
    with_settings = {"keys": {"0x0": {"states": {"0": {}}}}, "settings": {"a": 1}}
    without_settings = {"keys": {"1x1": {"states": {"0": {}}}}}
    document.adopt(with_settings)

    # --- the order, deterministically -----------------------------------
    seen = []
    document.adopt(ProbedContent(without_settings,
                                 lambda: seen.append(dict(document.data))))
    if not seen:
        print("FAIL: the probe never ran -- the ordering check is vacuous")
        return 1
    if not seen[0].get("keys"):
        print(f"FAIL: the refresh emptied the page before refilling it: {seen[0]}")
        return 1
    if "settings" not in seen[0]:
        print(f"FAIL: the refresh dropped a section before writing the new "
              f"content in: {seen[0]}")
        return 1
    if "settings" in document.data:
        print(f"FAIL: the dropped section survived the refresh: {document.data}")
        return 1

    # --- and the same thing under a real reader on a real thread ---------
    stop = threading.Event()
    misses = []

    def reader():
        # Exactly the shape every render-path read of a page has: a .get
        # chain off the live dict, evaluated where it is needed.
        while not stop.is_set():
            if not document.data.get("keys", {}):
                misses.append(dict(document.data))
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for i in range(20000):
            document.adopt(with_settings if i % 2 else without_settings)
    finally:
        stop.set()
        thread.join(timeout=5)

    if misses:
        print(f"FAIL: a reader saw the page without its keys mid-refresh: {misses[0]}")
        return 1

    print("PASS: a refresh is never observable as a page that lost a section")
    return 0


def check_heal_through_the_document() -> int:
    path = seed_page("Healed")
    backup_dir = os.path.join(gl.page_manager.PAGE_PATH, "backups")
    write_file(os.path.join(backup_dir, os.path.basename(path)),
               {"keys": {"from-backup": {"states": {"0": {}}}}})
    with open(path, "w") as f:
        f.write('{"keys": {"0x0"')  # truncated mid-token

    controller = StubController("heal-1")
    page = Page(json_path=path, deck_controller=controller)

    if "from-backup" not in page.dict.get("keys", {}):
        print(f"FAIL: a document loaded a corrupt page without healing: {page.dict}")
        return 1

    print("PASS: a document load reaches the corrupt-heal")
    return 0


def check_dict_cannot_be_replaced() -> int:
    path = seed_page("NoSetter")
    page = Page(json_path=path, deck_controller=StubController("nosetter-1"))
    try:
        page.dict = {"keys": {}}
    except AttributeError:
        print("PASS: replacing a Page's dict raises instead of forking it")
        return 0
    print("FAIL: page.dict = {...} silently gave this Page a private copy")
    return 1


def main() -> int:
    start_watchdog(60, "page_document_identity")
    fixtures._install_integration_globals()
    install_flush_recorder()

    for check in (check_identity,
                  check_refresh_preserves_aliasing,
                  check_refresh_never_blanks_a_section,
                  check_heal_through_the_document,
                  check_dict_cannot_be_replaced):
        WRITES.clear()
        if check() != 0:
            return 1

    print("PASS: scenario_page_document_identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
