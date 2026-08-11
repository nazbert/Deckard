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
     versions have go missing. Pinned deterministically by looking at the
     document from inside the update itself; a real reader on a real thread
     then witnesses the same thing across tens of thousands of refreshes,
     which is a witness rather than a pin -- it needs the interpreter's switch
     interval wound right down to catch a window this narrow at all.

  5. THE HEAL IS REACHED. A document loads through the page manager, so a
     corrupt primary is substituted from pages/backups/ exactly as it is for
     any other read of a page file.

  6. THE DICT CANNOT BE REPLACED. `page.dict = {...}` raises rather than
     handing that Page a private copy its siblings cannot see.

  7. ONE FILE, ONE KEY, WHATEVER SPELLING NAMES IT. A page reached through a
     symlinked directory and the same page reached through the resolved one
     share a document, a save lock and a pending write. Keying those on the
     raw spelling instead is a data-loss bug rather than an inefficiency: the
     mark sits under one name, the read barrier asks under the other and walks
     straight through, and the re-read behind that barrier then erases the
     edit from memory as well as leaving it unwritten.

  8. A RENAME DOES NOT REFRESH THE LIVE PAGE. The move asks for the page under
     its old name, which mints a Page for any deck that had not cached it;
     that mint must not be able to pull the moved page's content back off the
     file it is moving away from, or an edit that is only in memory dies with
     it.

  9. A RENAME ONTO AN OPEN PAGE CORRECTS IT. Renaming over a name a deck is
     showing replaces that deck's file underneath it; the document standing
     there is re-read, or the deck keeps showing the page the move discarded
     and writes it back at its next save.

 10. A WRITE THAT GOES AROUND THE PAGE KEEPS THE PAGE'S PENDING EDIT. The
     whole-settings write reads, edits and writes the file itself, then asks
     for a re-read; a page with edits still pending must come out of that with
     both -- its own edit and the new settings.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import json
import os
import sys
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
    # The window between the two steps is a few bytecodes wide, so at the
    # default 5 ms switch interval the reader is almost never scheduled inside
    # it and the loop witnesses nothing either way. Wound down, it lands there
    # reliably -- and still never sees a missing section, which is the point.
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for i in range(20000):
            document.adopt(with_settings if i % 2 else without_settings)
    finally:
        sys.setswitchinterval(previous_interval)
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


def check_two_spellings_are_one_page() -> int:
    """A symlinked second name for the pages directory, which is the shape a
    data directory under a symlink gives every page in it."""
    path = seed_page("TwoSpellings")
    link_dir = os.path.join(gl.DATA_PATH, "pages_link")
    if not os.path.islink(link_dir):
        os.symlink(gl.page_manager.PAGE_PATH, link_dir)
    other = os.path.join(link_dir, os.path.basename(path))
    if os.path.realpath(other) != os.path.realpath(path):
        print("FAIL: the two spellings do not name one file -- check vacuous")
        return 1

    if page_flush._get_save_lock(path) is not page_flush._get_save_lock(other):
        print("FAIL: two spellings of one page file take two save locks -- "
              "their backup and write can interleave on the same bytes")
        return 1

    # The first spelling wins the document's own name, which is the half of
    # this that decides where a refresh's read barrier is aimed.
    page_a = Page(json_path=path, deck_controller=StubController("spell-a"))
    page_b = Page(json_path=other, deck_controller=StubController("spell-b"))
    if page_a.dict is not page_b.dict:
        print("FAIL: two spellings of one page file got two documents")
        return 1

    # The edit is marked under the SECOND spelling; the refresh below asks
    # under the first.
    page_b.dict["two-spellings"] = "edit-via-the-link"
    page_b.save()
    gl.page_manager.refresh_document(path)

    if page_a.dict.get("two-spellings") != "edit-via-the-link":
        print("FAIL: a refresh through one spelling erased an edit marked "
              f"under the other: {page_a.dict}")
        return 1
    if read_file(path).get("two-spellings") != "edit-via-the-link":
        print("FAIL: the edit marked under the second spelling never reached "
              "the file -- its read barrier did not see the mark")
        return 1

    print("PASS: two spellings of one page file are one document, one save "
          "lock and one pending write")
    return 0


def check_rename_does_not_refresh_the_live_page() -> int:
    """A move with a deck that has no cache entry for the page.

    The move asks every controller for the page under its old name, so that
    deck mints a Page -- and a mint reads the file. An edit that is only in
    memory has to survive that.
    """
    old_path = seed_page("RenameMe")
    ctrl_a = StubController("rename-a")
    ctrl_b = StubController("rename-b")
    gl.deck_manager.deck_controller = [ctrl_a, ctrl_b]

    page = gl.page_manager.get_page(old_path, ctrl_a)
    ctrl_a.active_page = page
    # ctrl_b is live but has never seen this page: an active page of its own,
    # and no cache entry for old_path.
    other_page = gl.page_manager.get_page(seed_page("RenameBystander"), ctrl_b)
    ctrl_b.active_page = other_page

    # In memory only -- no save, exactly like a pasted key waiting for the
    # reload that will persist it.
    page.dict["only-in-memory"] = "not-marked"

    new_path = os.path.join(gl.page_manager.PAGE_PATH, "Renamed.json")
    gl.page_manager.move_page(old_path, new_path)

    if page.json_path != new_path:
        print(f"FAIL: the move did not re-point the page: {page.json_path}")
        return 1
    if page.dict.get("only-in-memory") != "not-marked":
        print(f"FAIL: the move destroyed an unsaved in-memory edit: {page.dict}")
        return 1
    if gl.page_manager.get_document(new_path).data is not page.dict:
        print("FAIL: the renamed page's document is not the one its Page reads")
        return 1

    # Every Page the move touched ends up on that one document, including the
    # one it minted for the deck that had never cached this page.
    minted = gl.page_manager.get_page(old_path, ctrl_b)
    if minted is not None and minted.dict is not page.dict:
        print("FAIL: the Page minted during the move reads its own copy")
        return 1

    print("PASS: a rename carries the page's content over instead of "
          "re-reading it, for every deck the move touches")
    return 0


def check_rename_over_a_live_page() -> int:
    """A rename onto a page name a deck is already showing, where the page
    being renamed was never opened.

    Nothing moves onto the destination's document -- there is none to move --
    so the document standing there is the one that must be corrected: its file
    has just been replaced underneath it, and a Page reading through it would
    otherwise keep showing the overwritten page and write it back at its next
    save.
    """
    source_path = seed_page("StandingSource")
    write_file(source_path, {"keys": {}, "which-page": "the-source"})
    target_path = seed_page("StandingTarget")
    write_file(target_path, {"keys": {}, "which-page": "the-target"})

    controller = StubController("standing-1")
    gl.deck_manager.deck_controller = [controller]
    # Only the DESTINATION is open, so only it has a document.
    target_page = gl.page_manager.get_page(target_path, controller)
    controller.active_page = target_page
    if target_page.dict.get("which-page") != "the-target":
        print(f"FAIL: the destination page did not load: {target_page.dict}")
        return 1

    gl.page_manager.move_page(source_path, target_path)

    if read_file(target_path).get("which-page") != "the-source":
        print("FAIL: the move did not put the source page at the target name "
              "-- check vacuous")
        return 1
    if target_page.dict.get("which-page") != "the-source":
        print("FAIL: the deck showing the renamed-over page kept the content "
              f"the move replaced: {target_page.dict}")
        return 1

    print("PASS: a rename onto an open page corrects the document standing "
          "at that name")
    return 0


def check_settings_write_keeps_a_pending_edit() -> int:
    """The two seams crossing: a page edit still on its timer while the whole
    settings section is written round the outside of the page."""
    path = seed_page("SettingsCross")
    page = Page(json_path=path, deck_controller=StubController("cross-1"))

    page.dict["pending-edit"] = "marked-not-written"
    page.save()
    if read_file(path).get("pending-edit") is not None:
        print("FAIL: the edit was written inline -- nothing is pending, so "
              "the check is vacuous")
        return 1

    gl.page_manager.set_page_settings(path, {"brightness": {"value": 42}})

    if page.dict.get("pending-edit") != "marked-not-written":
        print(f"FAIL: the settings write erased the pending edit: {page.dict}")
        return 1
    if page.dict.get("settings", {}).get("brightness", {}).get("value") != 42:
        print(f"FAIL: the settings never reached the live page: {page.dict}")
        return 1
    on_disk = read_file(path)
    if on_disk.get("pending-edit") != "marked-not-written":
        print(f"FAIL: the pending edit never reached the file: {on_disk}")
        return 1
    if on_disk.get("settings", {}).get("brightness", {}).get("value") != 42:
        print(f"FAIL: the settings never reached the file: {on_disk}")
        return 1

    print("PASS: a whole-settings write flushes the page's pending edit and "
          "leaves both on the page and on disk")
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
                  check_two_spellings_are_one_page,
                  check_rename_does_not_refresh_the_live_page,
                  check_rename_over_a_live_page,
                  check_settings_write_keeps_a_pending_edit,
                  check_dict_cannot_be_replaced):
        WRITES.clear()
        if check() != 0:
            return 1

    print("PASS: scenario_page_document_identity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
