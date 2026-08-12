"""
Scenario: the app-settings surface -- one file, one shared copy, one snapshot.

`settings/settings.json` is read by the deck controller, the label engine, the
store, the tray, the launch counter and every page of the settings dialog. It
used to be reached four ways at once: a cached dict everyone shared, a private
`lru_cache` on the settings manager holding it, three call sites that re-read
the file from disk behind that cache, and the settings dialog's own
construction-time snapshot. This scenario pins what replaced them -- the
store's APP surface -- and the one user-visible bug the tangle produced.

WHAT IS UNDER TEST

  * THE SHARED COPY. Everything that asks the settings manager for the app
    settings gets the SAME dict, because its callers were written that way:
    they read it, write into it, and save it back, and a copy per reader would
    quietly drop whichever write was not the last one saved. A write to the
    file drops that copy, so the next reader is never behind the disk. The
    sharing holds all the way down: a stored list or dict read through the
    typed view is the one inside the settings, not a snapshot of it, so
    appending to it and saving persists the append.

  * DEFAULTS AT READ, STORAGE SPARSE. An absent key reads as the table's
    default and is NOT written back, so the day a default improves, the people
    who never chose a value get the improvement. A key the table does not
    describe is refused on write rather than stored where no reader would ever
    look for it.

  * THE SNAPSHOT. The settings dialog is the one reader that must NOT join the
    shared copy: it edits its own picture of the file and writes the whole
    picture back, so joining would publish half-made edits to everyone else.
    Its snapshot is read from disk, privately, and its batch save is still a
    batch save -- that part is deliberate and is pinned here as such.

  * THE REVERT. And the bug that fell out of the two being mixed: the font
    row saved the font defaults properly (a merge into the shared copy, which
    every write refreshes) and THEN wrote the dialog's construction-time
    snapshot on top of it.
    Every `general.*` value that had changed on disk since the window opened
    -- by the app itself, or by a second settings window -- was put back as it
    stood when the window opened. Choosing a font reverted your hold time.
    The red proof below drives the real `FontRow.on_set` against a real
    settings manager and fails on the pre-fix code.

The rows are driven UNBOUND, on stubs carrying the real methods, so no GTK
widget is built and no display is needed -- the app-shell scenarios' pattern.
"""
import fixtures  # noqa: F401  (must be first -- isolates the data dir)

import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402

import globals as gl  # noqa: E402

# Resolve the lazy font fallback up front: touching gl.fallback_font for real
# would run a full system font scan.
gl.fallback_font = "HarnessFallbackFont"

import gi  # noqa: E402

gi.require_version("Pango", "1.0")
from gi.repository import Pango  # noqa: E402

from src.backend import settings_store  # noqa: E402
from src.backend.SettingsManager import (  # noqa: E402
    DEFAULTS,
    FONT_DEFAULTS,
    AppSettings,
    SettingsManager,
)
from src.windows.Settings.Settings import FontRow, Settings  # noqa: E402

WATCHDOG_SECONDS = 60

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# The modules that used to open the app settings file by name: three that
# re-read it from disk behind the shared copy, each with its own inline
# default, plus the dialog, whose snapshot is deliberate but no longer builds
# a path of its own. All four now go through the typed view.
CONVERTED_READERS = (
    os.path.join(REPO_ROOT, "main.py"),
    os.path.join(REPO_ROOT, "src", "windows", "Settings", "Settings.py"),
    os.path.join(REPO_ROOT, "src", "windows", "mainWindow", "elements", "KeyGrid.py"),
    os.path.join(REPO_ROOT, "src", "backend", "DeckManagement", "DeckManager.py"),
)


def settings_path() -> str:
    return settings_store.APP.path()


def read_disk() -> dict:
    with open(settings_path()) as f:
        return json.load(f)


def write_disk(data: dict) -> None:
    os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
    with open(settings_path(), "w") as f:
        json.dump(data, f, indent=4)


def fresh_manager(seed: dict) -> SettingsManager:
    """A real settings manager over a settings file with exactly ``seed`` in
    it, and a cold store cache."""
    write_disk(seed)
    settings_store.get().invalidate_path(settings_path())
    gl.settings_manager = SettingsManager()
    return gl.settings_manager


# --------------------------------------------------------------------- #
# The dialog and the font row, reduced to what the revert needs          #
# --------------------------------------------------------------------- #

class WindowStub:
    """The settings dialog, reduced to the three members the font row
    reaches: the construction-time snapshot, the typed view over it, and the
    batch save.

    All three are the REAL `Settings` methods, carried onto a stub, so this
    drives the dialog's own code without instantiating an
    Adw.PreferencesWindow (which would need a display). Rebinding them is what
    makes the check honest: change `Settings.load_json` or `Settings.save_json`
    and this scenario follows.
    """

    app = Settings.app
    load_json = Settings.load_json
    save_json = Settings.save_json

    def __init__(self):
        self.settings_json: dict = None
        self.load_json()


class FontGroupStub:
    """FontPageGroup, reduced to what `on_set` calls on it."""

    def __init__(self, window: WindowStub):
        self.settings = window
        self.reload_requests = 0

    def request_page_reload(self) -> None:
        self.reload_requests += 1


class FontRowStub:
    """The real `FontRow.on_set`, on a stub carrying its one collaborator."""

    on_set = FontRow.on_set

    def __init__(self, group: FontGroupStub):
        self.font_page_group = group


class FontButtonStub:
    """Gtk.FontButton, reduced to the one getter `on_set` reads."""

    def __init__(self, description: str):
        self.description = description

    def get_font_desc(self) -> "Pango.FontDescription":
        return Pango.FontDescription.from_string(self.description)


# --------------------------------------------------------------------- #
# Checks                                                                 #
# --------------------------------------------------------------------- #

def check_tables_are_re_exported_by_identity() -> None:
    """The tables moved to the surface that owns them; the old names still
    reach the same objects, not copies of them.

    A copy would read identically and drift silently -- and the table's own
    pin imports these names, so it would go on passing against a table nothing
    uses.
    """
    assert DEFAULTS is settings_store.APP_DEFAULTS, (
        "SettingsManager.DEFAULTS is a different object from the app surface's "
        "schema -- two tables that agree today and need not tomorrow"
    )
    assert FONT_DEFAULTS is settings_store.APP_FONT_DEFAULTS
    assert AppSettings is settings_store.AppSettings
    assert settings_store.APP.schema is DEFAULTS, (
        "the app surface is registered with a schema other than the table its "
        "readers use"
    )
    assert AppSettings({}).schema is DEFAULTS

    # The font subtable is deliberately NOT the schema: its fallbacks trigger
    # on a falsy stored value, not only on an absent key, and one of them is
    # resolved by calling it.
    assert callable(FONT_DEFAULTS["font-family"]), (
        "the font family fallback stopped being lazy -- resolving it at import "
        "runs a full system font scan"
    )
    stored_but_empty = AppSettings({"general": {"default-font": {"font-size": 0, "font-family": ""}}})
    assert stored_but_empty.font_default("font-size") == 15, (
        "a zero font size must fall back: it is a half-written font, not a choice"
    )
    assert stored_but_empty.font_default("font-family") == "HarnessFallbackFont"

    print("PASS: the tables are re-exported by identity, and the font fallbacks keep their own semantics")


def check_shared_copy_is_one_object() -> None:
    """Every holder of the app settings holds the same dict, and a write to
    the file replaces it rather than leaving one holder behind."""
    manager = fresh_manager({"general": {"hold-time": 0.4}})

    first = manager.get_app_settings()
    assert manager.get_app_settings() is first, (
        "two reads of the app settings handed out two different dicts -- the "
        "holders that mutate one and save it back would lose each other's writes"
    )
    assert manager.app().data is first, "app() copied the shared dict instead of wrapping it"
    assert settings_store.get().read(settings_store.APP) is first, (
        "the store handed out a copy of a shared surface"
    )

    # A write through one view is visible to a raw holder before any save --
    # this is the property the sharing exists for.
    manager.app().hold_time = 1.5
    assert first["general"]["hold-time"] == 1.5

    # ...and unpersisted until somebody saves.
    assert read_disk()["general"]["hold-time"] == 0.4, "a set() through the view wrote the file"

    manager.app().save()
    assert read_disk()["general"]["hold-time"] == 1.5

    after = manager.get_app_settings()
    assert after is not first, (
        "saving the app settings must drop the shared copy, so the next reader "
        "loads what was actually written"
    )
    assert after["general"]["hold-time"] == 1.5

    print("PASS: the app settings are one shared dict, replaced on write")


def check_stored_containers_are_handed_out_by_reference() -> None:
    """The sharing holds all the way down, not just at the top dict.

    A view over the app settings hands back a stored list or dict BY
    REFERENCE. Hand back a copy instead and the whole idiom breaks silently:
    `settings.custom_stores.append(entry)` followed by a save appends to
    something nobody keeps, and the settings file comes back unchanged with no
    error anywhere. The font-defaults dict has the same shape -- its holder
    mutates it in place and the label engine reads that same object.
    """
    manager = fresh_manager({
        "general": {"default-font": {"font-size": 11}},
        "store": {"custom-stores": [{"url": "https://first.invalid", "branch": "main"}]},
    })

    view = manager.app()
    assert view.custom_stores is view.data["store"]["custom-stores"], (
        "a stored list came back as a copy: appending to it reaches nothing"
    )

    view.custom_stores.append({"url": "https://second.invalid", "branch": "dev"})
    view.save()
    stored = read_disk()["store"]["custom-stores"]
    assert [e["url"] for e in stored] == ["https://first.invalid", "https://second.invalid"], (
        f"appending through the typed accessor and saving persisted nothing: {stored}"
    )

    # The same shape the font defaults rely on: the manager's in-place holder
    # IS the dict inside the settings, so the label engine and the settings
    # agree without a save between them.
    manager = fresh_manager({"general": {"default-font": {"font-size": 11}}})
    assert manager.font_defaults is manager.get_app_settings()["general"]["default-font"], (
        "the font defaults the label engine reads are a snapshot of the settings, "
        "not the settings -- every in-place font edit would need a reload to be seen"
    )
    manager.font_defaults["font-size"] = 33
    assert manager.app().default_font["font-size"] == 33

    print("PASS: stored containers are handed out by reference, so writing through them lands")


def check_defaults_are_still_copied_per_read() -> None:
    """...and an ABSENT key is still a copy of the table's default.

    There is nothing stored to alias, and handing out the table's own
    container is how the first holder that mutates what it read poisons every
    later reader of that key -- `font_defaults` mutates in place, so this is
    not hypothetical.
    """
    a = AppSettings({})
    b = AppSettings({})

    a.default_font["font-size"] = 99
    assert b.default_font == {}, f"the default-font default was shared: {b.default_font}"
    assert DEFAULTS["general"]["default-font"] == {}, "the DEFAULTS table itself was mutated"

    a.custom_stores.append({"url": "poison"})
    assert b.custom_stores == [], f"the custom-stores default was shared: {b.custom_stores}"
    assert DEFAULTS["store"]["custom-stores"] == [], "the DEFAULTS table itself was mutated"

    assert a.data == {} and b.data == {}, "reading a default wrote it into the settings"

    # The deck surface stays copying at every level: its store reads are
    # already copies per caller, and nothing shares one.
    deck = settings_store.DeckSettings({"screensaver": {"media-path": "/tmp/x"}})
    assert deck.get("screensaver", "media-path") == "/tmp/x"
    section = deck.section("screensaver")
    section["media-path"] = "/tmp/mutated"
    assert deck.get("screensaver", "media-path") == "/tmp/x", (
        "a deck-settings section came back as a handle on the settings"
    )

    print("PASS: defaults are copied per read, and the copying views still copy")


def check_defaults_at_read_and_the_tripwire() -> None:
    """Absent keys read as the table's defaults without being written back,
    and a key the table does not describe is refused."""
    manager = fresh_manager({})

    app = manager.app()
    assert app.hold_time == 0.5
    assert app.emulate_at_double_click is True
    assert app.n_fake_decks == 0
    assert app.auto_update is True
    assert app.keep_running is None, "the never-asked tri-state must survive as None"

    assert manager.get_app_settings() == {}, (
        f"reading defaults materialized them into the shared dict: "
        f"{manager.get_app_settings()}"
    )
    assert read_disk() == {}, f"reading defaults wrote the file: {read_disk()}"

    for section, key in (("general", "hold-tmie"), ("nope", "hold-time")):
        try:
            app.set(section, key, 1)
        except KeyError:
            pass
        else:
            raise AssertionError(f"set({section!r}, {key!r}) was accepted into the app settings")
    assert manager.get_app_settings() == {}, "a refused write still mutated the shared dict"

    # A stored value wins over the table, and only the stored key is written.
    app.hold_time = 0.25
    app.save()
    assert read_disk() == {"general": {"hold-time": 0.25}}, (
        f"saving one setting persisted more than that setting: {read_disk()}"
    )
    assert manager.app().rolling_labels is True, "an absent sibling stopped reading its default"

    print("PASS: defaults apply at read, storage stays sparse, unknown keys are refused")


def check_reads_do_not_re_resolve_the_path() -> None:
    """A read served from memory must not walk the filesystem first.

    The cache is keyed by the RESOLVED path, so a symlinked settings file is
    one identity for reads, writes and invalidation. Resolving is an lstat per
    path component, and doing it per read put a syscall chain in front of an
    in-memory lookup -- the app settings are read from the label engine and
    the media caches, so that is a real cost paid for a link almost nobody
    has. The answer is remembered instead, and every write and invalidation
    still resolves for real.
    """
    manager = fresh_manager({"general": {"hold-time": 0.4}})
    manager.get_app_settings()  # warm: this is the read that resolves

    real_resolve = settings_store._resolve
    resolves = []

    def counting_resolve(path: str) -> str:
        resolves.append(path)
        return real_resolve(path)

    settings_store._resolve = counting_resolve
    try:
        for _ in range(50):
            manager.app().hold_time
        assert resolves == [], (
            f"a read served from memory resolved the path {len(resolves)} times -- "
            f"that is an lstat walk per settings read"
        )

        # A write resolves for real, every time.
        manager.app().save()
        assert resolves, "a write did not resolve the path it was writing"
    finally:
        settings_store._resolve = real_resolve

    print("PASS: reads are served without re-resolving the path; writes always resolve")


def check_a_moved_symlink_is_followed_from_the_next_store_event() -> None:
    """What remembering the resolution costs, stated exactly.

    A settings file that is a symlink into a managed config tree (stow,
    chezmoi) reads and writes under its target's identity. Symlink
    retargeting moves from the set of outside changes the store followed
    correctly into the set it is blind to until the next write -- joining
    outside rewrites of the file, which the content cache never followed.
    Resolving per read meant a repointed link changed the cache key, so the
    very next read missed and parsed the new target; that no longer happens.

    It self-heals: any write or invalidation through the store resolves
    afresh, and the new target is followed from there. The shape to have in
    mind is a managed config tree re-linking these files mid-run.
    """
    path = settings_path()
    real_a = path + ".target-a"
    real_b = path + ".target-b"
    for stale in (path, real_a, real_b):
        if os.path.islink(stale) or os.path.exists(stale):
            os.remove(stale)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(real_a, "w") as f:
        json.dump({"general": {"hold-time": 1.0}}, f)
    with open(real_b, "w") as f:
        json.dump({"general": {"hold-time": 2.0}}, f)
    os.symlink(real_a, path)
    settings_store.get().invalidate_path(path)
    gl.settings_manager = manager = SettingsManager()

    assert manager.app().hold_time == 1.0, "the link's target was not read"

    os.remove(path)
    os.symlink(real_b, path)
    assert manager.app().hold_time == 1.0, (
        "a link moved by somebody else was followed mid-read -- if the store "
        "now watches for that, this check is the thing to update"
    )

    settings_store.get().invalidate_path(path)
    assert manager.app().hold_time == 2.0, (
        "an invalidation did not resolve the path afresh: the store is still "
        "reading the link's old target"
    )

    os.remove(path)
    print("PASS: a moved symlink is followed from the next write or invalidation")


def check_snapshot_is_private_and_read_from_disk() -> None:
    """The dialog's snapshot must be its own dict, taken from the file."""
    manager = fresh_manager({"general": {"hold-time": 0.4}})

    shared = manager.get_app_settings()
    snapshot = manager.app_snapshot()
    assert snapshot.data is not shared, (
        "the settings dialog's snapshot joined the shared dict -- its "
        "half-finished edits would read as settled to every other reader"
    )
    assert snapshot.hold_time == 0.4

    snapshot.hold_time = 9.9
    assert shared["general"]["hold-time"] == 0.4, "the snapshot wrote through to the shared dict"
    assert read_disk()["general"]["hold-time"] == 0.4, "the snapshot wrote through to the file"

    # An unsaved edit to the shared dict is NOT what the next snapshot sees:
    # the snapshot describes the file, which is what the dialog will later
    # write back over.
    manager.app().hold_time = 7.7
    assert manager.app_snapshot().hold_time == 0.4, (
        "the snapshot picked up an unsaved in-memory edit instead of reading the file"
    )

    print("PASS: the dialog's snapshot is a private read of the file")


def check_font_row_does_not_revert_sibling_settings() -> None:
    """THE RED PROOF.

    A settings window is open. Something else changes `general.hold-time` on
    disk -- the app's own launch counter, a second settings window, a plugin.
    The user then picks a font. Before the fix, the font row wrote the
    window's construction-time snapshot after saving the font defaults, and
    the hold time went back to what it was when the window opened.
    """
    manager = fresh_manager({"general": {"hold-time": 0.5, "rolling-labels": True}})

    # The window opens: its snapshot has hold-time 0.5.
    window = WindowStub()
    assert window.settings_json["general"]["hold-time"] == 0.5

    # The other writer lands while the window sits there.
    other = manager.app()
    other.hold_time = 2.5
    other.rolling_labels = False
    other.save()
    assert read_disk()["general"]["hold-time"] == 2.5

    # The user picks a font.
    group = FontGroupStub(window)
    FontRowStub(group).on_set(FontButtonStub("DejaVu Sans Bold 12"))

    persisted = read_disk()["general"]
    assert persisted.get("default-font", {}).get("font-family") == "DejaVu Sans", (
        f"the font itself was not saved: {persisted.get('default-font')}"
    )
    assert persisted.get("hold-time") == 2.5, (
        f"choosing a font reverted general.hold-time to the value it had when the "
        f"settings window opened (got {persisted.get('hold-time')!r}, expected 2.5) -- "
        f"the window's construction-time snapshot was written on top of the font save"
    )
    assert persisted.get("rolling-labels") is False, (
        f"choosing a font reverted general.rolling-labels: {persisted.get('rolling-labels')!r}"
    )
    assert group.reload_requests == 1, (
        "the font row must still ask for exactly one page reload -- the write is "
        "immediate, only the reload is deferred"
    )

    print("PASS: choosing a font no longer reverts general.* settings changed since the window opened")


def check_batch_save_still_writes_the_whole_snapshot() -> None:
    """The residual, pinned as deliberate rather than left ambiguous.

    Every OTHER row in the dialog still saves by writing the whole snapshot:
    that is the dialog's batch contract, and the rows depend on it. So a
    second writer that lands while the window is open is still last-write-wins
    against the next toggle. Fixing that means giving the dialog a different
    save model, which is a change to the dialog, not to the font row.
    """
    manager = fresh_manager({"general": {"hold-time": 0.5}})
    window = WindowStub()

    other = manager.app()
    other.hold_time = 3.5
    other.save()

    # A toggle row: change something through the snapshot view, then batch-save.
    Settings.app.fget(window).tray_icon = False
    window.save_json()

    persisted = read_disk()
    assert persisted["ui"]["tray-icon"] is False, "the batch save did not persist the toggle"
    assert persisted["general"]["hold-time"] == 0.5, (
        "the batch save stopped writing the whole snapshot -- if that is now "
        "intended, this check is the thing to update, deliberately"
    )

    print("PASS: the dialog's other rows still batch-save the whole snapshot (deliberate)")


def check_launch_counter_path_unchanged() -> None:
    """The launch counter reads, increments and saves through the shared view.

    It is the app's own "second writer" -- the one that lands while a settings
    window can be open -- so its shape is pinned here rather than assumed.
    """
    import src.app as app_mod

    source = inspect.getsource(app_mod.App.on_activate)
    for line in (
        "app_settings = gl.settings_manager.app()",
        "app_settings.app_launches = app_settings.app_launches + 1",
        "app_settings.save()",
    ):
        assert line in source, (
            f"the launch counter no longer does {line!r} -- it is the reference "
            f"shape for a writer that lands while the settings window is open"
        )

    manager = fresh_manager({})
    for expected in (1, 2, 3):
        app_settings = manager.app()
        app_settings.app_launches = app_settings.app_launches + 1
        app_settings.save()
        assert read_disk()["general"]["app-launches"] == expected, (
            f"launch {expected} persisted {read_disk()['general']['app-launches']!r}"
        )

    print("PASS: the launch counter increments and persists through the shared view")


def check_no_module_reads_the_settings_file_by_hand() -> None:
    """No module opens the app settings file by name any more.

    Three of the four carried their own inline default for the key they
    wanted, which is how the same setting came to mean different things in
    different modules, and bypassed the shared copy, so they could not see a
    write that had not been saved yet. The fourth is the dialog, whose
    snapshot stays a snapshot but is now asked for rather than assembled.
    """
    for path in CONVERTED_READERS:
        with open(path, encoding="utf-8") as f:
            body = f.read()
        assert "settings.json" not in body, (
            f"{os.path.relpath(path, REPO_ROOT)} reads the app settings file by name "
            f"again -- it must go through the typed view, which owns both the path "
            f"and the defaults"
        )

    print("PASS: no converted reader opens the app settings file by name")


def main() -> int:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_app_settings_surface")

    check_tables_are_re_exported_by_identity()
    check_shared_copy_is_one_object()
    check_stored_containers_are_handed_out_by_reference()
    check_defaults_are_still_copied_per_read()
    check_defaults_at_read_and_the_tripwire()
    check_reads_do_not_re_resolve_the_path()
    check_a_moved_symlink_is_followed_from_the_next_store_event()
    check_snapshot_is_private_and_read_from_disk()
    check_font_row_does_not_revert_sibling_settings()
    check_batch_save_still_writes_the_whole_snapshot()
    check_launch_counter_path_unchanged()
    check_no_module_reads_the_settings_file_by_hand()

    print("ALL PASS: scenario_app_settings_surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
