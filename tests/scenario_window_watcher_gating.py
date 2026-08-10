"""
Scenario: the active-window watcher runs only while a page actually wants it.

Watching the foreground window costs a permanent background poll on most
desktops -- the X11 integration spawns several xprop processes every 200 ms,
the KDE and Sway ones shell out just as often -- and that cost buys nothing
unless some page carries an enabled window auto-change rule. WindowGrabber
therefore gates the watcher on exactly that: nothing starts at boot without a
rule, the first rule starts it, the last rule's removal stops and reaps it.

Asserts, against a stub integration injected through the pure
`select_integration_class` selector:

  * zero rules at boot -- no integration is even CONSTRUCTED (the probe/proxy
    an integration builds in __init__ is part of the cost being avoided) and
    nothing is watching;
  * a rule already on disk at boot -- watching, with a live watcher thread;
  * the first rule added at runtime, through the page-editor write path
    (overwrite_auto_change_settings) -- starts the watcher;
  * the last rule switched off, and separately the last rule's page deleted
    -- stops the watcher and reaps its thread within a bounded wait;
  * start and stop are both idempotent, and a stopped watcher restarts;
  * one-shot window queries still work while gated off -- the page editor's
    matching-window list is what the user reaches BEFORE the first rule
    exists, so it must not depend on the watcher running.
"""
import fixtures  # noqa: F401  (must be imported first: isolates DATA_PATH)

import json
import os
import threading
import time

import globals as gl
import src.backend.WindowGrabber.WindowGrabber as window_grabber_module
from src.backend.WindowGrabber.Integration import Integration
from src.backend.WindowGrabber.Window import Window
from src.backend.WindowGrabber.WindowGrabber import WindowGrabber

REAP_TIMEOUT_S = 5.0


# ===================================================================== #
# Stub integration: a real thread, so "reaped" means something
# ===================================================================== #

class StubIntegration(Integration):
    """Holds an actual watcher thread, like every real integration does, so
    the stop path is exercised end to end rather than just counted."""

    instances: list["StubIntegration"] = []

    def __init__(self, window_grabber):
        super().__init__(window_grabber=window_grabber)
        self.start_calls = 0
        self.stop_calls = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        StubIntegration.instances.append(self)

    # -- Integration contract ---------------------------------------- #

    def start_watching(self) -> None:
        self.start_calls += 1
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="StubActiveWindowWatcher", daemon=True
        )
        self._thread.start()

    def stop_watching(self) -> None:
        self.stop_calls += 1
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=REAP_TIMEOUT_S)

    def get_all_windows(self) -> list[Window]:
        return [Window("firefox", "Mozilla Firefox")]

    # -- test helpers ------------------------------------------------- #

    def _loop(self) -> None:
        while not self._stop_event.wait(0.02):
            pass

    @property
    def watcher_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()


def _install_stub_selector() -> None:
    """Replaces the session sniffing with a fixed answer. `select_integration
    _class` is a pure function of the environment, which is what makes this a
    one-liner instead of an environment-variable dance."""
    window_grabber_module.select_integration_class = (
        lambda environment_components, server: StubIntegration
    )


def _fresh_grabber() -> WindowGrabber:
    StubIntegration.instances = []
    grabber = WindowGrabber()
    gl.window_grabber = grabber
    return grabber


# ===================================================================== #
# Page fixtures
# ===================================================================== #

def _write_page(name: str, auto_change: dict | None = None) -> str:
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    path = os.path.join(pages_dir, f"{name}.json")
    page: dict = {"keys": {}, "dials": {}, "touchscreens": {}}
    if auto_change is not None:
        page["settings"] = {"auto-change": auto_change}
    with open(path, "w") as page_file:
        json.dump(page, page_file)
    return path


def _clear_pages() -> None:
    pages_dir = os.path.join(gl.DATA_PATH, "pages")
    if not os.path.isdir(pages_dir):
        return
    for entry in os.listdir(pages_dir):
        if entry.endswith(".json"):
            os.remove(os.path.join(pages_dir, entry))


def _enabled_rule(wm_class: str = "firefox") -> dict:
    return {
        "enable": True,
        "wm-class": wm_class,
        "title": ".*",
        "stay-on-page": True,
        "decks": ["SERIAL"],
    }


def _wait_until(predicate, timeout: float = REAP_TIMEOUT_S) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ===================================================================== #
# The rule query itself
# ===================================================================== #

def check_rule_query() -> None:
    _clear_pages()
    page_manager = gl.page_manager

    assert page_manager.any_auto_change_rule_enabled() is False, (
        "no pages at all means no rule"
    )

    _write_page("Plain")
    _write_page("NoSection", auto_change=None)
    assert page_manager.any_auto_change_rule_enabled() is False, (
        "a page without an auto-change section is not a rule"
    )

    _write_page("Disabled", auto_change={"enable": False, "wm-class": "firefox",
                                         "title": ".*", "decks": ["SERIAL"]})
    assert page_manager.any_auto_change_rule_enabled() is False, (
        "a rule that is switched off must not arm the watcher -- a half-typed "
        "regex left behind in the page editor is the common case"
    )

    _write_page("Armed", auto_change=_enabled_rule())
    assert page_manager.any_auto_change_rule_enabled() is True, (
        "one enabled rule anywhere is enough"
    )

    print("ok: the rule query reads enable flags off the pages")


# ===================================================================== #
# Boot gating
# ===================================================================== #

def check_no_rules_starts_nothing() -> None:
    _clear_pages()
    _write_page("Plain")
    _write_page("Disabled", auto_change={"enable": False, "wm-class": "firefox",
                                         "title": ".*", "decks": ["SERIAL"]})

    grabber = _fresh_grabber()

    assert StubIntegration.instances == [], (
        "with no enabled rule the integration must not even be constructed: "
        "building one probes for a helper binary or opens a D-Bus proxy, and "
        f"the real ones start polling from there -- got "
        f"{len(StubIntegration.instances)} instance(s)"
    )
    assert grabber.is_watching is False, (
        "nothing may watch the active window when no page asks for it"
    )


def check_rule_at_boot_starts_the_watcher() -> None:
    _clear_pages()
    _write_page("Armed", auto_change=_enabled_rule())

    grabber = _fresh_grabber()

    assert len(StubIntegration.instances) == 1, (
        f"a rule on disk at boot must build the integration, got "
        f"{len(StubIntegration.instances)} instance(s)"
    )
    integration = StubIntegration.instances[0]
    assert grabber.is_watching is True, "a rule on disk at boot must arm the watcher"
    assert integration.start_calls == 1, (
        f"the watcher must be started exactly once at boot, got {integration.start_calls}"
    )
    assert integration.watcher_alive, "the watcher thread must actually be running"

    grabber.stop_watching()


# ===================================================================== #
# Dynamic start / stop through the page-settings write path
# ===================================================================== #

def check_first_rule_starts_and_last_rule_stops() -> None:
    _clear_pages()
    page_path = _write_page("Editable")
    grabber = _fresh_grabber()

    assert grabber.is_watching is False, "no rule yet: nothing to watch for"

    # The page editor's enable toggle lands here -- the write site, not the
    # widget, is what the gating hangs off, so any other caller (the DBus
    # API, a plugin, a page import) re-gates identically.
    gl.page_manager.overwrite_auto_change_settings(path=page_path, enable=True)

    assert grabber.is_watching is True, (
        "enabling the first rule must start the watcher without a restart"
    )
    assert len(StubIntegration.instances) == 1, (
        f"the integration is built on the first start, once, got "
        f"{len(StubIntegration.instances)}"
    )
    integration = StubIntegration.instances[0]
    assert integration.watcher_alive, "the started watcher must have a live thread"

    # ... and switching the last one back off must reap it.
    gl.page_manager.overwrite_auto_change_settings(path=page_path, enable=False)

    assert grabber.is_watching is False, (
        "disabling the last rule must stop the watcher"
    )
    assert integration.stop_calls == 1, (
        f"the integration must be told to stop exactly once, got {integration.stop_calls}"
    )
    assert _wait_until(lambda: not integration.watcher_alive), (
        "stopping must REAP the watcher thread, not just stop routing to it"
    )


def check_second_rule_does_not_restart_and_last_removal_stops() -> None:
    _clear_pages()
    first = _write_page("First", auto_change=_enabled_rule("firefox"))
    second = _write_page("Second")
    grabber = _fresh_grabber()
    integration = StubIntegration.instances[0]

    assert grabber.is_watching is True
    assert integration.start_calls == 1

    # A second rule while one is already armed changes nothing about the
    # watcher; the gate is "any", not a count.
    gl.page_manager.overwrite_auto_change_settings(path=second, enable=True)
    assert integration.start_calls == 1, (
        f"a second rule must not restart the watcher, got {integration.start_calls} starts"
    )
    assert grabber.is_watching is True

    # Dropping one of two rules leaves the other armed.
    gl.page_manager.overwrite_auto_change_settings(path=first, enable=False)
    assert grabber.is_watching is True, (
        "the watcher must keep running while any other rule is still enabled"
    )
    assert integration.stop_calls == 0, (
        f"nothing may stop while a rule remains, got {integration.stop_calls} stops"
    )

    # Deleting the page that carries the last rule removes the rule with it.
    gl.page_manager.remove_page(second)
    assert grabber.is_watching is False, (
        "deleting the page that carried the last rule must stop the watcher"
    )
    assert _wait_until(lambda: not integration.watcher_alive), (
        "the watcher thread must be reaped when the last rule's page is deleted"
    )


# ===================================================================== #
# Idempotence and restart
# ===================================================================== #

def check_start_stop_idempotence() -> None:
    _clear_pages()
    _write_page("Armed", auto_change=_enabled_rule())
    grabber = _fresh_grabber()
    integration = StubIntegration.instances[0]

    assert integration.start_calls == 1

    grabber.start_watching()
    grabber.start_watching()
    assert integration.start_calls == 1, (
        f"start must be a no-op while watching, got {integration.start_calls} starts"
    )
    assert len(StubIntegration.instances) == 1, (
        "a redundant start must not build a second integration"
    )
    assert integration.watcher_alive, "the one watcher thread must still be running"

    grabber.stop_watching()
    grabber.stop_watching()
    assert integration.stop_calls == 1, (
        f"stop must be a no-op when not watching, got {integration.stop_calls} stops"
    )
    assert grabber.is_watching is False
    assert _wait_until(lambda: not integration.watcher_alive)

    # Restart after a stop: the same integration is reused, with a new thread
    # (a Thread object cannot be started twice).
    grabber.start_watching()
    assert grabber.is_watching is True
    assert integration.start_calls == 2
    assert len(StubIntegration.instances) == 1, (
        "restarting must reuse the integration, not build another"
    )
    assert integration.watcher_alive, "a restarted watcher needs a live thread again"

    grabber.stop_watching()


# ===================================================================== #
# One-shot queries must survive the gate
# ===================================================================== #

def check_window_query_works_while_gated_off() -> None:
    _clear_pages()
    _write_page("Plain")
    grabber = _fresh_grabber()

    assert StubIntegration.instances == []

    # The page editor's matching-window list, reached while setting up the
    # very first rule -- i.e. always while the watcher is off.
    windows = grabber.get_all_matching_windows(class_regex="firefox", title_regex=".*")

    assert [window.wm_class for window in windows] == ["firefox"], (
        f"a one-shot window query must work with the watcher gated off, got {windows}"
    )
    assert len(StubIntegration.instances) == 1, (
        "the query must build the integration on demand"
    )
    assert grabber.is_watching is False, (
        "a one-shot query must NOT start the watcher -- that would reintroduce "
        "the polling for anyone who merely opens the page editor"
    )
    assert StubIntegration.instances[0].start_calls == 0
    assert not StubIntegration.instances[0].watcher_alive


def check_reset_rebuilds_and_regates() -> None:
    """Onboarding installs the GNOME shell extension mid-session and resets
    the grabber so the stale D-Bus proxy is rebuilt. The reset must reap the
    old watcher and come back armed, because the rules -- not the reset --
    decide whether anything watches."""
    _clear_pages()
    _write_page("Armed", auto_change=_enabled_rule())
    grabber = _fresh_grabber()
    first = StubIntegration.instances[0]
    assert grabber.is_watching is True

    grabber.reset_integration()

    assert len(StubIntegration.instances) == 2, (
        f"the reset must build a fresh integration, got "
        f"{len(StubIntegration.instances)} in total"
    )
    assert first.stop_calls == 1, "the superseded integration must be stopped"
    assert _wait_until(lambda: not first.watcher_alive), (
        "the superseded integration's watcher thread must be reaped, not leaked"
    )

    second = StubIntegration.instances[1]
    assert grabber.is_watching is True, (
        "the rule still exists, so the rebuilt integration must be watching"
    )
    assert second.watcher_alive

    grabber.stop_watching()

    # ... and with no rule, a reset leaves nothing behind at all.
    _clear_pages()
    _write_page("Plain")
    grabber.reset_integration()
    assert grabber.is_watching is False
    assert len(StubIntegration.instances) == 2, (
        "a reset with no rule must not build an integration speculatively"
    )


def main() -> None:
    fixtures.start_watchdog(90, label="scenario_window_watcher_gating")
    _install_stub_selector()
    fixtures._install_integration_globals()

    check_rule_query()
    check_no_rules_starts_nothing()
    check_rule_at_boot_starts_the_watcher()
    check_first_rule_starts_and_last_rule_stops()
    check_second_rule_does_not_restart_and_last_removal_stops()
    check_start_stop_idempotence()
    check_window_query_works_while_gated_off()
    check_reset_rebuilds_and_regates()

    print("PASS: scenario_window_watcher_gating")


if __name__ == "__main__":
    main()
