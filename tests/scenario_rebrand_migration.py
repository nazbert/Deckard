"""
Regression test for the one-time rename migration (rebrand_migration.py,
docs/rename-deckard-plan.md Phase 2): the whole pre-rename var-app tree
moves to the new id with a compat symlink left behind, guarded against
every state the ordering audit found:

  * the "new dir missing" check must survive the import-time makedirs
    skeleton (globals.py / mp4_tile_cache.py create empty dirs on every
    invocation before main() runs);
  * never merge or delete when both roots hold real files;
  * a crash between rename and symlink is healed on the next start via the
    pending marker (which is written into the OLD root and travels with the
    rename);
  * foreign or broken symlinks at the old root abort rather than being
    replaced;
  * a live pre-rename instance (old bus name owned) aborts before any
    filesystem mutation;
  * --data overrides skip the migration entirely;
  * completion removes the stale autostart entries under the old identity.

Deliberately does NOT import fixtures: the module under test is stdlib-only
and must stay importable before `import globals`; this scenario proves that
property too (globals must never enter sys.modules here).
"""
import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Redirect HOME before importing the module: the autostart cleanup resolves
# ~/.config/autostart at call time via expanduser.
HOME = tempfile.mkdtemp(prefix="rebrand_home_")
os.environ["HOME"] = HOME

import rebrand_migration as rm  # noqa: E402

assert "globals" not in sys.modules, "rebrand_migration must not pull in globals"

rm._old_instance_running = lambda: False  # no session bus in the harness

AUTOSTART_DIR = os.path.join(HOME, ".config", "autostart")


def fresh_roots():
    base = tempfile.mkdtemp(prefix="rebrand_roots_", dir=HOME)
    return os.path.join(base, rm.OLD_ID), os.path.join(base, rm.NEW_ID)


def make_old_tree(old_root):
    os.makedirs(os.path.join(old_root, "data", "settings"))
    os.makedirs(os.path.join(old_root, "data", "pages"))
    os.makedirs(os.path.join(old_root, "static"))
    with open(os.path.join(old_root, "data", "settings", "settings.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(old_root, "data", "pages", "Main.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(old_root, "static", "settings.json"), "w") as f:
        f.write("{}")


def make_skeleton(new_root):
    # exactly what globals.py + mp4_tile_cache.py leave behind at import time
    os.makedirs(os.path.join(new_root, "data", "plugins"))
    os.makedirs(os.path.join(new_root, "data", "cache", "videos"))


def make_stale_autostart():
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    for name in rm._STALE_AUTOSTART_NAMES:
        with open(os.path.join(AUTOSTART_DIR, name), "w") as f:
            f.write("[Desktop Entry]\n")
    # an unrelated entry that must survive
    with open(os.path.join(AUTOSTART_DIR, "opendeck.desktop"), "w") as f:
        f.write("[Desktop Entry]\n")


def marker_state(new_root):
    try:
        with open(os.path.join(new_root, rm.MARKER_NAME)) as f:
            return f.read().strip()
    except OSError:
        return None


def assert_migrated(old_root, new_root):
    assert os.path.isfile(os.path.join(new_root, "data", "settings", "settings.json"))
    assert os.path.isfile(os.path.join(new_root, "static", "settings.json"))
    assert os.path.islink(old_root), "compat symlink missing at old root"
    assert os.path.realpath(old_root) == os.path.realpath(new_root)
    # embedded absolute old paths must resolve through the link
    assert os.path.isfile(os.path.join(old_root, "data", "pages", "Main.json"))
    assert marker_state(new_root) == rm._STATE_COMPLETE


def expect_exit(fn):
    try:
        fn()
    except SystemExit as e:
        assert e.code == 1
        return
    raise AssertionError("expected SystemExit(1)")


# --- 1. fresh install: neither root exists -> no-op --------------------
old, new = fresh_roots()
rm.migrate(old, new, argv=["main.py"])
assert not os.path.lexists(old) and not os.path.lexists(new)
print("1. fresh install no-op: OK")

# --- 2. normal move + autostart cleanup --------------------------------
old, new = fresh_roots()
make_old_tree(old)
make_stale_autostart()
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
for name in rm._STALE_AUTOSTART_NAMES:
    assert not os.path.exists(os.path.join(AUTOSTART_DIR, name)), f"stale {name} not removed"
assert os.path.exists(os.path.join(AUTOSTART_DIR, "opendeck.desktop")), "unrelated entry removed"
print("2. normal move + autostart cleanup: OK")

# --- 3. idempotent re-run ----------------------------------------------
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
print("3. idempotent re-run: OK")

# --- 4. skeleton-poisoned new root (import-time makedirs residue) ------
old, new = fresh_roots()
make_old_tree(old)
make_skeleton(new)
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
assert not os.path.exists(os.path.join(new, "data", "cache", "videos")), "skeleton merged instead of replaced"
print("4. skeleton-poisoned new root: OK")

# --- 5. both roots hold real files -> abort, nothing touched -----------
old, new = fresh_roots()
make_old_tree(old)
os.makedirs(os.path.join(new, "data", "logs"))
with open(os.path.join(new, "data", "logs", "logs.log"), "w") as f:
    f.write("x")
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
assert os.path.isdir(old) and not os.path.islink(old), "old root mutated on abort"
assert os.path.isfile(os.path.join(old, "data", "settings", "settings.json"))
assert os.path.isfile(os.path.join(new, "data", "logs", "logs.log"))
print("5. both-have-files abort: OK")

# --- 6. foreign symlink at old root -> abort ----------------------------
old, new = fresh_roots()
elsewhere = tempfile.mkdtemp(prefix="elsewhere_", dir=HOME)
os.makedirs(os.path.dirname(old), exist_ok=True)
os.symlink(elsewhere, old)
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
assert os.path.realpath(old) == os.path.realpath(elsewhere), "foreign symlink replaced"
print("6. foreign symlink abort: OK")

# --- 7. broken symlink at old root -> abort -----------------------------
old, new = fresh_roots()
os.makedirs(os.path.dirname(old), exist_ok=True)
os.symlink(os.path.join(HOME, "does-not-exist"), old)
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
print("7. broken symlink abort: OK")

# --- 8. repair mode: crashed between rename and symlink -----------------
old, new = fresh_roots()
make_old_tree(old)
os.makedirs(os.path.dirname(new), exist_ok=True)
with open(os.path.join(old, rm.MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")
os.rename(old, new)  # the crash point: renamed, no symlink, marker pending
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
print("8. repair after rename/symlink crash: OK")

# --- 9. pending marker but old root reappeared as a real dir ------------
old, new = fresh_roots()
make_old_tree(old)
with open(os.path.join(old, rm.MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")
os.rename(old, new)
os.makedirs(os.path.join(old, "data"))  # an old build recreated the tree
rm.migrate(old, new, argv=["main.py"])  # must not raise, must not delete
assert marker_state(new) == rm._STATE_PENDING, "completed despite blocked symlink"
assert os.path.isdir(os.path.join(old, "data")), "reappeared old tree deleted"
print("9. pending + reappeared old root stays pending: OK")

# --- 10. --data override skips everything -------------------------------
old, new = fresh_roots()
make_old_tree(old)
rm.migrate(old, new, argv=["main.py", "--data", "/tmp/custom"])
assert os.path.isdir(old) and not os.path.lexists(new), "--data run touched the roots"
rm.migrate(old, new, argv=["main.py", "--data=/tmp/custom"])
assert os.path.isdir(old) and not os.path.lexists(new)
print("10. --data override skip: OK")

# --- 11. live pre-rename instance -> abort before any mutation ----------
old, new = fresh_roots()
make_old_tree(old)
rm._old_instance_running = lambda: True
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
assert os.path.isdir(old) and not os.path.islink(old)
assert not os.path.lexists(new)
rm._old_instance_running = lambda: False
print("11. live old-instance abort: OK")

# --- 12. our symlink already in place but marker missing -> completes ---
old, new = fresh_roots()
make_old_tree(old)
os.rename(old, new)
os.symlink(new, old)  # migration done by hand / marker write failed earlier
rm.migrate(old, new, argv=["main.py"])
assert marker_state(new) == rm._STATE_COMPLETE
assert_migrated(old, new)
print("12. marker backfill on existing symlink: OK")

# --- 13. pre-globals contract ------------------------------------------
class _FakeGlobals:  # simulate globals already imported
    pass

sys.modules["globals"] = _FakeGlobals()
try:
    rm.migrate(old, new, argv=["main.py"], require_pre_globals=True)
except AssertionError:
    print("13. pre-globals contract enforced: OK")
else:
    raise AssertionError("migrate() ran after `import globals`")
finally:
    del sys.modules["globals"]

shutil.rmtree(HOME, ignore_errors=True)
print("scenario_rebrand_migration: all cases passed")
