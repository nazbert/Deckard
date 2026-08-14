"""
A store preview button must not read "installed" after a failed download.

Each data-only preview install() checks the StoreResult, notifies on an Err,
and leaves the button in its previous state so the user can retry. An Err is
truthy, so the protocol is narrowing on the result type rather than a
truthiness check. The previews run unbound over a duck-typed self.
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
    """A duck-typed preview self. It records set_install_state and notify
    calls, over a backend stub whose install_* answers install_result."""
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
    """A successful install still flips the button. An Ok(None) reaches the
    idle-marshalled set_install_state, which this check pumps."""
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
