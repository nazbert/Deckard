"""
Regression test for gl#27 -- manifest-controlled asset ids reaching
rmtree/install path joins, and shell=True install-script invocation.

plugin_id/icon_id/wallpaper_id come from a REMOTE manifest.json. Before the
fix, an id like "../../.." walked `shutil.rmtree`/install targets out of the
app's data dirs (os.path.join happily traverses, and an absolute id replaces
the base entirely), and `subprocess.run(f"{sys.executable} {path}",
shell=True)` both broke on spaces and allowed shell injection via crafted
path components.

Now StoreBackend.is_safe_asset_id whitelists ids at every join site
(rejecting, not normalizing), and the install scripts run as argv lists
without a shell. All network-free: download_repo is stubbed.
"""
import asyncio
import os
import sys

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend.Store.StoreBackend import StoreBackend
from src.windows.Store.StoreData import PluginData, IconData, WallpaperData


EVIL_IDS = [
    "../../..",
    "..",
    ".",
    "../sibling",
    "/etc",
    "/home/user",
    "a/b",
    "a\\b",
    "with space",
    " leading",
    ".hidden",
    "",
    None,
    42,
    "a" * 200,  # over the length cap
]

CLEAN_IDS = [
    "com_core447_MediaPlugin",
    "com.core447.OSPlugin",
    "material-icons",
    "pack_1.2.3",
    "A",
]


def test_validator_cases() -> None:
    for evil in EVIL_IDS:
        assert not StoreBackend.is_safe_asset_id(evil), (
            f"unsafe id must be rejected: {evil!r}"
        )
    for clean in CLEAN_IDS:
        assert StoreBackend.is_safe_asset_id(clean), (
            f"legitimate id must be accepted: {clean!r}"
        )


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__ (spawns a fetch thread)
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    return sb


def test_install_plugin_rejects_traversal_id() -> None:
    sb = _make_backend()
    download_calls = []

    async def fake_download_repo(**kwargs):
        download_calls.append(kwargs)
        return 200

    sb.download_repo = fake_download_repo

    for evil in ["../../..", "/etc", "../sibling", None]:
        data = PluginData(github="https://github.com/evil/evil", plugin_id=evil)
        result = asyncio.run(sb.install_plugin(data))
        assert result == 400, f"unsafe plugin id {evil!r} must be refused, got {result!r}"

    assert download_calls == [], (
        f"download must never start for an unsafe id, got {download_calls}"
    )


def test_uninstall_icon_rejects_traversal_id() -> None:
    sb = _make_backend()

    icons_dir = os.path.join(gl.DATA_PATH, "icons")
    os.makedirs(icons_dir, exist_ok=True)
    sentinel = os.path.join(gl.DATA_PATH, "sentinel")
    os.makedirs(sentinel, exist_ok=True)

    # "icons/.." IS gl.DATA_PATH -- the old code would rmtree the whole data dir.
    result = asyncio.run(sb.uninstall_icon(IconData(icon_id="..")))
    assert result == 400, f"traversal icon id must be refused, got {result!r}"
    assert os.path.isdir(gl.DATA_PATH), "data dir must survive a traversal uninstall id"
    assert os.path.isdir(sentinel), "sibling dirs must survive a traversal uninstall id"

    result = asyncio.run(sb.uninstall_wallpaper(WallpaperData(wallpaper_id="../sentinel")))
    assert result == 400, f"traversal wallpaper id must be refused, got {result!r}"
    assert os.path.isdir(sentinel), "targeted sibling must survive a traversal uninstall id"


def test_install_script_runs_without_shell() -> None:
    sb = _make_backend()

    plugin_id = "com_test_ScriptPlugin"
    local_path = os.path.join(gl.PLUGIN_DIR, plugin_id)

    async def fake_download_repo(**kwargs):
        os.makedirs(kwargs["directory"], exist_ok=True)
        with open(os.path.join(kwargs["directory"], "__install__.py"), "w") as f:
            f.write("pass\n")
        return 200

    sb.download_repo = fake_download_repo

    # Stub the post-install plumbing install_plugin drives.
    class StubPluginManager:
        def load_plugins(self): pass
        def init_plugins(self): pass
        def generate_action_index(self): pass
        def get_plugins(self): return {}

    class StubSignalManager:
        def trigger_signal(self, *a, **k): pass

    fixtures.install_stub_globals()
    gl.plugin_manager = StubPluginManager()
    gl.signal_manager = StubSignalManager()

    import src.backend.Store.StoreBackend as backend_module
    captured = []
    real_run = backend_module.subprocess.run

    def capture_run(argv, **kwargs):
        captured.append((argv, kwargs))

    backend_module.subprocess.run = capture_run
    try:
        result = asyncio.run(sb.install_plugin(PluginData(
            github="https://github.com/test/test", plugin_id=plugin_id,
        )))
    finally:
        backend_module.subprocess.run = real_run

    assert result is True, f"clean install must succeed, got {result!r}"
    assert len(captured) == 1, f"expected exactly the __install__.py invocation, got {captured}"
    argv, kwargs = captured[0]
    assert isinstance(argv, list), f"install script must run as an argv list, got {argv!r}"
    assert argv[0] == sys.executable
    assert argv[1] == os.path.join(local_path, "__install__.py")
    assert kwargs.get("shell") is not True, "install script must not run through a shell"


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_id_sanitizer")
    test_validator_cases()
    test_install_plugin_rejects_traversal_id()
    test_uninstall_icon_rejects_traversal_id()
    test_install_script_runs_without_shell()
    print("scenario_store_id_sanitizer: PASS")


if __name__ == "__main__":
    main()
