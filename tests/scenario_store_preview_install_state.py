"""
Regression pin -- the store's icon / wallpaper / SD+ bar-wallpaper preview
buttons flipped to "installed" even when the download FAILED, exercised
WITHOUT network and WITHOUT a display.

The three data-only preview `install()` methods used to ignore the backend's
return and call set_install_state(1) unconditionally:

    def install(self):
        self.store.backend.install_icon(icon_data=self.icon_data)
        self.set_install_state(1)          # <- flips to "installed" on failure

A 400 (unsafe id / missing url), a 404 (hard install failure) or an offline
download therefore left the button reading "installed" over a pack that was
never written. The fix adopts the PluginPreview shape: check the StoreResult,
notify on an Err, and LEAVE the button in its previous state so the user can
retry.

Each preview's install() is driven UNBOUND over a duck-typed self (the real
widgets need a display; the result handling and the GLib marshalling are real).
One failure shape per sibling covers the three Err reasons:
  * icon        -> INSTALL_FAILED  (the 404 class)
  * wallpaper   -> NO_CONNECTION   (offline)
  * SD+ bar     -> INVALID_ASSET   (the 400 class)

MUTATION PIN: re-introducing a truthy success check (`if result:`) in any of
these previews is caught by these legs -- an Err is TRUTHY, so `if result:`
would flip the button to installed on every failure. Truthiness is not the
protocol; narrowing on Err is.
"""
import types

import fixtures  # noqa: F401  (isolates DATA_PATH before src imports)

import gi

gi.require_version("Adw", "1")
from gi.repository import GLib  # noqa: E402

from src.backend.Store.store_result import Ok, Err, ErrReason  # noqa: E402
from src.windows.Store.StoreData import (  # noqa: E402
    IconData,
    WallpaperData,
    SDPlusBarWallpaperData,
)

WATCHDOG_SECONDS = 30


def pump_main_context(rounds: int = 50) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(rounds):
        while ctx.pending():
            ctx.iteration(False)


def _make_fake(data, install_attr: str, install_result):
    """A duck-typed preview self: records set_install_state / notify calls, and
    a backend stub whose install_* answers `install_result`."""
    state = {"install_state": 0, "set_calls": [], "notified": 0}
    backend = types.SimpleNamespace(**{install_attr: lambda **kwargs: install_result})

    def set_install_state(s):
        state["set_calls"].append(s)
        state["install_state"] = s

    fake = types.SimpleNamespace(
        store=types.SimpleNamespace(backend=backend),
        install_state=0,
        set_install_state=set_install_state,
        notify_install_failure=lambda: state.__setitem__("notified", state["notified"] + 1),
    )
    return fake, state


def _check_failed_install_preserves_state(preview_cls, data_attr, data, install_attr,
                                          err, label) -> None:
    fake, state = _make_fake(data, install_attr, err)
    setattr(fake, data_attr, data)

    preview_cls.install(fake)
    pump_main_context()

    assert state["install_state"] != 1, (
        f"{label}: a failed install ({err.reason}) must NOT flip the button to "
        f"installed -- got set_install_state calls {state['set_calls']}"
    )
    assert 1 not in state["set_calls"], (
        f"{label}: set_install_state(1) must never run on a failed install, "
        f"got {state['set_calls']}"
    )
    assert state["notified"] == 1, (
        f"{label}: a failed install must record exactly one failure "
        f"notification, got {state['notified']}"
    )
    print(f"PASS: {label} preview keeps its state and notifies on a failed install")


def check_icon_preview_404() -> None:
    from src.windows.Store.Icons.IconPage import IconPreview
    data = IconData(github="https://github.com/a/Icons", icon_id="com_a_Icons",
                    icon_name="Test Icons")
    _check_failed_install_preserves_state(
        IconPreview, "icon_data", data, "install_icon",
        Err(ErrReason.INSTALL_FAILED, "404-shaped"), "icon")


def check_wallpaper_preview_offline() -> None:
    from src.windows.Store.Wallpapers.WallpaperPage import WallpaperPreview
    data = WallpaperData(github="https://github.com/b/Wall", wallpaper_id="com_b_Wall",
                         wallpaper_name="Test Wall")
    _check_failed_install_preserves_state(
        WallpaperPreview, "wallpaper_data", data, "install_wallpaper",
        Err(ErrReason.NO_CONNECTION, "offline"), "wallpaper")


def check_sd_plus_preview_400() -> None:
    from src.windows.Store.SDPlusBarWallpapers.SDPlusBarWallpaperPage import SDPlusBarWallpaperPreview
    data = SDPlusBarWallpaperData(github="https://github.com/c/SDPlus", id="com_c_SDPlus",
                                  name="Test SDPlus")
    _check_failed_install_preserves_state(
        SDPlusBarWallpaperPreview, "wallpaper_data", data, "install_sd_plus_bar_wallpaper",
        Err(ErrReason.INVALID_ASSET, "400-shaped"), "SD+ bar wallpaper")


def check_icon_preview_success_flips_installed() -> None:
    """The happy path still works: an Ok(None) install flips the button to
    installed (via the idle-marshalled set_install_state, pumped here)."""
    from src.windows.Store.Icons.IconPage import IconPreview
    data = IconData(github="https://github.com/a/Icons", icon_id="com_a_Icons",
                    icon_name="Test Icons")
    fake, state = _make_fake(data, "install_icon", Ok(None))
    fake.icon_data = data

    IconPreview.install(fake)
    pump_main_context()

    assert state["install_state"] == 1, (
        f"a successful install must flip the button to installed, got "
        f"{state['set_calls']}"
    )
    assert state["notified"] == 0, "a successful install must not notify a failure"
    print("PASS: icon preview flips to installed on a successful install")


def main() -> None:
    fixtures.start_watchdog(WATCHDOG_SECONDS, label="scenario_store_preview_install_state")
    check_icon_preview_404()
    check_wallpaper_preview_offline()
    check_sd_plus_preview_400()
    check_icon_preview_success_flips_installed()
    print("scenario_store_preview_install_state: PASS")


if __name__ == "__main__":
    main()
