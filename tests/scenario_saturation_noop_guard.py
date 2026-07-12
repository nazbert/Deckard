"""
Unit-tier scenario for issue #51: opening deck settings with saturation !=
1.0 must not trigger a page reload.

The settings pane's Saturation row (src/windows/mainWindow/elements/
DeckSettings/DeckGroup.py) defers load_default() to "map", which runs AFTER
value-changed is connected -- so set_value(<stored factor>) fires the
handler, and DeckController.set_display_saturation() used to apply
unconditionally: a full load_page(..., allow_reload=True) ~300ms after the
pane maps (visible flicker + full reload cost on every settings open), for
a value that didn't change.

Drives the REAL DeckController.set_display_saturation (unbound, on a stub
exposing exactly what it reads/calls: display_saturation, get_deck_settings,
deck.get_serial_number, active_page, load_page) and asserts the same-value
short-circuit:

  (a) echoing the current factor is a complete no-op: no load_page call and
      no settings write (the settings-open case).
  (b) sub-rounding jitter (the method rounds to 2 decimals; the Gtk.Scale
      shows 2 digits) counts as the same value.
  (c) a real change still persists + reloads exactly once, and updates the
      cached factor.
  (d) a real change with no active page persists without reloading.
"""
import fixtures

import globals as gl
from src.backend.DeckManagement.DeckController import DeckController


class _StubDeck:
    def __init__(self, serial: str = "sat-noop-1"):
        self._serial = serial

    def get_serial_number(self) -> str:
        return self._serial


class _StubSetterController:
    """Exactly the surface DeckController.set_display_saturation touches."""

    def __init__(self, current: float, active_page=None):
        self.display_saturation = current
        self.deck = _StubDeck()
        self.active_page = active_page
        self.load_page_calls: list = []
        self._settings: dict = {"display": {"saturation": current}}

    def get_deck_settings(self) -> dict:
        return self._settings

    def load_page(self, page, allow_reload: bool = False) -> None:
        self.load_page_calls.append((page, allow_reload))


class _CountingSettingsManager(fixtures.StubSettingsManager):
    def __init__(self):
        super().__init__()
        self.save_calls: list = []

    def save_deck_settings(self, serial_number: str, settings: dict) -> None:
        self.save_calls.append((serial_number, settings))
        super().save_deck_settings(serial_number, settings)


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_saturation_noop_guard")
    fixtures.install_stub_globals()
    settings_manager = _CountingSettingsManager()
    gl.settings_manager = settings_manager

    page = object()

    # (a) settings-open echo: the pane re-emits the loaded value.
    c = _StubSetterController(current=1.3, active_page=page)
    DeckController.set_display_saturation(c, 1.3)
    assert c.load_page_calls == [], (
        f"echoing the current factor must not reload the page, got {c.load_page_calls}"
    )
    assert settings_manager.save_calls == [], (
        "echoing the current factor must not rewrite deck settings"
    )
    assert c.display_saturation == 1.3

    # (b) sub-rounding jitter is the same value (the method rounds to 2
    # decimals before comparing/persisting).
    DeckController.set_display_saturation(c, 1.3000004)
    assert c.load_page_calls == [] and settings_manager.save_calls == [], (
        "sub-rounding jitter must hit the same-value short-circuit"
    )

    # (c) a real change still applies exactly once.
    DeckController.set_display_saturation(c, 1.4)
    assert c.load_page_calls == [(page, True)], (
        f"a real change must reload the active page once (allow_reload=True), "
        f"got {c.load_page_calls}"
    )
    assert len(settings_manager.save_calls) == 1
    assert c.display_saturation == 1.4
    assert c._settings["display"]["saturation"] == 1.4

    # ...and echoing the NEW value is again a no-op.
    DeckController.set_display_saturation(c, 1.4)
    assert len(c.load_page_calls) == 1 and len(settings_manager.save_calls) == 1

    # (d) a real change with no active page persists without reloading.
    c_no_page = _StubSetterController(current=1.0, active_page=None)
    DeckController.set_display_saturation(c_no_page, 1.2)
    assert c_no_page.load_page_calls == []
    assert c_no_page.display_saturation == 1.2
    assert len(settings_manager.save_calls) == 2

    print("PASS: scenario_saturation_noop_guard")


if __name__ == "__main__":
    main()
