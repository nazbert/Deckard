"""
Scenario: reload_similar_pages(identifier=None) must reload each sibling
controller's OWN Page object, not the caller's (issue #55 -- Page.py passed
`self` to the other controller's load_page, loading THIS controller's Page
onto other decks: cross-deck page bleed).

Also: get_pages_with_same_json must snapshot controller.active_page once. It
is cleared to None from another thread while a controller (dis)connects, so
re-reading the field per check raced a non-None guard against a None deref of
.json_path -- the same class as update_input's guard (issue #55).
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import globals as gl
from fixtures import FaultyFakeDeck, seed_page, start_watchdog

from src.backend.PageManagement.Page import Page


class RecordingController:
    """Records which Page object load_page received."""

    def __init__(self, serial: str):
        self.deck = FaultyFakeDeck(serial_number=serial)
        self.active_page = None
        self.loaded_pages = []
        self.loaded_inputs = []

    def serial_number(self) -> str:
        return self.deck.get_serial_number()

    def load_page(self, page, *args, **kwargs):
        self.loaded_pages.append(page)

    def load_input_from_identifier(self, identifier, page):
        self.loaded_inputs.append((identifier, page))


def main() -> int:
    start_watchdog(30, "reload_similar_pages")
    fixtures._install_integration_globals()

    path = seed_page("SharedPage")

    ctrl_a = RecordingController("reload-a")
    ctrl_b = RecordingController("reload-b")

    page_a = Page(json_path=path, deck_controller=ctrl_a)
    page_b = Page(json_path=path, deck_controller=ctrl_b)
    ctrl_a.active_page = page_a
    ctrl_b.active_page = page_b

    gl.deck_manager.deck_controller = [ctrl_a, ctrl_b]

    page_a.reload_similar_pages()  # identifier=None, reload_self=False

    if ctrl_a.loaded_pages:
        print(f"FAIL: caller's own controller was reloaded despite reload_self=False: {ctrl_a.loaded_pages}")
        return 1
    if ctrl_b.loaded_pages != [page_b]:
        got = ["page_a (the CALLER'S page)" if p is page_a else
               ("page_b" if p is page_b else repr(p)) for p in ctrl_b.loaded_pages]
        print(f"FAIL: sibling controller received {got}, expected its own [page_b]")
        return 1

    # --- active_page flips to None mid-scan must not AttributeError --------
    class FlippingController:
        """active_page reads non-None once (passing the guard), then None on
        the next read -- exactly the (dis)connect race. A per-check re-read
        would then deref None.json_path; a single snapshot is immune."""
        def __init__(self, serial, page):
            self.deck = FaultyFakeDeck(serial_number=serial)
            self._page = page
            self._reads = 0

        def serial_number(self):
            return self.deck.get_serial_number()

        @property
        def active_page(self):
            self._reads += 1
            return self._page if self._reads <= 1 else None

    probe_page = Page(json_path=path, deck_controller=ctrl_a)
    flipping = FlippingController("reload-flip", probe_page)
    gl.deck_manager.deck_controller = [flipping]
    try:
        probe_page.get_pages_with_same_json(get_self=True)
    except AttributeError as e:
        print(f"FAIL: get_pages_with_same_json derefs a concurrently-cleared active_page: {e}")
        return 1

    print("PASS: reload_similar_pages loads each controller's own Page; "
          "get_pages_with_same_json survives active_page clearing mid-scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
