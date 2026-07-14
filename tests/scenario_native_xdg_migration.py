"""
Regression test for the native-only var-app -> XDG data-dir migration
(rebrand_migration.migrate_native_var_app_to_xdg): pre-XDG native builds stored
data at ~/.var/app/<id>; this relocates it to $XDG_DATA_HOME/deckard with a
compat symlink, reusing migrate()'s crash-safe core (exhaustively exercised by
scenario_rebrand_migration.py). Here we prove only the XDG-specific wiring:
flatpak is a no-op, the move uses its OWN marker (never the StreamController
one), and --data is honoured.

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

shutil.rmtree(HOME, ignore_errors=True)
print("scenario_native_xdg_migration: all cases passed")
