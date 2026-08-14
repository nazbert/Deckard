"""Pins the app-settings surface to one file, one shared copy, one snapshot.

Every holder of settings/settings.json shares one dict. Absent keys read the
table default without a write-back. The settings dialog keeps a private copy.
"""
import fixtures  # noqa: F401  (must be first -- isolates the data dir)

import inspect  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402

import globals as gl  # noqa: E402

# Resolve the lazy font fallback up front, because a real gl.fallback_font read
# runs a full system font scan.
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

# The four modules that reach the app settings. Three read a value through the
# typed view instead of opening the file and applying an inline default. The
# fourth is the dialog, which asks for its snapshot instead of building a path.
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
    """Build a real settings manager over a file holding exactly seed.

    The store cache starts cold.
    """
    write_disk(seed)
    settings_store.get().invalidate_path(settings_path())
    gl.settings_manager = SettingsManager()
    return gl.settings_manager


# The dialog and the font row, reduced to what the revert needs

class WindowStub:
    """The settings dialog, reduced to the three members the font row reaches.

    The snapshot, the typed view over it and the batch save are the real
    Settings methods carried onto a stub, so no Adw.PreferencesWindow is built.
    Change Settings.load_json or Settings.save_json and this scenario follows.
    """

    app = Settings.app
    load_json = Settings.load_json
    save_json = Settings.save_json

    def __init__(self):
        self.settings_json: dict = None
        self.load_json()


class FontGroupStub:
    """FontPageGroup, reduced to what on_set calls on it."""

    def __init__(self, window: WindowStub):
        self.settings = window
        self.reload_requests = 0

    def request_page_reload(self) -> None:
        self.reload_requests += 1


class FontRowStub:
    """The real FontRow.on_set, on a stub carrying its one collaborator."""

    on_set = FontRow.on_set

    def __init__(self, group: FontGroupStub):
        self.font_page_group = group


class FontButtonStub:
    """Gtk.FontButton, reduced to the one getter on_set reads."""

    def __init__(self, description: str):
        self.description = description

    def get_font_desc(self) -> "Pango.FontDescription":
        return Pango.FontDescription.from_string(self.description)


# Checks

def check_tables_reexported_by_identity() -> None:
    """The re-exported table names reach the same objects, not copies.

    A copy reads identically and drifts silently. The table's own pin imports
    these names, so it would keep passing against a table nothing uses.
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

    # The font subtable is not the schema. Its fallbacks fire on a falsy stored
    # value, not on an absent key alone, and a call resolves one of them.
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
    """Every holder of the app settings holds the same dict.

    A write to the file replaces that dict instead of leaving a holder behind.
    """
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

    # A write through one view is visible to a raw holder before any save. The
    # sharing exists for this property.
    manager.app().hold_time = 1.5
    assert first["general"]["hold-time"] == 1.5

    # The write stays unpersisted until somebody saves.
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


def check_stored_containers_shared_by_reference() -> None:
    """The sharing holds all the way down, not at the top dict alone.

    A view hands back a stored list or dict by reference. A copy breaks the
    idiom silently; an append then a save writes the file back unchanged, with
    no error. The font-defaults dict has the same shape.
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

    # The font defaults rely on this shape. The in-place holder of the manager
    # is the dict inside the settings, so the label engine and the settings
    # agree with no save between them.
    manager = fresh_manager({"general": {"default-font": {"font-size": 11}}})
    assert manager.font_defaults is manager.get_app_settings()["general"]["default-font"], (
        "the font defaults the label engine reads are a snapshot of the settings, "
        "not the settings -- every in-place font edit would need a reload to be seen"
    )
    manager.font_defaults["font-size"] = 33
    assert manager.app().default_font["font-size"] == 33

    print("PASS: stored containers are handed out by reference, so writing through them lands")


def check_defaults_copied_per_read() -> None:
    """An absent key still reads as a copy of the table default.

    Nothing is stored to alias. Handing out the table's own container lets the
    first holder that mutates it poison every later reader of that key.
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

    # The deck surface copies at every level. Its store reads are already copies
    # per caller, and nothing shares one.
    deck = settings_store.DeckSettings({"screensaver": {"media-path": "/tmp/x"}})
    assert deck.get("screensaver", "media-path") == "/tmp/x"
    section = deck.section("screensaver")
    section["media-path"] = "/tmp/mutated"
    assert deck.get("screensaver", "media-path") == "/tmp/x", (
        "a deck-settings section came back as a handle on the settings"
    )

    print("PASS: defaults are copied per read, and the copying views still copy")


def check_defaults_at_read_tripwire() -> None:
    """Absent keys read table defaults and are not written back.

    A key the table does not describe is refused on write.
    """
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


def check_reads_do_not_reresolve_path() -> None:
    """A read served from memory must not walk the filesystem first.

    The cache keys on the resolved path, so a symlinked settings file is one
    identity for reads, writes and invalidation. Resolving costs an lstat per
    path component, and every write and invalidation still resolves for real.
    """
    manager = fresh_manager({"general": {"hold-time": 0.4}})
    manager.get_app_settings()  # warm the cache with the read that resolves

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


def check_moved_symlink_refollow() -> None:
    """What the remembered resolution costs, stated exactly.

    A settings file that is a symlink into a managed config tree reads and
    writes under its target identity. The store is blind to a retargeted link
    until the next write or invalidation, which resolves afresh and refollows.
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


def check_snapshot_private_read_from_disk() -> None:
    """The dialog snapshot must be its own dict, taken from the file."""
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

    # An unsaved edit to the shared dict is not what the next snapshot sees. The
    # snapshot describes the file, which the dialog later writes back over.
    manager.app().hold_time = 7.7
    assert manager.app_snapshot().hold_time == 0.4, (
        "the snapshot picked up an unsaved in-memory edit instead of reading the file"
    )

    print("PASS: the dialog's snapshot is a private read of the file")


def check_font_row_keeps_sibling_settings() -> None:
    """A font pick must not revert a sibling general.* value.

    A settings window is open. The launch counter, a second window or a plugin
    changes general.hold-time on disk. The user then picks a font. The font row
    must not write the construction-time snapshot on top of that change.
    """
    manager = fresh_manager({"general": {"hold-time": 0.5, "rolling-labels": True}})

    # The window opens with hold-time 0.5 in its snapshot.
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


def check_batch_save_writes_snapshot() -> None:
    """The batch save of every other dialog row still writes the whole snapshot.

    The rows depend on that batch contract, so a second writer that lands while
    the window is open still loses to the next toggle. A different save model
    for the dialog is the only fix, and that is a change to the dialog.
    """
    manager = fresh_manager({"general": {"hold-time": 0.5}})
    window = WindowStub()

    other = manager.app()
    other.hold_time = 3.5
    other.save()

    # A toggle row changes something through the snapshot view, then batch-saves.
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

    It is the app's own second writer, the one that lands while a settings
    window can be open, so its shape is pinned here rather than assumed.
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


def check_no_module_opens_settings_file() -> None:
    """No module opens the app settings file by name.

    A module with its own inline default gives one setting different meanings,
    and a module behind the shared copy cannot see an unsaved write. The dialog
    keeps a snapshot, but it asks the surface for it instead of assembling one.
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

    check_tables_reexported_by_identity()
    check_shared_copy_is_one_object()
    check_stored_containers_shared_by_reference()
    check_defaults_copied_per_read()
    check_defaults_at_read_tripwire()
    check_reads_do_not_reresolve_path()
    check_moved_symlink_refollow()
    check_snapshot_private_read_from_disk()
    check_font_row_keeps_sibling_settings()
    check_batch_save_writes_snapshot()
    check_launch_counter_path_unchanged()
    check_no_module_opens_settings_file()

    print("ALL PASS: scenario_app_settings_surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
