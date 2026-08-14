"""
The settings Data path Open button opens through Gio, not a subprocess.

DataPathGroup.on_open_data_path_button_clicked expanduser's the entry text and
launches it as a file:// URI through Gio.AppInfo.launch_default_for_uri, which
the portal routes under flatpak.
"""

# A raising launcher stays inside the handler, and a missing display makes this
# scenario skip.
import os

import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)
import globals as gl


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_settings_open_data_path")

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib

    if not Gtk.init_check():
        print("SKIP: no display available; scenario needs GTK")
        return
    Adw.init()

    class StubSettingsManager:
        """Only what DataPathGroup dereferences."""

        def get_static_settings(self) -> dict:
            return {"data-path": gl.DATA_PATH}

        def save_static_settings(self, settings: dict) -> None:
            pass

    gl.settings_manager = StubSettingsManager()

    import src.windows.Settings.Settings as settings_mod
    from src.windows.Settings.Settings import DataPathGroup

    group = DataPathGroup(settings=object())

    launched: list[str] = []
    real_gio = settings_mod.Gio

    class FakeAppInfo:
        @staticmethod
        def launch_default_for_uri(uri, ctx=None):
            launched.append(uri)
            return True

    class FakeGio:
        File = real_gio.File  # real, because the handler builds the URI through it
        AppInfo = FakeAppInfo

    settings_mod.Gio = FakeGio
    try:
        # 1. The tilde expands and the scheme is file://.
        group.data_path.set_text("~/deckard-open-probe")
        group.on_open_data_path_button_clicked()

        assert launched, "the Open button must launch the data path"
        expected = real_gio.File.new_for_path(
            os.path.expanduser("~/deckard-open-probe")).get_uri()
        assert launched[-1] == expected, (
            f"expected the expanduser'd file:// URI {expected!r}, got {launched[-1]!r}"
        )
        assert launched[-1].startswith("file://"), launched[-1]
        assert "~" not in launched[-1], "the ~ must be expanded before launching"
        print("PASS: Open launches the expanduser'd path as a file:// URI")

        # 2. A raising launcher stays contained.
        def raising_launch(uri, ctx=None):
            raise GLib.Error("simulated: no handler for the URI")

        FakeAppInfo.launch_default_for_uri = staticmethod(raising_launch)
        group.on_open_data_path_button_clicked()  # must not raise
        print("PASS: a raising launcher is contained inside the handler")
    finally:
        settings_mod.Gio = real_gio

    print("PASS: scenario_settings_open_data_path")


if __name__ == "__main__":
    main()
