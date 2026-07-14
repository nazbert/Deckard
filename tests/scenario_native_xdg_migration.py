"""
Regression test for the native-only var-app -> XDG data-dir migration
(rebrand_migration.migrate_native_var_app_to_xdg): pre-XDG native builds stored
data at ~/.var/app/<id>; this relocates it to $XDG_DATA_HOME/deckard with a
compat symlink.

Same-filesystem uses migrate()'s atomic-rename core (exhaustively exercised by
scenario_rebrand_migration.py); here we cover the XDG-specific wiring (flatpak
no-op, own marker, --data skip, native_data_root fallback) and the
cross-filesystem copy+atomic-publish path with its crash-safety states: resume
after publish with the old tree still present or already deleted, non-fatal copy
failure, and stale-staging cleanup.

Stdlib-only, like the module under test; globals must never be imported.
"""
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

rm._is_flatpak = lambda: False  # default: native


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


# --- 1. flatpak -> no-op (the ~/.var/app dir IS correct there) ----------
old, new = fresh_roots()
make_tree(old)
rm._is_flatpak = lambda: True
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.isdir(old) and not os.path.islink(old), "flatpak run moved the tree"
assert not os.path.lexists(new)
rm._is_flatpak = lambda: False
print("1. flatpak no-op: OK")

# --- 2. fresh native (no var-app tree) -> no-op -------------------------
old, new = fresh_roots()
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert not os.path.lexists(old) and not os.path.lexists(new)
print("2. fresh native no-op: OK")

# --- 3. normal native move ----------------------------------------------
old, new = fresh_roots()
make_tree(old)
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.isfile(os.path.join(new, "static", "settings.json"))
assert os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new), "compat symlink missing/wrong"
assert os.path.isfile(os.path.join(old, "data", "pages", "Main.json")), "old path does not resolve through link"
assert marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
assert marker_state(new, rm.MARKER_NAME) is None, "used the StreamController marker instead of the XDG one"
print("3. normal native move: OK")

# --- 4. idempotent re-run ------------------------------------------------
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("4. idempotent re-run: OK")

# --- 5. --data override skips everything --------------------------------
old, new = fresh_roots()
make_tree(old)
rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py", "--data", "/tmp/custom"])
assert os.path.isdir(old) and not os.path.islink(old) and not os.path.lexists(new), "--data run touched the roots"
print("5. --data override skip: OK")

# --- 6. default XDG root: $XDG_DATA_HOME, else ~/.local/share -----------
assert rm._xdg_root() == os.path.join(HOME, ".local", "share", "deckard")
os.environ["XDG_DATA_HOME"] = os.path.join(HOME, "custom-xdg")
assert rm._xdg_root() == os.path.join(HOME, "custom-xdg", "deckard")
os.environ.pop("XDG_DATA_HOME", None)
print("6. XDG root resolution: OK")

# ======================================================================
# Cross-filesystem: copy + atomic-publish path. Forced by pretending the
# roots differ (rm._same_filesystem -> False); the copy logic itself runs
# identically on the single temp filesystem the harness uses.
# ======================================================================
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


# --- 7. cross-fs copy: content preserved, symlink kept, old->symlink -----
old, new = fresh_roots()
make_tree(old)
with open(os.path.join(old, "data", "pages", "Main.json"), "w") as f:
    f.write('{"page": "main"}')
os.symlink("/nonexistent/target", os.path.join(old, "data", "reloc"))  # internal symlink: must be preserved, not followed
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
with open(os.path.join(new, "data", "pages", "Main.json")) as f:
    assert f.read() == '{"page": "main"}', "copied content mismatch"
assert os.path.islink(os.path.join(new, "data", "reloc")), "internal symlink not preserved (followed?)"
assert os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new), "compat symlink missing/wrong"
assert marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
assert not os.path.lexists(new + _STAGING_SUFFIX), "staging dir left behind"
print("7. cross-fs copy (content preserved, symlinked, cleaned): OK")

# --- 8. idempotent re-run ------------------------------------------------
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("8. cross-fs idempotent: OK")

# --- 9. resume: published (new=PENDING), old still a real dir -----------
old, new = fresh_roots()
make_tree(old)
shutil.copytree(old, new)  # copy already published on a prior run...
with open(os.path.join(new, rm.XDG_MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")  # ...marker PENDING, old NOT yet removed
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new), "old not finalized to symlink"
assert marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("9. resume after publish (old still real): OK")

# --- 10. resume: published (new=PENDING), old already deleted -----------
old, new = fresh_roots()
os.makedirs(os.path.dirname(new), exist_ok=True)
make_tree(new)  # copy published, old removed, symlink not yet made
with open(os.path.join(new, rm.XDG_MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py"])
assert os.path.islink(old) and marker_state(new, rm.XDG_MARKER_NAME) == rm._STATE_COMPLETE
print("10. resume after publish (old already deleted): OK")

# --- 11. copy failure is non-fatal: old intact, no partial new ----------
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

# --- 12. stale staging from a prior crash is cleaned + rebuilt ----------
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

# --- 13. --data override skips the copy path too ------------------------
old, new = fresh_roots()
make_tree(old)
with force_cross_fs():
    rm.migrate_native_var_app_to_xdg(old_root=old, xdg_root=new, argv=["main.py", "--data", "/tmp/custom"])
assert os.path.isdir(old) and not os.path.islink(old) and not os.path.lexists(new), "--data run touched the roots"
print("13. --data override skip (copy path): OK")

# --- 14. native_data_root fallback picks the working tree ---------------
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
