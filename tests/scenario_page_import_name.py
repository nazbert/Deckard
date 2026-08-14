"""
Regression test for page import through the PageManager menu.

MenuButton.import_page_name_selected_callback must derive the new page's name
from its name parameter, not from page_path, which add_page assigns two lines
later.
"""

# The callback runs unbound with a duck-typed self, and everything else it
# touches is real.
import json
import os

import fixtures
import globals as gl


class FakeFile:
    """Gio.File stand-in. The callback only calls get_path()."""

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
    """Duck-typed self that exposes what the callback dereferences,
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

    # Import under a fresh name. Both the dialog and the direct path end here.
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

    # Importing onto an existing name must return cleanly, without touching
    # the selector and without raising. FileExistsError is caught.
    fake_self2 = FakeMenuButtonSelf(FakeFile(src_path))
    MenuButton.import_page_name_selected_callback(fake_self2, "ImportedPage")
    assert fake_self2.pageEditor.page_manager.page_selector.added_paths == [], (
        "duplicate-name import must not add a selector row"
    )

    # A dotted name typed in the rename dialog must survive whole. Truncation
    # at the first dot would rename the user's page to "backup".
    fake_self3 = FakeMenuButtonSelf(FakeFile(src_path))
    MenuButton.import_page_name_selected_callback(fake_self3, "backup.v2")

    dotted_path = os.path.join(gl.page_manager.PAGE_PATH, "backup.v2.json")
    assert os.path.isfile(dotted_path), (
        f"dotted typed name was not preserved -- expected {dotted_path}, "
        f"the name was truncated at the dot"
    )
    assert not os.path.exists(os.path.join(gl.page_manager.PAGE_PATH, "backup.json")), (
        "the '.v2' suffix was stripped -- page saved as 'backup' instead of 'backup.v2'"
    )
    assert fake_self3.pageEditor.page_manager.page_selector.added_paths == [dotted_path], (
        "the dotted-name page was not added to the selector under its full name"
    )
    assert gl.page_manager.find_matching_page_path("backup.v2") == dotted_path, (
        "the dotted-name page is not findable under the name the user typed"
    )

    print("PASS: scenario_page_import_name")


if __name__ == "__main__":
    main()
