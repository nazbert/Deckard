"""
Regression test for "data-path setting persisted on every keystroke" (gl#46).

DataPathGroup used to write static settings on notify::text -- every character
typed. An abandoned half-typed edit (or a crash mid-edit) then became the
data path globals.py adopts on the next launch, makedirs'ing garbage and
booting an empty profile.

Contract under test (src/windows/Settings/Settings.py DataPathGroup):
  * text changes alone must NOT persist anything;
  * an explicit apply (Enter / the check button) persists -- after validation;
  * an invalid path (relative, unwritable) is refused, nothing is persisted;
  * the previous value is kept recoverable as "data-path-previous".

Needs a display (it builds real Adw widgets); prints SKIP and exits 0 when
GTK can't initialize, like a headless CI box.
"""
import os

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)
import globals as gl


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_data_path_commit")

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw

    if not Gtk.init_check():
        print("SKIP: no display available; scenario needs GTK")
        return
    Adw.init()

    saves: list[dict] = []

    class RecordingSettingsManager:
        """Only the two methods DataPathGroup dereferences. Returns a fresh
        dict per read, like the real load_settings_from_file."""

        def __init__(self):
            self._static = {"data-path": gl.DATA_PATH}

        def get_static_settings(self) -> dict:
            return dict(self._static)

        def save_static_settings(self, settings: dict) -> None:
            self._static = dict(settings)
            saves.append(dict(settings))

    gl.settings_manager = RecordingSettingsManager()

    from src.windows.Settings.Settings import DataPathGroup

    group = DataPathGroup(settings=object())
    entry = group.data_path

    # --- 1. typing must not persist ---------------------------------------
    for partial in ["/", "/t", "/tm", "/tmp", "/tmp/half-typed"]:
        entry.set_text(partial)
    assert not saves, (
        f"text changes persisted {len(saves)} time(s) without an explicit "
        f"apply -- an abandoned edit would boot an empty profile: {saves[-1]}"
    )
    print("PASS: keystrokes alone persist nothing")

    # --- 2. explicit apply persists (valid path), old value recoverable ---
    new_dir = os.path.join(gl.DATA_PATH, "relocated-data")
    entry.set_text(new_dir)
    assert not saves, "set_text must still not persist"
    entry.emit("apply")
    assert saves, "an explicit apply must persist the value"
    assert saves[-1]["data-path"] == new_dir, f"persisted wrong value: {saves[-1]}"
    assert saves[-1].get("data-path-previous") == gl.DATA_PATH, (
        f"previous data path not kept recoverable: {saves[-1]}"
    )
    assert os.path.isdir(new_dir), "validation should have created the new dir"
    print("PASS: apply persists the validated path and keeps the old one recoverable")

    # --- 3. invalid paths are refused --------------------------------------
    saves.clear()
    for bogus in ["relative/not-absolute", "", "/proc/definitely-not-writable"]:
        entry.set_text(bogus)
        entry.emit("apply")
    assert not saves, f"an invalid path was persisted: {saves[-1] if saves else None}"
    assert entry.has_css_class("error"), "invalid apply should mark the row as error"
    print("PASS: invalid paths are refused and flagged")

    print("PASS: scenario_data_path_commit")


if __name__ == "__main__":
    main()
