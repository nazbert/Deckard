"""
Regression scenario for #44: the window-based auto-page-switch machinery
must survive a deck without a page and an exception inside one watcher
iteration.

Two layers, matching the issue:

  1. WindowGrabber.on_active_window_changed dereferenced
     `deck_controller.active_page.json_path` with no None-guard --
     `active_page` is legitimately None mid-startup/hotplug, so a window
     change in that window raised AttributeError, aborting routing for
     every remaining deck (and, via layer 2, killing the watcher).

  2. X11's WatchForActiveWindowChange.run() had only the outer @log.catch:
     the first exception escaping the loop body ended the thread, killing
     window-based page switching until app restart. (Hyprland's socket
     listener already catches per-iteration and survives.)

Asserts: routing a window change with a pageless deck first in the list
does not raise and still auto-switches the healthy deck; the X11 watch
loop keeps routing after an iteration raised (both from the integration
poll and from the routing callback).
"""
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import threading
import time

import globals as gl
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber
from src.backend.WindowGrabber.Integrations.X11 import WatchForActiveWindowChange
from src.backend.WindowGrabber.Window import Window


# ===================================================================== #
# Stubs: exactly what on_active_window_changed dereferences
# ===================================================================== #

class StubPage:
    def __init__(self, json_path: str):
        self.json_path = json_path


class StubPageManager:
    def __init__(self, pages: dict[str, dict]):
        self._pages = pages

    def get_pages(self) -> list[str]:
        return list(self._pages)

    def get_auto_change_settings(self, path: str) -> dict:
        return self._pages.get(path, {})

    def get_page(self, path: str, deck_controller) -> StubPage:
        return StubPage(path)


class StubDeck:
    def __init__(self, serial: str):
        self._serial = serial

    def is_open(self) -> bool:
        return True

    def get_serial_number(self) -> str:
        return self._serial


class StubWGDeckController:
    def __init__(self, serial: str, active_page: StubPage | None,
                 page_auto_loaded: bool = False):
        self.deck = StubDeck(serial)
        self._serial = serial
        self.active_page = active_page
        self.page_auto_loaded = page_auto_loaded
        self.last_manual_loaded_page_path = None
        self.loaded_pages: list[str] = []

    def serial_number(self) -> str:
        return self._serial

    def load_page(self, page: StubPage, allow_reload: bool = True) -> None:
        self.loaded_pages.append(page.json_path)
        self.active_page = page


# ===================================================================== #
# Part 1: pageless deck must not abort routing for the other decks
# ===================================================================== #

def check_pageless_deck_routing() -> None:
    deck_manager = fixtures.install_stub_globals()

    # First in the list: a deck mid-startup/hotplug -- no page yet, but a
    # previous auto-load left page_auto_loaded set, so the pre-fix code
    # walks into the stay-on-page branch and derefs active_page.json_path.
    pageless = StubWGDeckController("HOTPLUG", active_page=None,
                                    page_auto_loaded=True)
    healthy = StubWGDeckController("GOOD",
                                   active_page=StubPage("/pages/other.json"))
    deck_manager.deck_controller.extend([pageless, healthy])

    gl.page_manager = StubPageManager({
        "/pages/match.json": {
            "wm-class": "firefox",
            "title": ".*",
            "enable": True,
            "decks": ["GOOD"],
        },
    })

    grabber = WindowGrabber.__new__(WindowGrabber)  # routing needs no integration

    try:
        grabber.on_active_window_changed(Window("firefox", "Mozilla Firefox"))
    except Exception as e:
        raise AssertionError(
            f"a deck without an active_page must be skipped, not raise: {e!r}"
        )

    assert healthy.loaded_pages == ["/pages/match.json"], (
        f"the healthy deck must still auto-switch when a pageless deck "
        f"precedes it, got {healthy.loaded_pages}"
    )
    assert pageless.loaded_pages == [], (
        "a pageless deck must not have pages loaded onto it by the watcher"
    )


# ===================================================================== #
# Part 1b: the None-guard itself, isolated from the per-deck try/except
# ===================================================================== #

def check_pageless_guard_is_a_clean_noop() -> None:
    """Part 1 routes through on_active_window_changed, whose per-deck
    try/except (the #104 restructure) also swallows the pre-fix
    active_page.json_path deref -- so Part 1 alone stays green even if the
    None-guard is deleted, and cannot red-test the guard on its own.

    This calls _apply_auto_change directly (the isolated per-deck body, with
    no surrounding try/except) so the guard's own effect is what is under
    test: with the guard, a pageless deck is a clean no-op; without it the
    deref raises straight out to here. Flips red iff the None-guard
    specifically is removed, independent of #104's isolation."""
    deck_manager = fixtures.install_stub_globals()

    pageless = StubWGDeckController("HOTPLUG", active_page=None,
                                    page_auto_loaded=True)
    deck_manager.deck_controller.append(pageless)

    gl.page_manager = StubPageManager({
        "/pages/match.json": {
            "wm-class": "firefox",
            "title": ".*",
            "enable": True,
            "decks": ["HOTPLUG"],
        },
    })

    grabber = WindowGrabber.__new__(WindowGrabber)

    try:
        # No try/except around this: only the None-guard can keep it from
        # raising AttributeError: 'NoneType' object has no attribute
        # 'json_path' (both at the match branch's active_page.json_path and
        # in the stay-on-page restore branch reached via page_auto_loaded).
        grabber._apply_auto_change(pageless, Window("firefox", "Mozilla Firefox"))
    except Exception as e:
        raise AssertionError(
            f"_apply_auto_change must skip a pageless deck without the "
            f"None-guard's protection, not raise: {e!r}"
        )

    assert pageless.loaded_pages == [], (
        "the None-guard must make a pageless deck a no-op, loading nothing"
    )


# ===================================================================== #
# Part 2: the X11 watch loop must survive raising iterations
# ===================================================================== #

class ScriptedX11:
    """Stands in for the X11 integration inside WatchForActiveWindowChange.
    get_active_window pops the next scripted item; an Exception item is
    raised (the real integration can raise out of any of its subprocess
    plumbing), a Window/None item is returned."""

    def __init__(self, script: list, window_grabber):
        self._script = list(script)
        self.window_grabber = window_grabber

    def get_active_window(self):
        if not self._script:
            return None
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingGrabber:
    """Records routed windows; raises on the marked one, like the real
    WindowGrabber did for a pageless deck (#44)."""

    def __init__(self, raise_on_class: str):
        self.calls: list[Window] = []
        self._raise_on_class = raise_on_class

    def on_active_window_changed(self, window: Window) -> None:
        self.calls.append(window)
        if window.wm_class == self._raise_on_class:
            raise AttributeError("'NoneType' object has no attribute 'json_path'")


def check_x11_watcher_survives() -> None:
    crasher = Window("crasher", "raises inside routing")
    survivor = Window("survivor", "must still be routed")

    recorder = RecordingGrabber(raise_on_class="crasher")
    scripted = ScriptedX11(
        script=[
            None,                            # consumed by __init__'s priming call
            crasher,                         # routing raises (pre-fix: thread dies)
            RuntimeError("xprop exploded"),  # poll itself raises
            survivor,                        # must still arrive post-fix
        ],
        window_grabber=recorder,
    )

    gl.threads_running = True
    watcher = WatchForActiveWindowChange(scripted)
    watcher.start()

    deadline = time.time() + 10.0
    while time.time() < deadline and len(recorder.calls) < 2:
        time.sleep(0.05)

    try:
        assert recorder.calls == [crasher, survivor], (
            f"the watch loop must keep routing after an iteration raised, "
            f"got {recorder.calls}"
        )
        assert watcher.is_alive(), (
            "the watcher thread must survive raising iterations"
        )
    finally:
        gl.threads_running = False
        watcher.join(timeout=3.0)


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_window_watcher_robustness")
    check_pageless_deck_routing()
    check_pageless_guard_is_a_clean_noop()
    check_x11_watcher_survives()
    print("PASS: scenario_window_watcher_robustness")


if __name__ == "__main__":
    main()
