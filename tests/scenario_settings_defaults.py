"""
Pins the app-settings DEFAULTS table (gl#174).

Every default used to be inlined at its call site, so the same key could --
and did -- carry different defaults in different modules. SettingsManager
now owns one DEFAULTS table and AppSettings reads/writes through it. This
scenario locks each entry to the value the old inline call sites used, so a
transcription slip in the table (which would silently change runtime
behavior) fails here instead of in the field.

Two entries are deliberately NOT a straight transcription:

  * store.custom-stores defaulted to ``{}`` in StoreBackend.get_stores and
    to ``[]`` in the Settings dialog. The table says ``[]`` -- the value the
    writer actually appends to.
  * system.keep-running has no default at all: ``None`` means "never asked"
    and is what makes mainWindow.on_close raise the KeepRunningDialog. The
    accessor must keep returning None on a missing key, not False.
"""
import fixtures  # noqa: F401  (isolates gl.DATA_PATH before anything reads it)
import globals as gl

# Resolve the lazy font fallback up front: touching gl.fallback_font for
# real would run a full system font scan.
gl.fallback_font = "HarnessFallbackFont"

from src.backend.SettingsManager import (  # noqa: E402
    DEFAULTS,
    FONT_DEFAULTS,
    AppSettings,
    SettingsManager,
)

# A real SettingsManager rooted at the harness temp dir. No controller and
# no fixture tier are needed here -- only the settings file is touched.
gl.settings_manager = SettingsManager()

# The value each inline call site used before the conversion, by
# (section, key). Kept as a literal second copy on purpose: a typo in
# DEFAULTS has to disagree with something.
EXPECTED_DEFAULTS = {
    ("general", "hold-time"): 0.5,
    ("general", "rolling-labels"): True,
    ("general", "app-launches"): 0,
    ("general", "show-donate-window"): True,
    ("general", "default-font"): {},
    ("ui", "tray-icon"): True,
    ("ui", "allow-white-mode"): False,
    ("ui", "show-notifications"): True,
    ("ui", "auto-open-action-config"): True,
    ("key-grid", "emulate-at-double-click"): True,
    ("warnings", "enable-fps-warnings"): True,
    ("system", "keep-running"): None,
    ("system", "autostart"): True,
    ("system", "lock-on-lock-screen"): True,
    ("performance", "n-cached-pages"): 3,
    ("performance", "cache-videos"): True,
    ("performance", "animation-pause-mode"): "screensaver",
    ("performance", "animation-idle-minutes"): 5,
    ("store", "auto-update"): True,
    ("store", "responsibility-notes-agreed"): False,
    ("store", "enable-custom-stores"): False,
    ("store", "enable-custom-plugins"): False,
    ("store", "custom-stores"): [],
    ("store", "custom-plugins"): [],
    ("dev", "n-fake-decks"): 0,
    ("dev", "n-remote-decks"): 0,
}

EXPECTED_FONT_DEFAULTS = {
    "font-family": "HarnessFallbackFont",
    "font-size": 15,
    "font-weight": 400,
    "font-style": "normal",
    "font-color": (255, 255, 255, 255),
    "outline-color": (0, 0, 0, 1),
    "outline-width": 2,
}

# Property name -> (section, key), so a renamed/rewired property is caught.
PROPERTIES = {
    "hold_time": ("general", "hold-time"),
    "rolling_labels": ("general", "rolling-labels"),
    "app_launches": ("general", "app-launches"),
    "show_donate_window": ("general", "show-donate-window"),
    "default_font": ("general", "default-font"),
    "tray_icon": ("ui", "tray-icon"),
    "allow_white_mode": ("ui", "allow-white-mode"),
    "show_notifications": ("ui", "show-notifications"),
    "auto_open_action_config": ("ui", "auto-open-action-config"),
    "emulate_at_double_click": ("key-grid", "emulate-at-double-click"),
    "enable_fps_warnings": ("warnings", "enable-fps-warnings"),
    "keep_running": ("system", "keep-running"),
    "autostart": ("system", "autostart"),
    "lock_on_lock_screen": ("system", "lock-on-lock-screen"),
    "n_cached_pages": ("performance", "n-cached-pages"),
    "cache_videos": ("performance", "cache-videos"),
    "animation_pause_mode": ("performance", "animation-pause-mode"),
    "animation_idle_minutes": ("performance", "animation-idle-minutes"),
    "auto_update": ("store", "auto-update"),
    "responsibility_notes_agreed": ("store", "responsibility-notes-agreed"),
    "enable_custom_stores": ("store", "enable-custom-stores"),
    "enable_custom_plugins": ("store", "enable-custom-plugins"),
    "custom_stores": ("store", "custom-stores"),
    "custom_plugins": ("store", "custom-plugins"),
    "n_fake_decks": ("dev", "n-fake-decks"),
    "n_remote_decks": ("dev", "n-remote-decks"),
}

# Non-default probe values, one per property, used for the write/read-back
# round trip. Types match what the real writers store.
SAMPLES = {
    "hold_time": 1.25,
    "rolling_labels": False,
    "app_launches": 42,
    "show_donate_window": False,
    "default_font": {"font-size": 22},
    "tray_icon": False,
    "allow_white_mode": True,
    "show_notifications": False,
    "auto_open_action_config": False,
    "emulate_at_double_click": False,
    "enable_fps_warnings": False,
    "keep_running": True,
    "autostart": False,
    "lock_on_lock_screen": False,
    "n_cached_pages": 9,
    "cache_videos": False,
    "animation_pause_mode": "system-idle",
    "animation_idle_minutes": 17,
    "auto_update": False,
    "responsibility_notes_agreed": True,
    "enable_custom_stores": True,
    "enable_custom_plugins": True,
    "custom_stores": [{"url": "https://example.invalid", "branch": "main"}],
    "custom_plugins": [{"url": "https://example.invalid", "branch": "main"}],
    "n_fake_decks": 2,
    "n_remote_decks": 1,
}


def check_table_matches_expectations() -> None:
    table = {
        (section, key): value
        for section, entries in DEFAULTS.items()
        for key, value in entries.items()
    }
    assert table.keys() == EXPECTED_DEFAULTS.keys(), (
        f"DEFAULTS keys drifted -- only in table: "
        f"{sorted(table.keys() - EXPECTED_DEFAULTS.keys())}, only expected: "
        f"{sorted(EXPECTED_DEFAULTS.keys() - table.keys())}"
    )
    drifted = {
        k: (table[k], v) for k, v in EXPECTED_DEFAULTS.items()
        if table[k] != v or type(table[k]) is not type(v)
    }
    assert not drifted, f"DEFAULTS values drifted (key: got/expected): {drifted}"
    assert FONT_DEFAULTS.keys() == EXPECTED_FONT_DEFAULTS.keys(), (
        f"FONT_DEFAULTS keys drifted: {sorted(FONT_DEFAULTS)} != "
        f"{sorted(EXPECTED_FONT_DEFAULTS)}"
    )
    print("PASS: DEFAULTS/FONT_DEFAULTS match the pinned table")


def check_empty_settings_read_as_defaults() -> None:
    """An empty settings dict must produce exactly the old inline defaults."""
    app = AppSettings({})
    for name, (section, key) in PROPERTIES.items():
        got = getattr(app, name)
        expected = EXPECTED_DEFAULTS[(section, key)]
        assert got == expected and type(got) is type(expected), (
            f"AppSettings.{name} on empty settings gave {got!r} "
            f"({type(got).__name__}), expected {expected!r} "
            f"({type(expected).__name__})"
        )

    for key, expected in EXPECTED_FONT_DEFAULTS.items():
        got = app.font_default(key)
        assert got == expected, f"font_default({key!r}) gave {got!r}, expected {expected!r}"

    assert app.data == {}, f"reading defaults mutated the settings dict: {app.data}"
    print("PASS: empty settings read back as the pinned defaults")


def check_custom_stores_is_a_list() -> None:
    """The concrete bug this table fixes: StoreBackend defaulted
    store.custom-stores to {} while the Settings dialog appended to []."""
    app = AppSettings({})
    assert isinstance(app.custom_stores, list), (
        f"custom-stores default is {type(app.custom_stores).__name__}, must be list"
    )
    assert isinstance(app.custom_plugins, list)
    print("PASS: store.custom-stores/-plugins default to []")


def check_keep_running_tri_state() -> None:
    """None (never asked) must survive as None -- it is what raises the
    KeepRunningDialog in mainWindow.on_close."""
    app = AppSettings({})
    assert app.keep_running is None, f"missing keep-running gave {app.keep_running!r}"

    app = AppSettings({"system": {}})
    assert app.keep_running is None, "empty system section must still read None"

    for stored in (True, False):
        app = AppSettings({"system": {"keep-running": stored}})
        assert app.keep_running is stored, (
            f"stored keep-running={stored!r} read back as {app.keep_running!r}"
        )
    print("PASS: system.keep-running stays tri-state")


def check_mutable_defaults_are_not_shared() -> None:
    """A miss must never hand out the table's own container -- mutating the
    result (font_defaults does exactly that) would poison every later read."""
    a = AppSettings({})
    b = AppSettings({})

    a.default_font["font-size"] = 99
    assert b.default_font == {}, f"default-font default was shared: {b.default_font}"
    assert DEFAULTS["general"]["default-font"] == {}, "DEFAULTS table was mutated"

    a.custom_stores.append({"url": "x"})
    assert b.custom_stores == [], f"custom-stores default was shared: {b.custom_stores}"
    assert DEFAULTS["store"]["custom-stores"] == [], "DEFAULTS table was mutated"
    print("PASS: mutable defaults are copied per read")


def check_round_trip_through_the_underlying_dict() -> None:
    """Setters must write straight through to the wrapped dict (no parallel
    store), using the section/key the old raw writers used."""
    data: dict = {}
    app = AppSettings(data)

    for name, value in SAMPLES.items():
        setattr(app, name, value)

    for name, (section, key) in PROPERTIES.items():
        assert data[section][key] == SAMPLES[name], (
            f"AppSettings.{name} wrote {data.get(section, {}).get(key)!r} "
            f"to {section}.{key}, expected {SAMPLES[name]!r}"
        )
        assert getattr(app, name) == SAMPLES[name], f"{name} did not read back"

    # A second wrapper over the same dict sees the writes -- the accessor is
    # a view, not a copy.
    assert AppSettings(data).hold_time == SAMPLES["hold_time"]
    print("PASS: accessors read/write through the wrapped dict")


def check_unknown_keys_rejected_both_ways() -> None:
    """DEFAULTS is the schema on writes too: get() already raised KeyError
    for a (section, key) outside the table, but set() silently wrote a
    misspelled key that no reader would ever find. Both directions must
    trip; the runtime-computed keys CustomContentGroup actually uses must
    keep working."""
    app = AppSettings({})

    for section, key in (("general", "hold-tmie"), ("nope", "hold-time")):
        try:
            app.get(section, key)
            raise AssertionError(f"get({section!r}, {key!r}) did not raise")
        except KeyError:
            pass
        try:
            app.set(section, key, 1)
            raise AssertionError(f"set({section!r}, {key!r}) did not raise")
        except KeyError:
            pass
    assert app.data == {}, f"a rejected write still mutated the dict: {app.data}"

    # The generic path CustomContentGroup depends on stays open.
    for runtime_key in ("enable-custom-stores", "enable-custom-plugins"):
        app.set("store", runtime_key, True)
        assert app.get("store", runtime_key) is True

    print("PASS: unknown keys raise KeyError on get and set")


def check_wraps_the_shared_and_snapshot_dicts() -> None:
    """SettingsManager.app() must wrap the shared cached dict itself, so a
    write through the accessor is visible to raw readers before the save."""
    shared = gl.settings_manager.get_app_settings()
    app = gl.settings_manager.app()
    assert app.data is shared, "app() copied the settings dict instead of wrapping it"

    app.hold_time = 0.9
    assert shared["general"]["hold-time"] == 0.9
    app.save()

    reloaded = gl.settings_manager.get_app_settings()
    assert reloaded.get("general", {}).get("hold-time") == 0.9, (
        f"save() did not persist: {reloaded}"
    )
    assert gl.settings_manager.app().hold_time == 0.9
    print("PASS: app() wraps the shared dict and save() persists")


if __name__ == "__main__":
    check_table_matches_expectations()
    check_empty_settings_read_as_defaults()
    check_custom_stores_is_a_list()
    check_keep_running_tri_state()
    check_mutable_defaults_are_not_shared()
    check_round_trip_through_the_underlying_dict()
    check_unknown_keys_rejected_both_ways()
    check_wraps_the_shared_and_snapshot_dicts()
    print("\nALL PASS: scenario_settings_defaults")
