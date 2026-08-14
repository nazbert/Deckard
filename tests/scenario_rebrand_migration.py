"""
Regression test for the one-time rename migration in rebrand_migration.py.

The whole pre-rename var-app tree moves to the new id and leaves a compat
symlink behind. The migration never merges when both roots hold real files,
heals a crash between the rename and the symlink through the pending marker,
and aborts on a foreign symlink or a live pre-rename instance.
"""
import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Redirect HOME before importing the module. The autostart cleanup resolves
# ~/.config/autostart at call time through expanduser.
HOME = tempfile.mkdtemp(prefix="rebrand_home_")
os.environ["HOME"] = HOME

import rebrand_migration as rm  # noqa: E402

assert "globals" not in sys.modules, "rebrand_migration must not pull in globals"

rm._old_instance_running = lambda: False  # no session bus in the harness


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


# 1. A fresh install has neither root, so nothing happens.
old, new = fresh_roots()
rm.migrate(old, new, argv=["main.py"])
assert not os.path.lexists(old) and not os.path.lexists(new)
print("1. fresh install no-op: OK")

# 2. The normal move. The stale-autostart cleanup lives in
# autostart.remove_legacy_autostart_entries, covered by
# scenario_autostart_disable.py.
old, new = fresh_roots()
make_old_tree(old)
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
print("2. normal move: OK")

# 3. An idempotent re-run.
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
print("3. idempotent re-run: OK")

# 4. A new root poisoned by the import-time makedirs skeleton.
old, new = fresh_roots()
make_old_tree(old)
make_skeleton(new)
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
assert not os.path.exists(os.path.join(new, "data", "cache", "videos")), "skeleton merged instead of replaced"
print("4. skeleton-poisoned new root: OK")

# 5. Both roots hold real files, so the migration aborts and touches nothing.
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

# 6. A foreign symlink at the old root aborts the migration.
old, new = fresh_roots()
elsewhere = tempfile.mkdtemp(prefix="elsewhere_", dir=HOME)
os.makedirs(os.path.dirname(old), exist_ok=True)
os.symlink(elsewhere, old)
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
assert os.path.realpath(old) == os.path.realpath(elsewhere), "foreign symlink replaced"
print("6. foreign symlink abort: OK")

# 7. A broken symlink at the old root aborts the migration.
old, new = fresh_roots()
os.makedirs(os.path.dirname(old), exist_ok=True)
os.symlink(os.path.join(HOME, "does-not-exist"), old)
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
print("7. broken symlink abort: OK")

# 8. Repair mode after a crash between the rename and the symlink.
old, new = fresh_roots()
make_old_tree(old)
os.makedirs(os.path.dirname(new), exist_ok=True)
with open(os.path.join(old, rm.MARKER_NAME), "w") as f:
    f.write(rm._STATE_PENDING + "\n")
os.rename(old, new)  # the crash point, renamed with no symlink and marker pending
rm.migrate(old, new, argv=["main.py"])
assert_migrated(old, new)
print("8. repair after rename/symlink crash: OK")

# 9. A pending marker with the old root reappeared as a real dir.
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

# 10. A --data override skips everything.
old, new = fresh_roots()
make_old_tree(old)
rm.migrate(old, new, argv=["main.py", "--data", "/tmp/custom"])
assert os.path.isdir(old) and not os.path.lexists(new), "--data run touched the roots"
rm.migrate(old, new, argv=["main.py", "--data=/tmp/custom"])
assert os.path.isdir(old) and not os.path.lexists(new)
print("10. --data override skip: OK")

# 11. A live pre-rename instance aborts before any mutation.
old, new = fresh_roots()
make_old_tree(old)
rm._old_instance_running = lambda: True
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
assert os.path.isdir(old) and not os.path.islink(old)
assert not os.path.lexists(new)
rm._old_instance_running = lambda: False
print("11. live old-instance abort: OK")

# 12. The compat symlink is already in place but the marker is missing.
old, new = fresh_roots()
make_old_tree(old)
os.rename(old, new)
os.symlink(new, old)  # migration done by hand, or the marker write failed
rm.migrate(old, new, argv=["main.py"])
assert marker_state(new) == rm._STATE_COMPLETE
assert_migrated(old, new)
print("12. marker backfill on existing symlink: OK")

# 14. argparse accepts any unambiguous prefix of --data, and only --data and
# --devel start with --d, so --dat and --da are overrides too.
for abbrev in ("--dat", "--da"):
    old, new = fresh_roots()
    make_old_tree(old)
    rm.migrate(old, new, argv=["main.py", abbrev, "/tmp/custom"])
    assert os.path.isdir(old) and not os.path.lexists(new), f"{abbrev} did not skip"
print("14. --data abbreviations (--dat, --da) skip: OK")

# 15. A dir-symlink in the new root is not skeleton, so the migration aborts.
old, new = fresh_roots()
make_old_tree(old)
target = tempfile.mkdtemp(prefix="relocation_", dir=HOME)
os.makedirs(os.path.join(new, "data"))
os.symlink(target, os.path.join(new, "data", "plugins"))  # user relocation, no plain files
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
assert os.path.islink(os.path.join(new, "data", "plugins")), "relocation symlink deleted as skeleton"
assert os.path.isdir(old) and not os.path.islink(old), "old tree mutated on abort"
print("15. dir-symlink not treated as skeleton: OK")

# 16. A new root that is itself a symlink aborts instead of crashing rmtree.
old, new = fresh_roots()
make_old_tree(old)
elsewhere = tempfile.mkdtemp(prefix="newlink_", dir=HOME)
os.makedirs(os.path.dirname(new), exist_ok=True)
os.symlink(elsewhere, new)
expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
assert os.path.islink(new) and os.path.realpath(new) == os.path.realpath(elsewhere)
print("16. symlinked new root abort: OK")

# 17. An undurable pending-marker write aborts before the rename.
old, new = fresh_roots()
make_old_tree(old)
os.chmod(old, 0o500)  # deny file creation in old_root, so the marker write fails
try:
    expect_exit(lambda: rm.migrate(old, new, argv=["main.py"]))
finally:
    os.chmod(old, 0o700)
assert os.path.isdir(old) and not os.path.lexists(new), "renamed despite undurable marker"
assert os.path.isfile(os.path.join(old, "data", "settings", "settings.json"))
print("17. undurable marker aborts before rename: OK")

# 13. The pre-globals contract.
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
