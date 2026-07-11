"""
Shared fixtures for the single-writer migration harness
(docs/presenter-migration-plan.md §4 M0, §7 test matrix).

IMPORT THIS MODULE FIRST, before anything from `src` or `globals`, in every
test/scenario script. `globals.py` computes DATA_PATH from argparse at
*import* time (globals.py:39-57); this module sets sys.argv to point at a
fresh temp directory before its own `import globals`, which is the only way
to guarantee later code (SettingsManager, PageManagerBackend, FakeDeck, ...)
never resolves paths under the user's real
~/.var/app/com.core447.StreamController/data.

Two fixture tiers:

  * Unit tier (`make_stub_controller`) -- a `StubDeckController` exposing
    exactly what `MediaPlayerThread`'s judge and queues dereference
    (`DeckController.py` ~100-460): `_page_gen_lock`, `active_page`,
    `_page_load_generation`, `deck` (a `FaultyFakeDeck`), `serial_number()`,
    and the couple of attributes the loop's animation-tick branch reads
    (`background.video`, `inputs[Input.Key/Dial]`). No GTK, no real Page, no
    PluginManager -- scenarios drive `perform_media_player_tasks()` (or the
    task classes) directly.

  * Integration tier (`make_headless_controller`) -- a REAL `DeckController`
    over a `FaultyFakeDeck`, with a real `SettingsManager` /
    `PageManagerBackend` / `SignalManager` rooted at the temp data dir and a
    `StubDeckManager` standing in for `DeckManager` (the real one starts a
    `USBMonitor` + an `Xdp` portal probe -- unwanted/unavailable in a
    harness process). `gl.app` / `gl.app.main_win` are never set; every UI
    touch on the paths this harness exercises is `recursive_hasattr`-guarded.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# --- Isolated data dir, established before the first `import globals`. ---
DATA_DIR = tempfile.mkdtemp(prefix="sc_harness_")
sys.argv = [
    sys.argv[0] if sys.argv else "test",
    "--data", DATA_DIR,
    "--devel",
    "--skip-load-hardware-decks",
]


def _cleanup_data_dir():
    shutil.rmtree(DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_data_dir)

import globals as gl  # noqa: E402  (must follow the argv setup above)

if gl.DATA_PATH != DATA_DIR:
    raise RuntimeError(
        f"globals.DATA_PATH ({gl.DATA_PATH!r}) did not pick up the harness's "
        f"temp dir ({DATA_DIR!r}) -- something imported `globals` before "
        f"`fixtures` did in this process. Make sure test/scenario scripts "
        f"`import fixtures` first."
    )

_REAL_DATA_ROOT = os.path.expanduser("~/.var/app/com.core447.StreamController")
if gl.DATA_PATH.startswith(_REAL_DATA_ROOT):
    raise RuntimeError("refusing to run the harness against the real user data dir")

from src.backend.DeckManagement.InputIdentifier import Input  # noqa: E402
from faulty_fake_deck import FaultyFakeDeck  # noqa: E402


# ===================================================================== #
# Stub gl.* collaborators
# ===================================================================== #

class StubSettingsManager:
    """Minimal gl.settings_manager stand-in for the unit tier. Only the
    methods actually dereferenced on the harness's code paths are
    implemented: get_app_settings() (MediaPlayerThread init + the write
    task classes' error-swallow check) and get_deck_settings()/
    save_deck_settings() (FakeDeck.__init__/set_key_layout)."""

    def __init__(self, app_settings: dict = None):
        self._app_settings = app_settings if app_settings is not None else {}
        self._deck_settings: dict[str, dict] = {}

    def get_app_settings(self) -> dict:
        return self._app_settings

    def get_deck_settings(self, serial_number: str) -> dict:
        return self._deck_settings.setdefault(serial_number, {})

    def save_deck_settings(self, serial_number: str, settings: dict) -> None:
        self._deck_settings[serial_number] = settings


class StubDeckManager:
    """Stands in for gl.deck_manager / DeckController's `deck_manager`
    constructor arg. Only `.deck_controller` (iterated by several Page.*
    helpers and by the write-task error paths' remove_controller/
    connect_new_decks) is dereferenced on the paths this harness exercises.
    Tracks calls so scenarios can assert "no removal attempt" etc. (beta-
    resume graduated to the only mode in M2 -- there is no more flag to
    plumb through here.)"""

    def __init__(self):
        self.deck_controller: list = []
        self.remove_calls: list = []
        self.connect_calls: int = 0

    def remove_controller(self, deck_controller) -> None:
        self.remove_calls.append(deck_controller)
        if deck_controller in self.deck_controller:
            self.deck_controller.remove(deck_controller)

    def connect_new_decks(self) -> None:
        self.connect_calls += 1

    def close_all(self) -> None:
        """Mirrors the real DeckManager.close_all() (M1: submits a terminal
        ClearAndCloseMsg per controller, then joins each media thread with a
        bounded timeout) -- see DeckManager.py's close_all for the
        authoritative version this must stay in sync with."""
        from src.backend.DeckManagement.DeckController import ClearAndCloseMsg

        pending_joins = []
        for controller in list(self.deck_controller):
            if controller.deck is None:
                continue
            if not controller.deck.is_open():
                continue
            media_player = getattr(controller, "media_player", None)
            if media_player is None:
                try:
                    controller.deck.close()
                except Exception:
                    pass
                continue
            media_player.submit_control(ClearAndCloseMsg())
            pending_joins.append(controller)

        for controller in pending_joins:
            controller.media_player.stop(timeout=2.0)


def install_stub_globals(app_settings: dict = None) -> StubDeckManager:
    """Unit tier: installs a StubSettingsManager + StubDeckManager on `gl`.
    Returns the StubDeckManager for assertions (e.g. `.remove_calls`)."""
    gl.settings_manager = StubSettingsManager(app_settings=app_settings)
    deck_manager = StubDeckManager()
    gl.deck_manager = deck_manager
    return deck_manager


# ===================================================================== #
# Unit tier
# ===================================================================== #

class StubBackground:
    def __init__(self):
        self.video = None


class StubInput:
    """Minimal ControllerKey/ControllerTouchScreen stand-in for the
    resume-repaint / write-result scenarios (plan §4 M2): exposes exactly
    the dedup hash attrs _reset_dedup_hashes touches and an update() that
    unconditionally enqueues a fresh image/touchscreen task -- the unit tier
    doesn't render real content, so there's nothing to dual-hash-skip
    against here (that guard is exercised at the integration tier instead,
    see scenario_dedup_coherence.py)."""

    def __init__(self, controller: "StubDeckController", index: int, touchscreen: bool = False):
        self.controller = controller
        self.index = index
        self.touchscreen = touchscreen
        self._last_img_hash = None
        self._last_enqueued_hash = None

    def update(self) -> None:
        img = make_native_image(fill=self.index)
        img_hash = hash(img)
        media_player = self.controller.media_player
        if self.touchscreen:
            media_player.add_touchscreen_task(
                img, page=self.controller.active_page,
                config_gen=self.controller._page_load_generation,
                controller_touchscreen=self, img_hash=img_hash,
            )
        else:
            media_player.add_image_task(
                self.index, img, page=self.controller.active_page,
                config_gen=self.controller._page_load_generation,
                controller_key=self, img_hash=img_hash,
            )


class StubDeckController:
    """Unit-tier stand-in exposing exactly what MediaPlayerThread's judge and
    queues dereference: _page_gen_lock, active_page, _page_load_generation,
    deck, serial_number(), background.video, inputs[Input.Key/Dial/
    Touchscreen], and get_touchscreen_image_size() (read by
    MediaPlayerSetTouchscreenImageTask.run). Also mirrors the write-result/
    resume-repaint methods DeckController gained in M2
    (_on_write_result/_schedule_full_repaint/_reset_dedup_hashes) -- the
    task classes and MediaPlayerThread.check_resume_gap call these by name,
    so the stub must provide equivalents. Keep these in sync with
    DeckController.py; `repaint_count` is a test-only counter with no real
    equivalent."""

    def __init__(self, deck=None, serial: str = "stub-serial-1", n_keys: int = 0, has_touchscreen: bool = False):
        self.deck = deck if deck is not None else FaultyFakeDeck(serial_number=serial)
        self._serial = serial
        self.active_page = object()  # opaque "page" sentinel; judged by `is`
        self._page_load_generation = 0
        self._page_gen_lock = threading.Lock()
        self.background = StubBackground()
        self.inputs = {
            Input.Key: [StubInput(self, i) for i in range(n_keys)],
            Input.Dial: [],
            Input.Touchscreen: [StubInput(self, 0, touchscreen=True)] if has_touchscreen else [],
        }
        self.media_player = None  # set by make_stub_controller()
        self._had_write_failure = False
        self._full_repaint_pending = False
        self._last_full_repaint_ts = 0.0
        self.repaint_count = 0

    def serial_number(self) -> str:
        return self._serial

    def get_touchscreen_image_size(self):
        return (800, 100)

    def is_visual(self) -> bool:
        return self.deck.is_visual()

    def _write_blank_frames(self) -> None:
        """Unit-tier stand-in for DeckController._write_blank_frames (M1):
        writes a deterministic blank marker directly to every key +
        touchscreen. MediaPlayerThread's Clear/ClearAndClose control-message
        handling calls this by name, so the stub must provide it -- real
        image encoding doesn't matter here, only journal shape/ordering."""
        if not self.is_visual():
            return
        for i in range(self.deck.key_count()):
            self.deck.set_key_image(i, b"\x00" * 16)
        if self.deck.is_touch():
            size = self.get_touchscreen_image_size()
            self.deck.set_touchscreen_image(b"\x00" * 16, x_pos=0, y_pos=0, width=size[0], height=size[1])

    def _reset_dedup_hashes(self) -> None:
        """Mirrors DeckController._reset_dedup_hashes (M2)."""
        for key in self.inputs.get(Input.Key, []):
            key._last_img_hash = None
            key._last_enqueued_hash = None
        for touchscreen in self.inputs.get(Input.Touchscreen, []):
            touchscreen._last_img_hash = None
            touchscreen._last_enqueued_hash = None

    def update_all_inputs(self, gen=None) -> None:
        """Mirrors DeckController.update_all_inputs's key/touchscreen
        fan-out (the unit tier has no background-video branch to skip --
        see StubBackground)."""
        for t in self.inputs:
            for i in self.inputs[t]:
                i.update()

    def _schedule_full_repaint(self) -> None:
        """Mirrors DeckController._schedule_full_repaint (M2): arms the
        pending flag; _run_pending_repaint fires it (deferred, not dropped,
        on rate-limit)."""
        self._full_repaint_pending = True

    def _run_pending_repaint(self) -> bool:
        """Mirrors DeckController._run_pending_repaint (M2)."""
        if not self._full_repaint_pending:
            return False
        now = time.time()
        if now - self._last_full_repaint_ts < 2.0:
            return False
        self._full_repaint_pending = False
        self._last_full_repaint_ts = now
        self._reset_dedup_hashes()
        self.update_all_inputs()
        self.repaint_count += 1
        return True

    def _on_write_result(self, success: bool) -> None:
        """Mirrors DeckController._on_write_result (M2): every failure arms
        the pending repaint; the loop's 2s cadence retries until one lands."""
        if success:
            if self._had_write_failure:
                self._had_write_failure = False
        else:
            self._had_write_failure = True
            self._full_repaint_pending = True

    def new_page(self):
        """A fresh opaque page sentinel, distinct from .active_page."""
        return object()

    def bump_generation(self) -> int:
        with self._page_gen_lock:
            self._page_load_generation += 1
            return self._page_load_generation


def make_stub_controller(serial: str = "stub-serial-1", n_keys: int = 0, has_touchscreen: bool = False):
    """Builds a StubDeckController over a fresh FaultyFakeDeck and wires a
    real MediaPlayerThread to it. The thread is constructed but NOT started
    -- unit scenarios drive perform_media_player_tasks() (or the task
    classes) directly so they stay deterministic. Returns
    (controller, media_player, deck_manager_stub)."""
    from src.backend.DeckManagement.DeckController import MediaPlayerThread

    deck_manager = install_stub_globals()
    controller = StubDeckController(serial=serial, n_keys=n_keys, has_touchscreen=has_touchscreen)
    media_player = MediaPlayerThread(deck_controller=controller)
    controller.media_player = media_player
    return controller, media_player, deck_manager


def make_native_image(size=(72, 72), fill: int = 0) -> bytes:
    """Cheap stand-in for an encoded key image -- the write path only cares
    that it's a bytes-like payload it can hash, never that it's real JPEG."""
    return bytes([fill]) * (size[0] * size[1])


# ===================================================================== #
# Integration tier
# ===================================================================== #

def make_test_png(path: str, size=(72, 72), color=(255, 0, 0)) -> str:
    """Writes a tiny solid-color PNG to `path` (used as screensaver/page
    media in integration scenarios) and returns the path."""
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def seed_page_with_background(page_name: str, media_path: str, data_dir: str = None) -> str:
    """Like seed_page(), but the page overwrites the deck background with
    `media_path` -- used to make two pages visually (and hash-) distinct in
    the journal for switch-storm-style scenarios."""
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {}, "dials": {}, "touchscreens": {},
            "settings": {
                "background": {
                    "overwrite": True,
                    "show": True,
                    "media-path": media_path,
                    "loop": False,
                    "fps": 30,
                },
            },
        }, f)
    return path


def seed_page_with_background_and_screensaver(
    page_name: str, media_path: str, screensaver_media_path: str,
    screensaver_time_delay: int = 60, data_dir: str = None,
) -> str:
    """Like seed_page_with_background(), but ALSO persists the screensaver's
    media/enable/time-delay on the page itself. Needed for any scenario that
    calls hide() more than once: DeckController.load_page() always calls
    load_screensaver(page) (DeckController.py ~1042-1060), which re-reads the
    screensaver config from the page's (or deck's) settings on every single
    load and overwrites ScreenSaver.media_path/enable/time_delay with
    whatever it finds there -- `None`/disabled if the page never persisted
    anything. Since hide()'s phase 3 IS a load_page() call, a scenario that
    calls ScreenSaver.set_media_path() once up front and then hide()s more
    than once will have that path silently reset to None by the very next
    hide(), and a later show() would then paint a blank background instead
    of the intended media -- not a concurrency bug, just this reload
    contract, which real usage never notices because the persisted page
    settings ARE the source of truth show() lands on every reload."""
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    with open(path, "w") as f:
        json.dump({
            "keys": {}, "dials": {}, "touchscreens": {},
            "settings": {
                "background": {
                    "overwrite": True,
                    "show": True,
                    "media-path": media_path,
                    "loop": False,
                    "fps": 30,
                },
                "screensaver": {
                    "overwrite": True,
                    "enable": True,
                    "media-path": screensaver_media_path,
                    "time-delay": screensaver_time_delay,
                    "loop": False,
                    "fps": 30,
                    "brightness": 30,
                },
            },
        }, f)
    return path


def seed_page(page_name: str = "Main", data_dir: str = None) -> str:
    """Writes a minimal, action-free page JSON to the temp data dir's pages
    folder (empty keys/dials/touchscreens -- load_action_objects() then never
    touches gl.plugin_manager, which this harness intentionally never
    installs). Idempotent; returns the page's path."""
    data_dir = data_dir if data_dir is not None else gl.DATA_PATH
    pages_dir = os.path.join(data_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{page_name}.json")
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump({"keys": {}, "dials": {}, "touchscreens": {}}, f)
    return path


_integration_globals_installed = False


def _install_integration_globals() -> None:
    """Populates the minimum gl.* graph DeckController.__init__/load_page
    dereference. Idempotent -- safe across multiple headless controllers in
    one process (see scenario_two_decks)."""
    global _integration_globals_installed
    if _integration_globals_installed:
        return

    from src.backend.SettingsManager import SettingsManager
    from src.backend.PageManagement.PageManagerBackend import PageManagerBackend
    from src.Signals.SignalManager import SignalManager

    gl.settings_manager = SettingsManager()
    gl.signal_manager = SignalManager()
    gl.page_manager = PageManagerBackend(gl.settings_manager)
    gl.deck_manager = StubDeckManager()
    _integration_globals_installed = True


def make_headless_controller(serial: str = "headless-1", key_layout=None, page_name: str = "Main"):
    """Integration tier: a REAL DeckController over a FaultyFakeDeck, no GTK
    main loop, no hardware. Seeds one empty page on disk first so
    load_default_page() (run at the end of DeckController.__init__) has
    something to load."""
    _install_integration_globals()
    seed_page(page_name)

    from src.backend.DeckManagement.DeckController import DeckController

    deck = FaultyFakeDeck(serial_number=serial, deck_type="Fake Deck", key_layout=key_layout)
    controller = DeckController(gl.deck_manager, deck)
    gl.deck_manager.deck_controller.append(controller)
    return controller


def raw_deck(controller) -> FaultyFakeDeck:
    """Unwraps the real BetterDeck that DeckController.__init__ installs
    around `controller.deck` (integration tier only -- the unit tier's
    StubDeckController.deck IS the FaultyFakeDeck already) and returns the
    underlying FaultyFakeDeck, for journal/fail_next/fire_*_event access."""
    return controller.deck.deck


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Polls `predicate()` until it's truthy or `timeout` elapses. Returns
    whether it became true -- used instead of a fixed sleep so scenarios
    settle as fast as the media thread actually runs, not slower."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def start_watchdog(seconds: float, label: str = "scenario") -> None:
    """Starts a daemon thread that hard-exits the process (os._exit) if it's
    still alive after `seconds` -- a deadlock in the code under test must
    fail loud and fast instead of hanging (docs/presenter-migration-plan.md
    §4 M3's screensaver-storm/blocked-plugin-transition scenarios need this
    explicitly; run_all.py's subprocess timeout would eventually catch a
    hang too, but only after its own, longer, per-scenario timeout, and with
    no specific message)."""
    def _fire():
        time.sleep(seconds)
        print(f"FAIL: {label} watchdog fired after {seconds}s -- likely deadlock", flush=True)
        os._exit(1)

    t = threading.Thread(target=_fire, name=f"{label}-watchdog", daemon=True)
    t.start()


def teardown(controller) -> None:
    """Bounded, best-effort shutdown mirroring DeckManager.remove_controller
    (minus the UI-stack removal, which is recursive_hasattr-guarded out
    anyway since gl.app is never set here)."""
    try:
        if controller in gl.deck_manager.deck_controller:
            gl.deck_manager.deck_controller.remove(controller)
    except Exception:
        pass
    try:
        controller.keep_actions_ticking = False
        controller.delete()
    except Exception:
        pass
    tick_thread = getattr(controller, "tick_thread", None)
    if tick_thread is not None:
        tick_thread.join(timeout=2.0)
