"""
Regression test for gl#120: page import via the PageManager menu raised
NameError.

MenuButton.import_page_name_selected_callback derived the new page's name
from `page_path` -- a local that is only assigned two lines LATER (by
add_page) -- instead of from its `name` parameter, so every import through
the menu died with `NameError: name 'page_path' is not defined` before the
page was ever written.

The callback is driven unbound with a duck-typed `self` (a real MenuButton
is a Gtk widget and would need a display); everything it touches beyond
`self` -- gl.page_manager.add_page, gl.signal_manager, the source JSON on
disk -- is real.
"""
import json
import os

import fixtures
import globals as gl


class FakeFile:
    """Gio.File stand-in: the callback only calls get_path()."""

    def __init__(self, path: str):
        self._path = path

    def get_path(self) -> str:
        return self._path


class FakePageSelector:
    def __init__(self):
        self.added_paths: list[str] = []

    def add_row_by_path(self, path: str) -> None:
        self.added_paths.append(path)


class FakePageManagerWindow:
    def __init__(self):
        self.page_selector = FakePageSelector()


class FakePageEditor:
    def __init__(self):
        self.page_manager = FakePageManagerWindow()


class FakeMenuButtonSelf:
    """Duck-typed `self` exposing exactly what the callback dereferences:
    .selected_file and .pageEditor.page_manager.page_selector."""

    def __init__(self, selected_file: FakeFile):
        self.selected_file = selected_file
        self.pageEditor = FakePageEditor()


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_page_import_name")
    fixtures._install_integration_globals()

    from src.windows.PageManager.elements.MenuButton import MenuButton

    # add_page assumes the pages dir exists (the app creates it at startup).
    os.makedirs(gl.page_manager.PAGE_PATH, exist_ok=True)

    # A source file to import, outside the pages dir (as a FileDialog pick
    # would be).
    src_path = os.path.join(fixtures.DATA_DIR, "import_src", "exported.json")
    os.makedirs(os.path.dirname(src_path), exist_ok=True)
    page_dict = {"keys": {}, "dials": {}, "touchscreens": {}}
    with open(src_path, "w") as f:
        json.dump(page_dict, f)

    # --- Import under a fresh name (both dialog and direct path end here).
    fake_self = FakeMenuButtonSelf(FakeFile(src_path))
    MenuButton.import_page_name_selected_callback(fake_self, "ImportedPage")

    expected_path = os.path.join(gl.page_manager.PAGE_PATH, "ImportedPage.json")
    assert os.path.isfile(expected_path), (
        f"import did not create {expected_path} -- the callback died before add_page"
    )
    with open(expected_path) as f:
        assert json.load(f) == page_dict, "imported page content does not match the source file"
    assert fake_self.pageEditor.page_manager.page_selector.added_paths == [expected_path], (
        "the imported page was not added to the page selector"
    )
    assert fake_self.selected_file is None, "selected_file was not cleared after import"
    assert gl.page_manager.find_matching_page_path("ImportedPage") == expected_path

    # --- Importing onto an existing name must return cleanly (FileExistsError
    # is caught), not touch the selector, and not raise.
    fake_self2 = FakeMenuButtonSelf(FakeFile(src_path))
    MenuButton.import_page_name_selected_callback(fake_self2, "ImportedPage")
    assert fake_self2.pageEditor.page_manager.page_selector.added_paths == [], (
        "duplicate-name import must not add a selector row"
    )

    print("PASS: scenario_page_import_name")


if __name__ == "__main__":
    main()
