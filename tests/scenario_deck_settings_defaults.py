"""Pins the deck-settings defaults table, and what the deck does with it.

DECK_DEFAULTS is the one table. A literal second copy of every value catches a
transcription slip, and the legs follow each default down to the device.
"""
import fixtures  # noqa: F401  (must be first -- see fixtures.py docstring)

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from types import MethodType  # noqa: E402

import globals as gl  # noqa: E402
from faulty_fake_deck import _hash_bytes  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from src.backend.settings_store import DECK_DEFAULTS, DeckSettings, SchemaView  # noqa: E402

# The value each inline call site used before the table existed, keyed by
# (section, key), or by name for a setting stored as a bare value. A literal
# second copy, so a typo in DECK_DEFAULTS has to disagree with something. Four
# entries below resolve an old disagreement and hold the device-layer number.
EXPECTED_DEFAULTS = {
    ("brightness", "value"): 75,        # the device layer and the page UI
    ("screensaver", "enable"): False,
    ("screensaver", "media-path"): None,
    ("screensaver", "loop"): True,
    ("screensaver", "fps"): 30,
    ("screensaver", "time-delay"): 5,
    ("screensaver", "brightness"): 30,  # what the device dims to without a key
    ("background", "enable"): False,
    ("background", "media-path"): None,  # None, not "", is what the device reads
    ("background", "loop"): True,        # same rationale as the screensaver loop
    ("background", "fps"): 30,
    ("background", "extend-to-touchscreen"): False,
    ("display", "saturation"): 1.0,
    ("rotation", None): 0,
}

# Not in the table. Whoever constructs a fake deck decides its key layout and
# passes it in as that caller's own fallback.
UNDESCRIBED_KEYS = ("key-layout",)


# The table

def check_table_matches_expectations() -> None:
    table = {}
    for name, value in DECK_DEFAULTS.items():
        if isinstance(value, dict):
            for key, sub in value.items():
                table[(name, key)] = sub
        else:
            table[(name, None)] = value

    assert table.keys() == EXPECTED_DEFAULTS.keys(), (
        f"DECK_DEFAULTS keys drifted -- only in table: "
        f"{sorted(k for k in table.keys() - EXPECTED_DEFAULTS.keys())}, only "
        f"expected: {sorted(k for k in EXPECTED_DEFAULTS.keys() - table.keys())}"
    )
    drifted = {
        k: (table[k], v) for k, v in EXPECTED_DEFAULTS.items()
        if table[k] != v or type(table[k]) is not type(v)
    }
    assert not drifted, f"DECK_DEFAULTS values drifted (key: got/expected): {drifted}"

    for key in UNDESCRIBED_KEYS:
        assert key not in DECK_DEFAULTS, (
            f"{key!r} gained a default in DECK_DEFAULTS -- it has no one value the "
            f"app could name, and describing it would let a write of it be refused"
        )
    print("PASS: DECK_DEFAULTS matches the pinned table")


def check_clamp_site_agrees() -> None:
    """The one deck default a reader still names for itself.

    The saturation factor feeds an ImageEnhance factor and a cache key, so the
    reader clamps it and rejects non-finite values. Its no-op constant must
    still be the table number, or there are two saturation defaults again.
    """
    from src.backend.DeckManagement.DeckController import DeckController

    assert DeckController.DEFAULT_DISPLAY_SATURATION == DECK_DEFAULTS["display"]["saturation"], (
        f"the saturation clamp's no-op constant "
        f"({DeckController.DEFAULT_DISPLAY_SATURATION}) and the table "
        f"({DECK_DEFAULTS['display']['saturation']}) disagree"
    )
    print("PASS: the saturation clamp's constant is the table's value")


# The view. Defaults at read, sparse storage, unknown keys refused

def check_absent_keys_read_table() -> None:
    data: dict = {}
    view = DeckSettings(data)

    for (name, key), expected in EXPECTED_DEFAULTS.items():
        got = view.get(name) if key is None else view.get(name, key)
        assert got == expected and type(got) is type(expected), (
            f"{name}.{key} on empty settings gave {got!r} ({type(got).__name__}), "
            f"expected {expected!r} ({type(expected).__name__})"
        )

    assert data == {}, f"reading defaults mutated the settings dict: {data}"
    print("PASS: absent keys read as the table without being written into the file")


def check_stored_values_win_and_survive() -> None:
    """A stored value is returned unchanged, and an undescribed key is kept.

    The persisted population keeps its meaning.
    """
    data = {
        "brightness": {"value": 50},
        "screensaver": {"loop": False, "brightness": 12},
        "background": {"enable": True},
        "rotation": 180,
        "key-layout": [3, 2],
    }
    view = DeckSettings(data)

    assert view.get("brightness", "value") == 50, "a persisted brightness was overridden"
    assert view.get("screensaver", "loop") is False, "an explicit loop=false was overridden"
    assert view.get("screensaver", "brightness") == 12
    assert view.get("rotation") == 180
    # The keys that section never stored still follow the table.
    assert view.get("screensaver", "fps") == 30
    assert view.get("background", "loop") is True

    merged = view.section("screensaver")
    assert merged == {
        "enable": False, "media-path": None, "loop": False, "fps": 30,
        "time-delay": 5, "brightness": 12,
    }, f"section() merged wrong: {merged}"

    merged["fps"] = 1
    assert view.get("screensaver", "fps") == 30, "section() handed out live state"
    assert data["screensaver"] == {"loop": False, "brightness": 12}, (
        f"reading through the view rewrote the stored settings: {data['screensaver']}"
    )
    assert data["key-layout"] == [3, 2], "a key the schema does not describe was dropped"
    print("PASS: stored values win, undescribed keys survive, section() is a copy")


def check_container_defaults_copied_per_read() -> None:
    """No reader may ever receive the schema's own container.

    DECK_DEFAULTS holds only scalars today, so nothing here notices a shallow
    hand-out. The app-settings default-font is a dict its holder mutates in
    place, and one shared container there poisons every later read.
    """
    schema = {"fonts": {"default-font": {}, "families": []}}
    a, b = SchemaView({}, schema), SchemaView({}, schema)

    a.section("fonts")["default-font"]["font-size"] = 99
    a.get("fonts", "families").append("poisoned")
    assert b.section("fonts") == {"default-font": {}, "families": []}, (
        f"a container default was handed out by reference: {b.section('fonts')}"
    )
    assert schema["fonts"] == {"default-font": {}, "families": []}, "the schema table was mutated"
    print("PASS: container defaults are copied per read")


def check_writes_are_sparse_and_tripwired() -> None:
    data: dict = {}
    view = DeckSettings(data)

    view.set("screensaver", "enable", True)
    assert data == {"screensaver": {"enable": True}}, (
        f"a write persisted more than the key it was given: {data}"
    )
    view.set_value("rotation", 90)
    assert data == {"screensaver": {"enable": True}, "rotation": 90}

    for name, key in (("screensaver", "brightnes"), ("screensvaer", "enable")):
        for call in (lambda: view.get(name, key), lambda: view.set(name, key, 1)):
            try:
                call()
            except KeyError:
                pass
            else:
                raise AssertionError(f"{name}.{key} was accepted -- a typo writes a key nothing reads")

    try:
        view.get("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("an undescribed top-level name was accepted")

    assert data == {"screensaver": {"enable": True}, "rotation": 90}, (
        f"a rejected write still mutated the settings: {data}"
    )

    # A section is a section and a bare value is a bare value. Confusing them
    # would write a shape no reader expects.
    for call in (lambda: view.get("screensaver"), lambda: view.set_value("screensaver", 1),
                 lambda: view.get("rotation", "value"), lambda: view.set("rotation", "value", 1)):
        try:
            call()
        except KeyError:
            pass
        else:
            raise AssertionError("a section and a top-level setting were used interchangeably")
    print("PASS: writes are sparse, unknown keys and wrong shapes raise")


def check_scalar_in_section_slot() -> None:
    """A hand-edited file can leave a bare value where a section belongs.

    The table answers then, and a write replaces the wreckage rather than
    raising out of a settings page.
    """
    data = {"screensaver": "on"}
    view = DeckSettings(data)
    assert view.get("screensaver", "brightness") == 30
    assert view.section("screensaver")["loop"] is True
    view.set("screensaver", "enable", True)
    assert data == {"screensaver": {"enable": True}}
    print("PASS: a scalar where a section belongs reads as the table")


def check_view_over_dict_cannot_save() -> None:
    try:
        DeckSettings({}).save()
    except ValueError:
        pass
    else:
        raise AssertionError("a view built over a bare dict saved somewhere")
    print("PASS: a view over a dict refuses to save")


# Fixtures for the device legs

def make_gif(name: str, n_frames: int = 4) -> str:
    path = os.path.join(gl.DATA_PATH, "media", name)
    frames = []
    for i in range(n_frames):
        frame = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(frame).ellipse([8 + i * 4, 12, 36 + i * 4, 40], fill=(200, 40, 60, 255))
        frames.append(frame)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0, disposal=2)
    return path


def seed_page_settings(name: str, settings: dict) -> str:
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"keys": {}, "dials": {}, "touchscreens": {}, "settings": settings}, f)
    return path


def deck_settings_file(serial: str) -> str:
    return os.path.join(gl.DATA_PATH, "settings", "decks", f"{serial}.json")


def file_fingerprint(path: str):
    """The file content, or None while the file does not exist.

    The first-open legs must tell those two states apart.
    """
    if not os.path.exists(path):
        return None
    return hashlib.sha1(open(path, "rb").read()).hexdigest()


def seed_deck_settings(serial: str, settings: dict) -> None:
    fixtures._install_integration_globals()
    gl.settings_manager.save_deck_settings(serial, settings)


def brightness_writes(deck) -> list:
    return [(e[4], e[5]) for e in deck.ops_by_name("set_brightness")]


def assert_landed_on_media_thread(controller, deck, value, what: str) -> None:
    landed = fixtures.wait_until(
        lambda: any(e[0] == _hash_bytes(value) for e in brightness_writes(deck)), timeout=5)
    assert landed, (
        f"{what}: brightness {value} never reached the device -- journal has "
        f"{brightness_writes(deck)}"
    )
    for fingerprint, thread in brightness_writes(deck):
        assert thread == controller.media_player.name, (
            f"{what}: a brightness write landed on {thread!r}, not the media thread "
            f"{controller.media_player.name!r} -- the deck has one writer"
        )
    assert controller.deck.owner_violations == [], (
        f"{what}: device owner violations recorded: {controller.deck.owner_violations}"
    )


# The defaults, followed to the device

def check_fresh_deck_table_brightness() -> None:
    controller = fixtures.make_headless_controller(serial="deck-defaults-brightness")
    deck = fixtures.raw_deck(controller)
    try:
        assert controller.brightness == EXPECTED_DEFAULTS[("brightness", "value")], (
            f"a deck whose brightness nobody chose came up at {controller.brightness}"
        )
        assert_landed_on_media_thread(
            controller, deck, EXPECTED_DEFAULTS[("brightness", "value")], "fresh deck")
        assert file_fingerprint(deck_settings_file("deck-defaults-brightness")) is None, (
            "bringing a deck up wrote a settings file for it"
        )
        print("PASS: a deck nobody configured runs at the table's brightness")
    finally:
        fixtures.teardown(controller)


def check_keyless_screensaver_loops_dims() -> None:
    gif = make_gif("saver.gif")
    serial = "deck-defaults-saver"
    # No loop and no brightness, the shape every config written before those
    # toggles existed has.
    seed_deck_settings(serial, {"screensaver": {
        "enable": True, "media-path": gif, "time-delay": 60, "fps": 30,
    }})
    controller = fixtures.make_headless_controller(serial=serial)
    deck = fixtures.raw_deck(controller)
    try:
        page = gl.page_manager.get_page(fixtures.seed_page("PlainSaver"), controller)
        controller.load_page(page, allow_reload=True)

        assert controller.screen_saver.loop is True, (
            "a deck screensaver without a loop key must loop -- one pass and a frozen "
            "last frame for the whole idle window is not a screensaver"
        )
        assert controller.screen_saver.brightness == 30, (
            f"a deck screensaver without a brightness key must dim to 30, got "
            f"{controller.screen_saver.brightness}"
        )

        deck.clear_journal()
        controller.screen_saver.show()
        assert fixtures.wait_until(lambda: controller.background.video is not None, timeout=5), (
            "the screensaver's media never landed"
        )
        assert controller.background.video.loop is True, (
            "the defaulted loop must reach the live provider, not just the ScreenSaver"
        )
        assert_landed_on_media_thread(controller, deck, 30, "keyless deck screensaver")
        controller.screen_saver.hide()
        assert fixtures.wait_until(lambda: not controller.screen_saver.showing, timeout=5)
        print("PASS: a keyless deck screensaver loops and dims to the table's brightness")
    finally:
        fixtures.teardown(controller)


def check_deck_loops_page_does_not() -> None:
    """The two loop defaults disagree, and this leg keeps them that way.

    A deck background is the leave-it-running case. A page background is a
    flourish on page entry.
    """
    gif = make_gif("bg.gif")
    serial = "deck-defaults-bg"
    seed_deck_settings(serial, {"background": {"enable": True, "media-path": gif, "fps": 30}})
    controller = fixtures.make_headless_controller(serial=serial)
    try:
        page = gl.page_manager.get_page(fixtures.seed_page("PlainBg"), controller)
        controller.load_page(page, allow_reload=True)
        assert fixtures.wait_until(lambda: controller.background.video is not None, timeout=5), (
            "the deck background never landed"
        )
        assert controller.background.video.loop is True, (
            "a deck background without a loop key must loop"
        )

        page_path = seed_page_settings("PageBg", {"background": {
            "overwrite": True, "show": True, "media-path": gif, "fps": 30,
        }})
        page = gl.page_manager.get_page(page_path, controller)
        controller.load_page(page, allow_reload=True)
        assert fixtures.wait_until(
            lambda: controller.background.video is not None and not controller.background.video.loop,
            timeout=5,
        ), (
            "a PAGE background without a loop key must still be one-shot -- that "
            "default is deliberate and separate from the deck's"
        )
        print("PASS: a deck background loops, a page background stays one-shot")
    finally:
        fixtures.teardown(controller)


def check_locked_deck_shows_config() -> None:
    """A deck reconnected while the session is locked shows its configured saver.

    No page load runs then, so nothing applies the config and the deck shows
    the bare state of the ScreenSaver class instead.
    """
    gif = make_gif("locked.gif")
    serial = "deck-defaults-locked"
    seed_deck_settings(serial, {"screensaver": {
        "enable": True, "media-path": gif, "time-delay": 60, "fps": 30, "brightness": 17,
    }})
    gl.screen_locked = True
    controller = fixtures.make_headless_controller(serial=serial)
    deck = fixtures.raw_deck(controller)
    try:
        assert controller.screen_saver.media_path == gif, (
            f"the locked deck shows {controller.screen_saver.media_path!r}, not its "
            f"configured screensaver media"
        )
        assert controller.screen_saver.brightness == 17, (
            f"the locked deck dims to {controller.screen_saver.brightness}, not the "
            f"configured 17"
        )
        assert controller.screen_saver.showing, "the locked deck did not show its screensaver"
        assert_landed_on_media_thread(controller, deck, 17, "deck connected while locked")
        print("PASS: a deck connected while locked shows its configured screensaver")
    finally:
        gl.screen_locked = False
        fixtures.teardown(controller)


def check_locked_deck_blanks_without_screensaver() -> None:
    """A deck with no screensaver configured still blanks when the session locks.

    It does so at the table brightness, not at one from nowhere.
    """
    serial = "deck-defaults-locked-bare"
    gl.screen_locked = True
    controller = fixtures.make_headless_controller(serial=serial)
    try:
        assert controller.screen_saver.showing, "the locked deck did not blank"
        assert controller.screen_saver.media_path is None
        assert controller.screen_saver.brightness == 30, (
            f"the blanked deck sits at {controller.screen_saver.brightness}, not the "
            f"table's screensaver brightness"
        )
        print("PASS: a locked deck with no screensaver configured still blanks")
    finally:
        gl.screen_locked = False
        fixtures.teardown(controller)


# The settings page. Opening it changes nothing

class _Widget:
    """Stand-in for a Gtk scale, switch, spin button, toggle group or expander.

    It holds a value, emits on every set, and refuses to disconnect a handler
    that is not connected, which makes a swallowed better_disconnect argument
    observable. Removal matches GTK and drops one matching handler per call.
    """

    def __init__(self, value=None):
        self.value = value
        self.handlers: list = []
        self.visible = None

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value
        self._emit()

    def get_active(self):
        return self.value

    def set_active(self, value):
        self.value = value
        self._emit()

    def get_active_name(self):
        return str(self.value)

    def set_active_name(self, value):
        self.value = value
        self._emit()

    def set_visible(self, value):
        self.visible = value

    def get_enable_expansion(self):
        return self.value

    def set_enable_expansion(self, value):
        self.value = value
        self._emit()

    def set_expanded(self, value):
        self.expanded = value

    def _emit(self):
        for handler in list(self.handlers):
            handler(self)

    def connect(self, signal, handler):
        self.handlers.append(handler)

    def disconnect_by_func(self, handler):
        if handler not in self.handlers:
            raise TypeError("nothing connected")
        self.handlers.remove(handler)


class _StubDeck:
    def is_touch(self):
        return False


class _StubSettingsPage:
    def __init__(self, controller):
        self.deck_controller = controller


class _StubController:
    """What the settings-page rows call back into."""

    def __init__(self):
        self.deck = _StubDeck()
        self.active_page = None
        self.screen_saver = _StubScreenSaver()
        self.brightness_calls: list = []
        self.rotation_calls: list = []
        self.load_background_calls: int = 0

    def set_brightness(self, value):
        self.brightness_calls.append(value)

    def set_rotation(self, value):
        self.rotation_calls.append(value)

    def load_background(self, page=None):
        self.load_background_calls += 1


class _StubScreenSaver:
    def __init__(self):
        self.calls: list = []

    def __getattr__(self, name):
        if not name.startswith("set_"):
            raise AttributeError(name)
        return lambda value: self.calls.append((name, value))


class _Row:
    """Base for the row stubs. Binds the real unbound method under test."""

    def __init__(self, serial, controller):
        self.deck_serial_number = serial
        self.settings_page = _StubSettingsPage(controller)
        self.on_map_tasks: list = []
        self.handler_calls: list = []

    def get_mapped(self):
        return True

    def disconnect_signals(self):
        pass

    def connect_signals(self):
        pass

    def set_thumbnail(self, path):
        pass


class BrightnessRow(_Row):
    def __init__(self, serial, controller):
        super().__init__(serial, controller)
        from src.windows.mainWindow.elements.DeckSettings.DeckGroup import Brightness
        self._real = Brightness
        self.scale = _Widget(0)
        # The real row is in this state by the time load_default runs. It defers
        # itself to map, which is after __init__ connected the handler.
        self.scale.connect("value-changed", self.on_value_changed)

    def on_value_changed(self, scale):
        # The GLib.idle_add callback, run inline. The harness has no main loop,
        # and the question is whether the load path reaches this at all.
        self.handler_calls.append(scale.get_value())
        self._real.on_value_changed_idle(self, scale)

    def load_default(self):
        self._real.load_default(self)


class SaturationRow(_Row):
    def __init__(self, serial, controller):
        super().__init__(serial, controller)
        from src.windows.mainWindow.elements.DeckSettings.DeckGroup import Saturation
        self._real = Saturation
        self.scale = _Widget(1.0)
        self.scale.connect("value-changed", self.on_value_changed)

    def on_value_changed(self, scale):
        # Recorded, not forwarded. The real handler is a 300 ms GLib debounce.
        self.handler_calls.append(scale.get_value())

    def load_default(self):
        self._real.load_default(self)


class RotationRow(_Row):
    def __init__(self, serial, controller):
        super().__init__(serial, controller)
        from src.windows.mainWindow.elements.DeckSettings.DeckGroup import Rotation
        self._real = Rotation
        self.toggle_group = _Widget(0)
        self.toggle_group.connect("notify::active", self.on_value_changed)

    def on_value_changed(self, *args):
        self.handler_calls.append(self.toggle_group.get_active_name())
        self._real.on_value_changed_idle(self)

    def load_default(self):
        self._real.load_default(self)


class ScreensaverRow(_Row):
    def __init__(self, serial, controller):
        super().__init__(serial, controller)
        from src.windows.mainWindow.elements.DeckSettings.DeckGroup import Screensaver
        self._real = Screensaver
        self.enable_switch = _Widget(False)
        self.config_box = _Widget()
        self.time_spinner = _Widget(0)
        self.loop_switch = _Widget(False)
        self.fps_spinner = _Widget(0)
        self.scale = _Widget(0)

    def page_overwrites_screensaver(self):
        return False

    def load_defaults(self):
        self._real.load_defaults(self)

    def toggle_enable(self, state):
        self._real.on_toggle_enable(self, self.enable_switch, state)

    def toggle_loop(self, state):
        self._real.on_toggle_loop(self, self.loop_switch, state)


class BackgroundRow(_Row):
    def __init__(self, serial, controller):
        super().__init__(serial, controller)
        from src.windows.mainWindow.elements.DeckSettings.BackgroundGroup import BackgroundMediaRow
        self._real = BackgroundMediaRow
        self.enable_switch = _Widget(False)
        self.config_box = _Widget()
        self.loop_switch = _Widget(False)
        self.fps_spinner = _Widget(0)
        self.extend_touchscreen_switch = _Widget(False)
        self.extend_touchscreen_box = _Widget()

    def load_defaults(self):
        self._real.load_defaults(self)

    def toggle_enable(self, state):
        self._real.on_toggle_enable(self, self.enable_switch, state)

    def toggle_loop(self, state):
        self._real.on_toggle_loop(self, self.loop_switch, state)


def open_every_row(serial, controller):
    rows = {
        "brightness": BrightnessRow(serial, controller),
        "saturation": SaturationRow(serial, controller),
        "rotation": RotationRow(serial, controller),
        "screensaver": ScreensaverRow(serial, controller),
        "background": BackgroundRow(serial, controller),
    }
    rows["brightness"].load_default()
    rows["saturation"].load_default()
    rows["rotation"].load_default()
    rows["screensaver"].load_defaults()
    rows["background"].load_defaults()
    return rows


def check_first_open_writes_moves_nothing() -> None:
    serial = "deck-defaults-first-open"
    fixtures._install_integration_globals()
    path = deck_settings_file(serial)
    before = file_fingerprint(path)
    controller = _StubController()

    rows = open_every_row(serial, controller)

    assert file_fingerprint(path) == before, (
        "opening the deck settings page rewrote the deck's configuration -- filling a "
        "missing key in and saving it pins today's default onto that deck forever"
    )
    assert controller.brightness_calls == [], (
        f"opening the settings page changed the physical brightness: {controller.brightness_calls}"
    )
    assert controller.rotation_calls == [], (
        f"opening the settings page rotated the deck: {controller.rotation_calls}"
    )
    assert controller.screen_saver.calls == [], (
        f"opening the settings page reconfigured the screensaver: {controller.screen_saver.calls}"
    )
    assert controller.load_background_calls == 0, "opening the settings page reloaded the background"
    for name, row in rows.items():
        assert row.handler_calls == [], (
            f"the {name} row's load path fired its saving handler: {row.handler_calls}"
        )
    print("PASS: opening the deck settings page writes nothing and moves nothing")


def check_fresh_page_shows_table() -> None:
    serial = "deck-defaults-fresh-ui"
    fixtures._install_integration_globals()
    rows = open_every_row(serial, _StubController())

    shown = {
        ("brightness", "value"): rows["brightness"].scale.get_value(),
        ("display", "saturation"): rows["saturation"].scale.get_value(),
        ("rotation", None): int(rows["rotation"].toggle_group.get_active_name()),
        ("screensaver", "enable"): rows["screensaver"].enable_switch.get_active(),
        ("screensaver", "loop"): rows["screensaver"].loop_switch.get_active(),
        ("screensaver", "fps"): rows["screensaver"].fps_spinner.get_value(),
        ("screensaver", "time-delay"): rows["screensaver"].time_spinner.get_value(),
        ("screensaver", "brightness"): rows["screensaver"].scale.get_value(),
        ("background", "enable"): rows["background"].enable_switch.get_active(),
        ("background", "loop"): rows["background"].loop_switch.get_active(),
        ("background", "fps"): rows["background"].fps_spinner.get_value(),
        ("background", "extend-to-touchscreen"):
            rows["background"].extend_touchscreen_switch.get_active(),
    }
    for key, value in shown.items():
        assert value == EXPECTED_DEFAULTS[key], (
            f"the settings page shows {key} as {value!r}, the deck uses "
            f"{EXPECTED_DEFAULTS[key]!r}"
        )
    print("PASS: a fresh deck's settings page shows the values the deck actually uses")


def check_persisted_value_shown_untouched() -> None:
    """A population that already holds a value keeps it, untouched.

    None of the resolutions above changes a byte on disk.
    """
    serial = "deck-defaults-persisted"
    stored = {
        "brightness": {"value": 50},
        "screensaver": {"loop": False, "brightness": 12},
        "background": {"enable": True, "loop": False},
        "rotation": 90,
    }
    seed_deck_settings(serial, stored)
    path = deck_settings_file(serial)
    before = file_fingerprint(path)

    rows = open_every_row(serial, _StubController())

    assert rows["brightness"].scale.get_value() == 50
    assert rows["screensaver"].loop_switch.get_active() is False
    assert rows["screensaver"].scale.get_value() == 12
    assert rows["background"].loop_switch.get_active() is False
    assert int(rows["rotation"].toggle_group.get_active_name()) == 90
    assert file_fingerprint(path) == before, "opening the page rewrote a configured deck"
    assert gl.settings_manager.get_deck_settings(serial) == stored, (
        "a configured deck's settings changed meaning"
    )
    print("PASS: a configured deck keeps its values, byte for byte")


def check_control_use_saves_sparsely() -> None:
    """The load paths stopped writing. The controls must not have."""
    serial = "deck-defaults-user-action"
    fixtures._install_integration_globals()
    controller = _StubController()

    ScreensaverRow(serial, controller).toggle_enable(True)
    assert gl.settings_manager.get_deck_settings(serial) == {"screensaver": {"enable": True}}, (
        f"enabling the screensaver saved "
        f"{gl.settings_manager.get_deck_settings(serial)!r} -- it must save that key "
        f"and no other, so every default still applies to this deck"
    )
    assert controller.screen_saver.calls == [("set_enable", True)], (
        f"enabling the screensaver did not reach the deck: {controller.screen_saver.calls}"
    )

    BackgroundRow(serial, controller).toggle_enable(True)
    assert gl.settings_manager.get_deck_settings(serial) == {
        "screensaver": {"enable": True}, "background": {"enable": True},
    }, (
        f"enabling the deck background saved "
        f"{gl.settings_manager.get_deck_settings(serial)!r} -- and it used to raise "
        f"here, having relied on the load path to create the section first"
    )
    assert controller.load_background_calls == 1, "enabling the background did not reload it"

    BackgroundRow(serial, controller).toggle_loop(False)
    assert gl.settings_manager.get_deck_settings(serial)["background"] == {
        "enable": True, "loop": False,
    }
    print("PASS: using a control still saves, and saves only what was chosen")


def check_reopened_row_one_handler() -> None:
    """A row that reloads must end with the handler count it started with.

    better_disconnect swallows what it cannot disconnect, so a wrong argument
    is silent. Every reload then adds another handler, until one load fires
    the saving handler as many times as the row has ever been loaded.
    """
    serial = "deck-defaults-reopen"
    seed_deck_settings(serial, {"rotation": 90})
    controller = _StubController()
    row = RotationRow(serial, controller)

    for _ in range(3):
        row.load_default()

    assert len(row.toggle_group.handlers) == 1, (
        f"reloading the rotation row left {len(row.toggle_group.handlers)} handlers "
        f"connected -- each of them saves and re-applies the rotation"
    )
    assert row.handler_calls == [], f"the reloads saved a rotation: {row.handler_calls}"
    assert controller.rotation_calls == [], (
        f"the reloads re-applied the rotation to the deck: {controller.rotation_calls}"
    )
    print("PASS: reloading a settings row keeps exactly one handler")


class _ScaleRow:
    """The ScaleRow of GtkHelper, wrapping the scale that carries the handler.

    Telling the row and the scale apart is what this stub exists for.
    """

    def __init__(self, value=0):
        self.scale = _Widget(value)

    def get_value(self):
        return self.scale.get_value()

    def set_value(self, value):
        self.scale.set_value(value)


# The row's own signal handlers, bound onto the stub, so a fired one reaches the
# real writer. That is what makes an accidental write observable.
_PAGE_SCREENSAVER_HANDLERS = (
    "on_overwrite_changed", "on_enable_changed", "on_delay_changed", "on_loop_changed",
    "on_fps_changed", "on_brightness_changed", "on_media_selector_click",
)


class PageScreensaverGroup:
    """Drive the real page-editor screensaver row through one selection cycle.

    The cycle is disconnect, load, connect.
    """

    def __init__(self, page_path):
        from src.windows.PageManager.elements.PageEditor import ScreensaverGroup
        self._real = ScreensaverGroup
        self.page_editor = _StubPageEditor(page_path)
        self.overwrite_expander = _Widget(False)
        self.enable_screensaver_toggle = _Widget(False)
        self.delay_spin = _Widget(0)
        self.loop_toggle = _Widget(False)
        self.fps_spin = _Widget(0)
        self.brightness_scale = _ScaleRow(0)
        self.media_selector_button = _Widget()
        self.updates = 0
        for name in _PAGE_SCREENSAVER_HANDLERS:
            setattr(self, name, MethodType(getattr(ScreensaverGroup, name), self))

    def set_thumbnail(self, path):
        pass

    def update_screensaver(self):
        self.updates += 1

    def load_for_page(self):
        self._real.disconnect_events(self)
        self._real.load_config_settings(self, self.page_editor.active_page_path)
        self._real.connect_events(self)


class _StubPageEditor:
    def __init__(self, page_path):
        self.active_page_path = page_path


def check_page_editor_adds_no_handlers() -> None:
    """Selecting page after page must not turn the page editor into a writer.

    The row reloads per selection, and a handler left connected across that
    reload saves the value it was just shown.
    """
    fixtures._install_integration_globals()
    page_path = seed_page_settings("PageEditorReopen", {"screensaver": {
        "overwrite": True, "enable": True, "time-delay": 60, "fps": 30,
    }})
    before = file_fingerprint(page_path)
    group = PageScreensaverGroup(page_path)

    for _ in range(3):
        group.load_for_page()

    widgets = {
        "overwrite_expander": group.overwrite_expander,
        "enable_screensaver_toggle": group.enable_screensaver_toggle,
        "delay_spin": group.delay_spin,
        "loop_toggle": group.loop_toggle,
        "fps_spin": group.fps_spin,
        "brightness_scale.scale": group.brightness_scale.scale,
        "media_selector_button": group.media_selector_button,
    }
    for name, widget in widgets.items():
        assert len(widget.handlers) == 1, (
            f"{name} ended a page selection with {len(widget.handlers)} handlers "
            f"connected -- the row disconnects something other than what it connects"
        )
    assert group.updates == 0, "loading the row reconfigured the deck's screensaver"
    assert file_fingerprint(page_path) == before, (
        "loading the page editor's screensaver row rewrote the page -- the row was "
        "saving back the values it had just displayed"
    )
    print("PASS: reloading the page editor's screensaver row writes nothing")


if __name__ == "__main__":
    fixtures.start_watchdog(180, label="scenario_deck_settings_defaults")

    check_table_matches_expectations()
    check_clamp_site_agrees()
    check_absent_keys_read_table()
    check_stored_values_win_and_survive()
    check_container_defaults_copied_per_read()
    check_writes_are_sparse_and_tripwired()
    check_scalar_in_section_slot()
    check_view_over_dict_cannot_save()

    check_fresh_deck_table_brightness()
    check_keyless_screensaver_loops_dims()
    check_deck_loops_page_does_not()
    check_locked_deck_shows_config()
    check_locked_deck_blanks_without_screensaver()

    check_first_open_writes_moves_nothing()
    check_fresh_page_shows_table()
    check_persisted_value_shown_untouched()
    check_control_use_saves_sparsely()
    check_reopened_row_one_handler()
    check_page_editor_adds_no_handlers()

    print("\nALL PASS: scenario_deck_settings_defaults")
