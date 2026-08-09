"""
A refused asset import must never hand its caller something that looks like
a media path.

`AssetManagerBackend.add_custom_media_set_by_ui` answered a url that points
at no supported media with `-1`, while every other refusal in the same
function answers `None`. `KeyButton.handle_file_drop` -- the drop target on
every key in the grid -- tested the result with `is None`, so the `-1` sailed
through and was written into the key's `media.path` and saved to the page:
a key config referring to a media path of -1, i.e. a broken key where the
user expected a rejection.

Both ends are pinned here, because either one alone leaves the door open:

  (a) source: a rejected url yields None (and still tells the user), like
      the download-failure and corrupt-file refusals next to it;
  (b) consumer: handle_file_drop treats ANY non-path answer as a refusal --
      None, the historical -1, or an empty string -- and only proceeds for
      a real path.

The consumer check calls the method with a stand-in `self`: the refusal
branch returns before touching a single widget, and the accept branch is
identified by the AttributeError it raises the moment it reaches
`self.key_grid` -- which is the proof that the guard let it through, with no
GTK display anywhere in sight.
"""
import fixtures  # noqa: F401  (must be first: isolates DATA_PATH)

import types

import globals as gl

from src.backend import AssetManagerBackend as amb_mod
from src.backend.AssetManagerBackend import AssetManagerBackend
from src.windows.mainWindow.elements.KeyGrid import KeyButton

REJECTED_URL = "https://example.com/search?q=cat"


class StubBackend:
    """Stands in for gl.asset_manager_backend with a scripted answer."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def add_custom_media_set_by_ui(self, url=None, path=None):
        self.calls += 1
        return self.answer


class StubFile:
    """A Gio.File-alike for a remote drop: a uri, but no local path."""

    def get_uri(self):
        return REJECTED_URL

    def get_path(self):
        return None


class StubDropValue:
    def get_files(self):
        return [StubFile()]


def check_rejected_url_returns_none() -> None:
    dialogs: list[dict] = []

    class FakeAlertDialog:
        def __init__(self, **kwargs):
            dialogs.append(kwargs)

        def show(self, *a, **k):
            pass

    backend = AssetManagerBackend()
    real_gtk, real_glib = amb_mod.Gtk, amb_mod.GLib
    real_app, real_backend = gl.app, gl.asset_manager_backend
    amb_mod.Gtk = types.SimpleNamespace(AlertDialog=FakeAlertDialog)
    amb_mod.GLib = types.SimpleNamespace(idle_add=lambda fn, *a: fn(*a))
    gl.app = types.SimpleNamespace(main_win=None)
    gl.asset_manager_backend = backend
    try:
        result = backend.add_custom_media_set_by_ui(url=REJECTED_URL, path=None)
    finally:
        amb_mod.Gtk, amb_mod.GLib = real_gtk, real_glib
        gl.app, gl.asset_manager_backend = real_app, real_backend

    assert result is None, (
        f"a rejected url must refuse with None like every other refusal in "
        f"this function, got {result!r} -- a sentinel that is not a path but "
        f"is not None either is what reaches the key config"
    )
    assert dialogs, "the rejection must still tell the user (AlertDialog)"
    print("ok: a rejected url refuses with None, with the alert intact")


def check_key_drop_rejects_every_non_path() -> None:
    real_backend = gl.asset_manager_backend
    try:
        for answer in (None, -1, "", 0, False):
            backend = StubBackend(answer)
            gl.asset_manager_backend = backend
            result = KeyButton.handle_file_drop(
                types.SimpleNamespace(), None, StubDropValue(), 0, 0)
            assert backend.calls == 1, "the drop must reach the importer"
            assert result is False, (
                f"an import answering {answer!r} is a refusal, but the drop "
                f"returned {result!r} and went on to write it into the key's "
                f"media path"
            )

        # The accept branch must still be reachable: with a real path the
        # guard lets it through and it dies on the stand-in `self`.
        gl.asset_manager_backend = StubBackend("Assets/imported.png")
        raised = None
        try:
            KeyButton.handle_file_drop(
                types.SimpleNamespace(), None, StubDropValue(), 0, 0)
        except AttributeError as e:
            raised = e
        assert raised is not None and "key_grid" in str(raised), (
            "a real media path must get PAST the guard (it should reach "
            f"self.key_grid and fail on the stand-in self); got {raised!r}"
        )
    finally:
        gl.asset_manager_backend = real_backend

    print("ok: the key drop target refuses every non-path answer and only "
          "proceeds for a real path")


def main() -> None:
    fixtures.start_watchdog(60, label="scenario_asset_reject_sentinel")

    check_rejected_url_returns_none()
    check_key_drop_rejects_every_non_path()

    print("PASS: scenario_asset_reject_sentinel")


if __name__ == "__main__":
    main()
