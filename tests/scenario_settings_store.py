"""
Scenario: the settings store -- the one owner of the app's settings files.

Two things are under test here, and they are different in kind.

THE PRIMITIVE. The store answers four questions for every settings file it
owns: where it is, what an absent or corrupt one reads as, who may write it,
and what a write does to anyone holding a cached copy. Each answer is pinned
below at the store's own surface rather than through whichever caller happens
to exercise it:

  * heal -- a corrupt file is moved aside to a `.corrupt` sidecar, logged, and
    read as an empty root, with the corruption reported so a caller holding a
    backup can heal from the flag rather than from the rename having worked;
    a second corruption never clobbers the first forensic copy;
  * list roots -- an absent or corrupt ARRAY-rooted file reads as `[]`, not
    `{}`. This is not cosmetic: a list-rooted reader handed an empty object
    silently sees nothing, which is how a fresh install came to be seeded with
    the wrong shape;
  * isolation -- a cached surface hands out a deep copy per read, so a caller
    may mutate what it got (they nearly all do) without reaching the next
    reader;
  * invalidation follows the FILE that was written, not the surface the writer
    thought it was writing. Writing one deck's settings drops that deck's
    cached copy and nothing else's -- and the store has no entry point that
    writes without invalidating, which is the property the importer leg below
    exists to prove. One file means one identity, symlinks resolved, because
    the atomic writer follows a link and a store that did not would cache
    under one name and invalidate under another;
  * a write that lands while a reader is still inside the file is not undone
    by that reader finishing afterwards. Dropping a cache entry cannot reach a
    read that has not stored one yet, and once the narrowed invalidation is in
    place nothing else would ever correct it: an unrelated settings write no
    longer clears a deck;
  * `corrupt` describes the read, not the surface -- a cached surface must not
    keep reporting a quarantine that already happened;
  * `edit()` serializes a read-modify-write against other edits of the same
    file, so two of them cannot lose an update. Proved against a control that
    does the same work without it and loses one every time.

THE CRASH. `Assets.json` -- the custom asset library index -- was the one
settings surface with no heal path at all: a bare `json.load` inside
`AssetManagerBackend.__init__`, which the app constructs while it is building
its global objects. A corrupt index therefore did not degrade anything, it
took the whole app down before a window ever appeared, and the fix a user
could apply was "find and delete a file you have never heard of". The library
legs below are red on that shape: constructing the backend over a poisoned
index raises out of the constructor. They pass when the index reads as an
empty library, the corrupt bytes are preserved beside it, and the app carries
on -- a subsequent add still persists.
"""
import fixtures  # noqa: F401  (must be first -- see fixtures.py docstring)

import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import globals as gl  # noqa: E402

from src.backend import settings_store  # noqa: E402
from src.backend.MediaManager import MediaManager  # noqa: E402

WATCHDOG_SECONDS = 90

fixtures._install_integration_globals()
gl.media_manager = MediaManager()

from src.backend.AssetManagerBackend import AssetManagerBackend  # noqa: E402

WORK_DIR = os.path.join(gl.DATA_PATH, "store-probe")
DECKS_DIR = os.path.join(gl.DATA_PATH, "settings", "decks")

GARBAGE = "{ not json at all,,,"


def store() -> settings_store.SettingsStore:
    return settings_store.get()


def probe_path(name: str) -> str:
    os.makedirs(WORK_DIR, exist_ok=True)
    return os.path.join(WORK_DIR, name)


def write_raw(path: str, text: str) -> None:
    """Put bytes on disk WITHOUT the store, so the store is reading a file it
    did not write -- which is the only interesting case for a loader."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read_raw(path: str) -> str:
    with open(path) as f:
        return f.read()


def seed_deck(serial: str, settings: dict) -> str:
    path = os.path.join(DECKS_DIR, f"{serial}.json")
    write_raw(path, json.dumps(settings))
    store().invalidate_path(path)
    return path


# --------------------------------------------------------------------- #
# The primitive                                                          #
# --------------------------------------------------------------------- #

def check_heal_and_quarantine() -> None:
    path = probe_path("heal.json")
    write_raw(path, GARBAGE)

    data, corrupt = store().load_file(path)

    assert data == {}, f"a corrupt dict-rooted file must read as an empty dict, got {data!r}"
    assert corrupt is True, "a present-but-unparseable file must be reported as corrupt"
    assert not os.path.exists(path), "the corrupt primary must be moved aside, not left where the next save lands"
    assert read_raw(path + ".corrupt") == GARBAGE, "the corrupt bytes must survive in the sidecar"

    # Second corruption: the first forensic copy is the one a user would send
    # in, and it must not be destroyed by the next one.
    write_raw(path, "ALSO NOT JSON")
    data, corrupt = store().load_file(path)
    assert (data, corrupt) == ({}, True), f"second corruption misreported: {(data, corrupt)}"
    assert read_raw(path + ".corrupt") == GARBAGE, "the second quarantine clobbered the first forensic copy"
    assert read_raw(path + ".corrupt.1") == "ALSO NOT JSON", "the second corrupt copy was not preserved"
    print("PASS: a corrupt file is quarantined, reported, and read as an empty root; sidecars are never clobbered")


def check_absent_and_valid() -> None:
    missing = probe_path("never-written.json")
    assert store().load_file(missing) == ({}, False), "a missing file is not corrupt"
    assert not os.path.exists(missing + ".corrupt"), "a missing file must not produce a sidecar"

    valid = probe_path("valid.json")
    body = json.dumps({"kept": [1, 2, {"deep": True}]})
    write_raw(valid, body)

    data, corrupt = store().load_file(valid)
    assert corrupt is False, "a valid file must not be reported corrupt"
    assert data == {"kept": [1, 2, {"deep": True}]}, f"valid content misread: {data!r}"
    assert read_raw(valid) == body, "reading a valid file rewrote it"
    assert not os.path.exists(valid + ".corrupt"), "reading a valid file produced a sidecar"
    print("PASS: an absent file reads empty and a valid one is returned verbatim and left alone")


def check_list_root() -> None:
    missing = os.path.join(gl.DATA_PATH, "Assets", "AssetManager", "Assets.json")
    if os.path.exists(missing):
        os.remove(missing)

    empty = store().read(settings_store.ASSET_LIBRARY)
    assert empty == [], f"an absent array-rooted surface must read as [], got {empty!r}"
    assert isinstance(empty, list), (
        f"an absent array-rooted surface read as {type(empty).__name__}: a list-rooted "
        "reader handed a dict silently sees nothing"
    )
    assert not os.path.exists(missing), "reading an absent surface must not create the file"

    write_raw(missing, GARBAGE)
    corrupt_read = store().read(settings_store.ASSET_LIBRARY)
    assert corrupt_read == [], f"a corrupt array-rooted surface must read as [], got {corrupt_read!r}"
    assert read_raw(missing + ".corrupt") == GARBAGE, "the corrupt library index was not preserved"
    print("PASS: an array-rooted surface reads as a list when absent and when corrupt")


def check_corrupt_flag_describes_the_read() -> None:
    """`corrupt` is a fact about the read, not a property of the surface.

    On a CACHED surface it would be easy to keep the flag next to the content
    and hand it back forever; a caller that heals on it -- the shape page
    loads use -- would then heal on every read of a file that has been fine
    since the quarantine.
    """
    serial = "STORE-FLAG"
    path = os.path.join(DECKS_DIR, f"{serial}.json")
    write_raw(path, GARBAGE)
    store().invalidate_path(path)

    data, corrupt = store().read_reporting_corruption(settings_store.DECK, serial)
    assert (data, corrupt) == ({}, True), f"the healing read misreported: {(data, corrupt)}"

    # The file is gone now (quarantined), so a re-read finds it absent.
    assert store().read_reporting_corruption(settings_store.DECK, serial) == ({}, False), (
        "the corrupt flag was cached with the content and is now reported forever"
    )
    print("PASS: the corrupt flag describes the read that healed, not the surface")


def check_deepcopy_isolation() -> None:
    serial = "STORE-ISO"
    seed_deck(serial, {"brightness": {"value": 42}, "screensaver": {"enable": True}})

    first = gl.settings_manager.get_deck_settings(serial)
    first["brightness"]["value"] = 999
    first["injected"] = True

    second = gl.settings_manager.get_deck_settings(serial)
    assert second["brightness"]["value"] == 42, (
        f"a mutation of one read leaked into the next: {second!r}"
    )
    assert "injected" not in second, "a top-level mutation of one read leaked into the next"

    direct = store().read(settings_store.DECK, serial)
    assert direct == {"brightness": {"value": 42}, "screensaver": {"enable": True}}, (
        f"the store's own read was reached by the caller's mutation: {direct!r}"
    )
    assert json.loads(read_raw(os.path.join(DECKS_DIR, f"{serial}.json")))["brightness"]["value"] == 42, (
        "mutating a read persisted"
    )
    print("PASS: every read of a cached surface is an isolated deep copy")


def check_invalidation_follows_the_file() -> None:
    a, b = "STORE-INV-A", "STORE-INV-B"
    path_a = seed_deck(a, {"marker": "a-original"})
    path_b = seed_deck(b, {"marker": "b-original"})

    # Both cached.
    assert gl.settings_manager.get_deck_settings(a)["marker"] == "a-original"
    assert gl.settings_manager.get_deck_settings(b)["marker"] == "b-original"

    # A write through the store is visible to the next read of that deck.
    gl.settings_manager.save_deck_settings(a, {"marker": "a-written"})
    assert gl.settings_manager.get_deck_settings(a)["marker"] == "a-written", (
        "a deck's own write did not invalidate its cached copy"
    )

    # ...and reaches nothing else. Deck B's file is changed behind the store's
    # back so its cached copy is distinguishable from a reload; nothing but
    # the store writes these files, so the staleness is unreachable in the
    # app and is used here only to make the SCOPE observable.
    write_raw(path_b, json.dumps({"marker": "b-changed-behind-the-store"}))
    assert gl.settings_manager.get_deck_settings(b)["marker"] == "b-original", (
        "writing one deck's settings cleared another deck's cached copy: invalidation "
        "is supposed to follow the file that was written"
    )

    # A settings write elsewhere in the tree is not a reason to drop a deck
    # either -- it cannot have changed a deck's file.
    gl.settings_manager.save_settings_to_file(probe_path("unrelated.json"), {"unrelated": True})
    assert gl.settings_manager.get_deck_settings(b)["marker"] == "b-original", (
        "an unrelated settings write cleared a deck's cached copy"
    )

    # The path-level entry point still invalidates what it lands on: this is
    # what makes "no write through the store leaves a stale reader" true.
    gl.settings_manager.save_settings_to_file(path_b, {"marker": "b-through-the-path-entry"})
    assert gl.settings_manager.get_deck_settings(b)["marker"] == "b-through-the-path-entry", (
        "a write through the path-level entry point left a stale cached copy behind"
    )

    store().invalidate_path(path_a)
    assert gl.settings_manager.get_deck_settings(a)["marker"] == "a-written"
    print("PASS: invalidation is keyed by the file written -- exactly that file, and always that file")


def check_write_during_a_cold_read_is_not_lost() -> None:
    """A write that lands while a reader is still in the file must not be
    undone by that reader finishing afterwards.

    The window is between a cache miss and the parsed content being stored: a
    reader that got there first holds the content from BEFORE the write, and
    caching it makes every later reader see the old settings with nothing left
    to correct them -- the deck-settings cache is only dropped by writes to
    that deck's own file, so an unrelated write will not heal it. Reachable in
    the app as an import editing a deck's settings on a worker thread while
    the main thread reads that same deck.
    """
    serial = "STORE-RACE"
    path = seed_deck(serial, {"marker": "before-the-write"})

    real_load_file = settings_store.SettingsStore.load_file
    in_the_file = threading.Event()
    may_leave = threading.Event()

    def stalled_load_file(self, file_path, root=dict):  # type: ignore[no-untyped-def]
        parsed = real_load_file(self, file_path, root=root)
        if os.path.basename(file_path) == f"{serial}.json":
            # Parsed, not yet cached: exactly where the writer must be able to
            # get in front of this reader.
            in_the_file.set()
            may_leave.wait(timeout=15)
        return parsed

    read_result: list = []
    settings_store.SettingsStore.load_file = stalled_load_file  # type: ignore[method-assign]
    try:
        reader = threading.Thread(
            target=lambda: read_result.append(gl.settings_manager.get_deck_settings(serial)))
        reader.start()
        assert in_the_file.wait(timeout=10), "the stalled reader never reached the file"

        # The write lands while the reader is holding pre-write content.
        gl.settings_manager.save_deck_settings(serial, {"marker": "the-write"})

        may_leave.set()
        reader.join(timeout=15)
        assert not reader.is_alive(), "the stalled reader never finished"
    finally:
        settings_store.SettingsStore.load_file = real_load_file  # type: ignore[method-assign]

    assert read_result and read_result[0]["marker"] == "before-the-write", (
        f"the racing reader was supposed to read the pre-write content: {read_result!r}"
    )
    assert json.loads(read_raw(path))["marker"] == "the-write", "the write did not reach the file"

    after = gl.settings_manager.get_deck_settings(serial)
    assert after["marker"] == "the-write", (
        f"a read that raced the write cached the content from before it, and nothing "
        f"clears a deck's copy but a write to that deck's own file: {after!r}"
    )

    # And it is not a one-read fluke that a later write would have healed:
    # nothing else touches this deck's file from here on.
    gl.settings_manager.save_settings_to_file(probe_path("unrelated-after-race.json"), {"x": 1})
    assert gl.settings_manager.get_deck_settings(serial)["marker"] == "the-write", (
        "the cached copy went stale again once an unrelated settings write happened"
    )
    print("PASS: a write landing during a cold read is not undone by that read finishing")


def check_symlinked_surface_is_one_file() -> None:
    """A settings file that is a symlink must read, write, cache and
    invalidate under ONE identity.

    The atomic writer follows the link and writes the real file, so a store
    that keyed its cache on the spelling it was handed would cache under the
    link and invalidate under the target -- two names for one file, and a
    write through either spelling leaving the other's reader stale. Managed
    config trees (stow, chezmoi) are exactly this shape.
    """
    serial = "STORE-LINK"
    link_path = os.path.join(DECKS_DIR, f"{serial}.json")
    real_path = probe_path(f"real-{serial}.json")

    for stale in (link_path, real_path):
        if os.path.islink(stale) or os.path.exists(stale):
            os.remove(stale)
    write_raw(real_path, json.dumps({"marker": "through-the-link"}))
    os.makedirs(DECKS_DIR, exist_ok=True)
    os.symlink(real_path, link_path)
    store().invalidate_path(real_path)

    # Read via the LINK spelling (the app's own), then write via the TARGET
    # spelling. One file, so the reader must see the write.
    assert gl.settings_manager.get_deck_settings(serial)["marker"] == "through-the-link"
    gl.settings_manager.save_settings_to_file(real_path, {"marker": "through-the-target"})
    assert gl.settings_manager.get_deck_settings(serial)["marker"] == "through-the-target", (
        "a write to the link's target left the link's cached copy behind: the store is "
        "keying two names to one file"
    )

    # ...and the other way round: write via the link, read via the target.
    gl.settings_manager.save_deck_settings(serial, {"marker": "back-through-the-link"})
    assert store().load_file(real_path)[0]["marker"] == "back-through-the-link", (
        "the write through the link did not land in the file the link points at"
    )
    assert os.path.islink(link_path), "the write replaced the symlink instead of following it"
    print("PASS: a symlinked settings file is one identity for reads, writes and invalidation")


def check_importer_write_is_coherent() -> None:
    """The importer writes a deck's settings mid-session; a reader that has
    already seen that deck must not keep the version from before the import."""
    serial = "SDUIST"
    seed_deck(serial, {"rotation": 90, "brightness": {"value": 20}, "custom": {"keep": 1}})

    before = gl.settings_manager.get_deck_settings(serial)
    assert before["brightness"]["value"] == 20

    export = {
        "streamdeck_ui_version": 2,
        "state": {
            serial: {
                "brightness": 44,
                "brightness_dimmed": 7,
                "display_timeout": 600,
                "buttons": {},
            }
        },
    }
    export_path = os.path.join(gl.DATA_PATH, "sdui_store_export.json")
    write_raw(export_path, json.dumps(export))

    from src.windows.PageManager.Importer.StreamDeckUI.StreamDeckUI import StreamDeckUIImporter
    StreamDeckUIImporter(export_path).perform_import()

    after = gl.settings_manager.get_deck_settings(serial)
    assert after["brightness"]["value"] == 44, (
        f"the import was invisible to a reader that had already read this deck: {after!r}"
    )
    assert after["screensaver"]["enable"] is True, f"the import did not land: {after!r}"
    assert after["screensaver"]["time-delay"] == 10, f"the import did not land: {after!r}"
    assert after["screensaver"]["brightness"] == 7, f"the import did not land: {after!r}"
    assert after["rotation"] == 90 and after["custom"] == {"keep": 1}, (
        f"the import replaced unrelated deck settings instead of merging: {after!r}"
    )
    print("PASS: an import of a deck's preferences is visible to every later reader of that deck")


def check_edit_serializes() -> None:
    serial = "STORE-EDIT"
    threads_n, per_thread, hold = 4, 5, 0.01

    # The control: the same read-modify-write WITHOUT the store's edit block,
    # arranged so the interleaving is certain rather than likely. If this does
    # not lose an update, the leg below proves nothing.
    control = probe_path("lost-update.json")
    write_raw(control, json.dumps({"counter": 0}))
    gate = threading.Barrier(2)

    def unsynchronized() -> None:
        data, _ = store().load_file(control)
        gate.wait(timeout=10)
        data["counter"] = data["counter"] + 1
        store().save_file(control, data)

    racers = [threading.Thread(target=unsynchronized) for _ in range(2)]
    for t in racers:
        t.start()
    for t in racers:
        t.join(timeout=15)
    assert json.loads(read_raw(control))["counter"] == 1, (
        "the control did not lose an update, so this scenario cannot tell a lock from no lock"
    )

    seed_deck(serial, {"counter": 0})

    def increment() -> None:
        for _ in range(per_thread):
            with store().edit(settings_store.DECK, serial) as data:
                current = data["counter"]
                # Hold the block open: without serialization another thread
                # reads this same value and one of the increments vanishes.
                time.sleep(hold)
                data["counter"] = current + 1

    workers = [threading.Thread(target=increment) for _ in range(threads_n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=30)
        assert not t.is_alive(), "an edit() worker never finished -- the edit lock is not being released"

    expected = threads_n * per_thread
    on_disk = json.loads(read_raw(os.path.join(DECKS_DIR, f"{serial}.json")))["counter"]
    assert on_disk == expected, f"edit() lost an update: counter is {on_disk}, expected {expected}"
    assert gl.settings_manager.get_deck_settings(serial)["counter"] == expected, (
        "the last edit() left a stale cached copy behind"
    )
    print(f"PASS: {threads_n} threads x {per_thread} edits of one file lose nothing "
          f"(the same work without edit() loses an update every time)")


def check_edit_does_not_write_on_failure() -> None:
    serial = "STORE-EDIT-RAISE"
    seed_deck(serial, {"marker": "original"})

    class Deliberate(Exception):
        pass

    try:
        with store().edit(settings_store.DECK, serial) as data:
            data["marker"] = "half-applied"
            raise Deliberate()
    except Deliberate:
        pass

    assert json.loads(read_raw(os.path.join(DECKS_DIR, f"{serial}.json")))["marker"] == "original", (
        "an edit that raised half-way through was persisted anyway"
    )
    # And the lock is released, or every later edit of this deck would hang.
    with store().edit(settings_store.DECK, serial) as data:
        data["marker"] = "second"
    assert gl.settings_manager.get_deck_settings(serial)["marker"] == "second"
    print("PASS: an edit that raises writes nothing and still releases its lock")


def check_key_discipline() -> None:
    try:
        store().read(settings_store.DECK)
    except ValueError:
        pass
    else:
        raise AssertionError("a keyed surface read without a key invented a path")

    try:
        store().read(settings_store.ASSET_LIBRARY, "SOME-SERIAL")
    except ValueError:
        pass
    else:
        raise AssertionError("a keyless surface accepted a key, so a swapped argument is silent")
    print("PASS: a surface refuses a missing or surplus key instead of guessing a path")


# --------------------------------------------------------------------- #
# The asset library: a corrupt index must not stop the app starting      #
# --------------------------------------------------------------------- #

def library_path() -> str:
    return settings_store.ASSET_LIBRARY.path()


def check_corrupt_library_boots() -> None:
    path = library_path()
    for sidecar in (path + ".corrupt", path + ".corrupt.1", path + ".corrupt.2"):
        if os.path.exists(sidecar):
            os.remove(sidecar)
    write_raw(path, GARBAGE)

    # The boot shape: this constructor runs while the app builds its global
    # objects, so anything it raises is an app that never comes up.
    backend = AssetManagerBackend()

    assert list(backend) == [], f"a corrupt index must read as an empty library, got {list(backend)!r}"
    assert os.path.exists(path + ".corrupt"), "the corrupt index was not preserved beside its file"
    assert read_raw(path + ".corrupt") == GARBAGE, "the preserved index does not hold the corrupt bytes"
    assert json.loads(read_raw(path)) == [], (
        "the rewritten index is not an empty ARRAY -- a list-rooted reader sees nothing in an object"
    )

    # The app carries on: the library still works after the loss.
    png = fixtures.make_test_png(probe_path("library-asset.png"), size=(32, 32))
    asset_id = backend.add(png)
    assert asset_id is not None, "adding an asset after the corrupt index was healed failed"
    assert len(backend) == 1, f"the added asset is not in the live library: {list(backend)!r}"

    persisted = store().read(settings_store.ASSET_LIBRARY)
    assert isinstance(persisted, list) and len(persisted) == 1, (
        f"the added asset did not persist through the store: {persisted!r}"
    )
    assert persisted[0]["id"] == asset_id, "the persisted asset is not the one that was added"

    # A second construction over the healed index sees it -- the reopen path.
    reopened = AssetManagerBackend()
    assert [a["id"] for a in reopened] == [asset_id], (
        f"a fresh backend did not see the persisted library: {list(reopened)!r}"
    )
    print("PASS: a corrupt asset library index quarantines, boots empty, and the library still works")


def check_valid_library_untouched_by_load() -> None:
    path = library_path()
    entry = [{"name": "kept", "id": "kept-id", "internal-path": None, "thumbnail": None}]
    body = json.dumps(entry, indent=4)
    write_raw(path, body)
    sidecars_before = sorted(n for n in os.listdir(os.path.dirname(path)) if ".corrupt" in n)

    loaded = store().read(settings_store.ASSET_LIBRARY)

    assert loaded == entry, f"a valid index was misread: {loaded!r}"
    assert read_raw(path) == body, "merely reading a valid index rewrote it"
    sidecars_after = sorted(n for n in os.listdir(os.path.dirname(path)) if ".corrupt" in n)
    assert sidecars_after == sidecars_before, (
        f"reading a valid index quarantined it: {sorted(set(sidecars_after) - set(sidecars_before))}"
    )
    print("PASS: reading a valid asset library index leaves the file exactly as it was")


def main() -> int:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_settings_store")

    check_heal_and_quarantine()
    check_absent_and_valid()
    check_list_root()
    check_corrupt_flag_describes_the_read()
    check_deepcopy_isolation()
    check_invalidation_follows_the_file()
    check_write_during_a_cold_read_is_not_lost()
    check_symlinked_surface_is_one_file()
    check_importer_write_is_coherent()
    check_edit_serializes()
    check_edit_does_not_write_on_failure()
    check_key_discipline()
    check_corrupt_library_boots()
    check_valid_library_untouched_by_load()

    print("ALL PASS: scenario_settings_store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
