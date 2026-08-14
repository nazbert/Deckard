"""
Regression test for rebrand_migration.migrate_native_var_app_to_xdg.

Pre-XDG native builds stored data at ~/.var/app/<id>. The migration moves it to
$XDG_DATA_HOME/deckard and leaves a compat symlink. Stdlib only, so globals
stay unimported.
"""

# The same-filesystem path reuses migrate()'s atomic rename, so the cases below
# cover the XDG wiring and the cross-filesystem copy.
import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

HOME = tempfile.mkdtemp(prefix="xdg_home_")
os.environ["HOME"] = HOME
os.environ.pop("XDG_DATA_HOME", None)  # exercise the ~/.local/share default

import rebrand_migration as rm  # noqa: E402

assert "globals" not in sys.modules, "rebrand_migration must not pull in globals"

rm._is_flatpak = lambda: False  # native by default


def fresh_roots():
    base = tempfile.mkdtemp(prefix="xdg_roots_", dir=HOME)
    return os.path.join(base, "var_app_deckard"), os.path.join(base, "xdg_deckard")


def make_tree(root):
    os.makedirs(os.path.join(root, "data", "pages"))
    os.makedirs(os.path.join(root, "static"))
    with open(os.path.join(root, "data", "pages", "Main.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(root, "static", "settings.json"), "w") as f:
        f.write("{}")


def marker_state(root, name):
    try:
        with open(os.path.join(root, name)) as f:
            return f.read().strip()
    except OSError:
        return None


# 1. Flatpak changes nothing. The ~/.var/app dir is correct there.
old, new = fresh_roots()
make_tree(old)
rm._is_flatpak = lambda: True
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.isdir(old) and not os.path.islink(old), "flatpak run moved the tree"
assert not os.path.lexists(new)
rm._is_flatpak = lambda: False
print("1. flatpak no-op: OK")

# 2. A fresh native install with no var-app tree changes nothing.
old, new = fresh_roots()
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert not os.path.lexists(old) and not os.path.lexists(new)
print("2. fresh native no-op: OK")

# 3. Normal native move.
old, new = fresh_roots()
make_tree(old)
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.isfile(os.path.join(new, "static", "settings.json"))
assert os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new), "compat symlink missing/wrong"
assert os.path.isfile(os.path.join(old, "data", "pages", "Main.json")), "old path does not resolve through link"
assert marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
assert marker_state(new, rm.MARKER_NAME) is None, "used the StreamController marker instead of the XDG one"
print("3. normal native move: OK")

# 4. Idempotent re-run.
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("4. idempotent re-run: OK")

# 5. A --data override skips everything.
old, new = fresh_roots()
make_tree(old)
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py", "--data", "/tmp/custom"])
assert os.path.isdir(old) and not os.path.islink(old) and not os.path.lexists(new), "--data run touched the roots"
print("5. --data override skip: OK")

# 6. Default XDG root: $XDG_DATA_HOME, else ~/.local/share.
assert rm._xdg_root() == os.path.join(HOME, ".local", "share", "deckard")
os.environ["XDG_DATA_HOME"] = os.path.join(HOME, "custom-xdg")
assert rm._xdg_root() == os.path.join(HOME, "custom-xdg", "deckard")
os.environ.pop("XDG_DATA_HOME", None)
print("6. XDG root resolution: OK")

# Cross-filesystem copy and atomic publish. _same_filesystem is forced to
# False. The copy logic runs the same on the harness's single temp filesystem.
import contextlib  # noqa: E402
_STAGING_SUFFIX = ".xdg-migrating"


@contextlib.contextmanager
def force_cross_fs():
    orig = rm._same_filesystem
    rm._same_filesystem = lambda src, dest: False
    try:
        yield
    finally:
        rm._same_filesystem = orig


def _boom(*a, **k):
    raise OSError("disk full")


# 7. A cross-filesystem copy keeps the content and makes old a symlink.
old, new = fresh_roots()
make_tree(old)
with open(os.path.join(old, "data", "pages", "Main.json"), "w") as f:
    f.write('{"page": "main"}')
os.symlink("/nonexistent/target", os.path.join(old, "data", "reloc"))  # internal symlink, preserved and not followed
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
with open(os.path.join(new, "data", "pages", "Main.json")) as f:
    assert f.read() == '{"page": "main"}', "copied content mismatch"
assert os.path.islink(os.path.join(new, "data", "reloc")), "internal symlink not preserved (followed?)"
assert os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new), "compat symlink missing/wrong"
assert marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
assert not os.path.lexists(new + _STAGING_SUFFIX), "staging dir left behind"
print("7. cross-fs copy (content preserved, symlinked, cleaned): OK")

# 8. Idempotent re-run across filesystems.
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("8. cross-fs idempotent: OK")

# 9. Resume after publish, with the marker PENDING and old still a real dir.
old, new = fresh_roots()
make_tree(old)
shutil.copytree(old, new)  # a prior run already published the copy
with open(os.path.join(new, rm.XDG_MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")  # marker PENDING, old not yet removed
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new), "old not finalized to symlink"
assert marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("9. resume after publish (old still real): OK")

# 10. Resume after publish, with the marker PENDING and old already deleted.
old, new = fresh_roots()
os.makedirs(os.path.dirname(new), exist_ok=True)
make_tree(new)  # copy published, old removed, symlink not yet made
with open(os.path.join(new, rm.XDG_MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("10. resume after publish (old already deleted): OK")

# 11. A copy failure is non-fatal. Old stays intact and no new tree lands.
old, new = fresh_roots()
make_tree(old)
_orig_copytree = shutil.copytree
shutil.copytree = _boom
try:
    with force_cross_fs():
        rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
finally:
    shutil.copytree = _orig_copytree
assert os.path.isdir(old) and not os.path.islink(old), "old altered on copy failure"
assert not os.path.lexists(new), "new left behind on copy failure"
assert not os.path.lexists(new + _STAGING_SUFFIX), "staging left behind on copy failure"
print("11. copy failure non-fatal: OK")

# 12. Stale staging from a crash is cleaned and rebuilt.
old, new = fresh_roots()
make_tree(old)
os.makedirs(new + _STAGING_SUFFIX)  # leftover partial staging
with open(os.path.join(new + _STAGING_SUFFIX, "junk"), "w") as f:
    f.write("partial")
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.isfile(os.path.join(new, "static", "settings.json")), "rebuilt copy incomplete"
assert not os.path.exists(os.path.join(new, "junk")), "stale staging leaked into new"
assert not os.path.lexists(new + _STAGING_SUFFIX)
print("12. stale staging cleaned + rebuilt: OK")

# 13. A --data override skips the copy path too.
old, new = fresh_roots()
make_tree(old)
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py", "--data", "/tmp/custom"])
assert os.path.isdir(old) and not os.path.islink(old) and not os.path.lexists(new), "--data run touched the roots"
print("13. --data override skip (copy path): OK")

# 14. native_data_root fallback picks the working tree.
base = tempfile.mkdtemp(prefix="root_pick_", dir=HOME)
legacy = os.path.join(base, "legacy")
xdg = os.path.join(base, "xdg")
assert rm.native_data_root(legacy_root=legacy, xdg_root=xdg) == xdg, "fresh should pick XDG"
os.makedirs(legacy)
assert rm.native_data_root(legacy_root=legacy, xdg_root=xdg) == legacy, "deferred move should keep legacy"
os.makedirs(xdg)
assert rm.native_data_root(legacy_root=legacy, xdg_root=xdg) == xdg, "migrated should pick XDG"
print("14. native_data_root fallback: OK")

shutil.rmtree(HOME, ignore_errors=True)
print("scenario_native_xdg_migration: all cases passed")
