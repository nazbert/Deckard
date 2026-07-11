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


EVIL_REFS = [
    "main; touch /tmp/mr16_pwned",
    "main && rm -rf ~",
    "main`id`",
    "main$(id)",
    "main | cat /etc/passwd",
    "main\nrm -rf ~",
    "-c",              # git would read a leading '-' as an option
    "--upload-pack=x",
    "with space",
    "quote'inject",
    "",
    None,
    42,
    "a/" + "b" * 300,  # over the length cap
]

CLEAN_REFS = [
    "main",
    "release/1.5.0",
    "feature/store-hardening",
    "v1.2.3",
    "1.5.0",
    "a-b_c.d",
]


def test_ref_and_sha_validator_cases() -> None:
    for evil in EVIL_REFS:
        assert not StoreBackend.is_safe_ref_name(evil), (
            f"unsafe branch/ref must be rejected: {evil!r}"
        )
    for clean in CLEAN_REFS:
        assert StoreBackend.is_safe_ref_name(clean), (
            f"legitimate ref must be accepted: {clean!r}"
        )
    # commit sha: exactly 40 hex.
    assert StoreBackend.is_safe_commit_sha("a" * 40)
    assert StoreBackend.is_safe_commit_sha("0123456789abcdef0123456789abcdef01234567")
    for bad in ["a" * 39, "a" * 41, "z" * 40, "main; id", "", None, 40,
                "abc; rm -rf ~" + "a" * 27]:
        assert not StoreBackend.is_safe_commit_sha(bad), (
            f"malformed commit sha must be rejected: {bad!r}"
        )


def test_clone_repo_rejects_injection_and_never_shells() -> None:
    """The devel clone path used to os.system(f'... git switch {branch}') /
    'git reset --hard {sha}' with REMOTE-catalog values. A branch of
    'main; touch <marker>' would have run the injected command. Assert:
    (1) a metachar branch/sha is refused (400) before any git call, and the
    injected side effect never happens; (2) git is only ever invoked as an
    argv list (never a shell string)."""
    sb = _make_backend()

    marker = os.path.join(gl.DATA_PATH, "mr16_injection_marker")
    if os.path.exists(marker):
        os.remove(marker)

    calls = []

    async def fake_subp_call(args):
        calls.append(args)
        # A correct fix passes argv lists; a regression to a shell string
        # would show up here as a str, which we forbid outright.
        assert isinstance(args, list), f"git must be invoked as argv list, got {args!r}"
        # Stand in for `git clone`, which is what actually creates the dir
        # (clone_repo rmtree's it first) -- so the later VERSION write works.
        if len(args) >= 2 and args[1] == "clone":
            os.makedirs(args[-1], exist_ok=True)
        return 0

    async def fake_os_sys(args):
        # os_sys is os.system -- it must NEVER be reached with catalog values
        # on the clone path anymore.
        raise AssertionError(f"os_sys (shell) must not be used on the clone path: {args!r}")

    sb.subp_call = fake_subp_call
    sb.os_sys = fake_os_sys

    # 1) Injected branch: refused, no git call, no side effect.
    injected_branch = f"main; touch {marker}"
    result = asyncio.run(sb.clone_repo("https://github.com/evil/evil",
                                       os.path.join(gl.PLUGIN_DIR, "victim"),
                                       commit_sha=None, branch_name=injected_branch))
    assert result == 400, f"injected branch must be refused, got {result!r}"
    assert calls == [], f"no git call may happen for an injected branch, got {calls}"
    assert not os.path.exists(marker), "injected command must never create its marker"

    # 2) Injected commit sha: refused likewise.
    result = asyncio.run(sb.clone_repo("https://github.com/evil/evil",
                                       os.path.join(gl.PLUGIN_DIR, "victim"),
                                       commit_sha=f"deadbeef; touch {marker}", branch_name=None))
    assert result == 400, f"injected commit sha must be refused, got {result!r}"
    assert not os.path.exists(marker), "injected command must never create its marker"

    # 3) A CLEAN branch reaches git only as an argv list (no shell). git is
    #    fully stubbed above (fake_subp_call), so this needs no real binary
    #    and asserts the exact argv shape. `git switch` is the token we care
    #    about; shutil.which("git") inside clone_repo is monkeypatched so the
    #    "git not installed" 404 branch can't fire on a git-less box.
    calls.clear()
    import src.backend.Store.StoreBackend as backend_module
    real_which = backend_module.shutil.which
    backend_module.shutil.which = lambda name: "/usr/bin/git" if name == "git" else real_which(name)
    try:
        local_path = os.path.join(gl.PLUGIN_DIR, "clean")
        result = asyncio.run(sb.clone_repo("https://github.com/x/y", local_path,
                                           commit_sha=None, branch_name="release/1.5.0"))
    finally:
        backend_module.shutil.which = real_which
    assert result == 200, f"clean clone must succeed, got {result!r}"
    switch_calls = [c for c in calls if len(c) >= 4 and c[3] == "switch"]
    assert switch_calls, f"expected an argv 'git switch' call, got {calls}"
    argv = switch_calls[0]
    assert argv[:4] == ["git", "-C", local_path, "switch"], f"unexpected argv {argv!r}"
    assert argv[-1] == "release/1.5.0", "the validated branch must be passed as its own token"


def test_download_repo_refuses_zip_slip_member() -> None:
    """Defense-in-depth: a downloaded archive with a traversal/absolute
    member is refused before shutil.unpack_archive writes anything."""
    import zipfile
    sb = _make_backend()

    cache_dir = os.path.join(gl.DATA_PATH, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    evil_zip = os.path.join(cache_dir, "mr16_slip.zip")
    with zipfile.ZipFile(evil_zip, "w") as z:
        z.writestr("pkg/normal.txt", "ok")
        z.writestr("../../mr16_escape.txt", "PWNED")
    assert sb.zip_has_unsafe_members(evil_zip) is True, "traversal member must be flagged"

    abs_zip = os.path.join(cache_dir, "mr16_abs.zip")
    with zipfile.ZipFile(abs_zip, "w") as z:
        z.writestr("/etc/mr16_abs.txt", "abs")
    assert sb.zip_has_unsafe_members(abs_zip) is True, "absolute member must be flagged"

    clean_zip = os.path.join(cache_dir, "mr16_clean.zip")
    with zipfile.ZipFile(clean_zip, "w") as z:
        z.writestr("pkg/a.txt", "a")
        z.writestr("pkg/sub/b.txt", "b")
    assert sb.zip_has_unsafe_members(clean_zip) is False, "a clean archive must pass"


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_id_sanitizer")
    test_validator_cases()
    test_ref_and_sha_validator_cases()
    test_install_plugin_rejects_traversal_id()
    test_uninstall_icon_rejects_traversal_id()
    test_install_script_runs_without_shell()
    test_clone_repo_rejects_injection_and_never_shells()
    test_download_repo_refuses_zip_slip_member()
    print("scenario_store_id_sanitizer: PASS")


if __name__ == "__main__":
    main()
