"""One-time move of the app var-app tree from the pre-rename id to Deckard.

migrate() must run before the globals import, and before anything that imports
globals. globals.py resolves DATA_PATH and makes that directory at import
time, on every invocation, which creates an empty tree under the new id and
breaks the "does the new tree exist" check below. main.py calls migrate()
before its main import block. For the same reason this module uses only the
standard library, plus appinfo and cli_args, which are also stdlib-only and
have no import-time side effects.

The whole ~/.var/app/<id> directory moves, which includes data, static, cache
and config. static/settings.json can hold a custom data-path pointer, so it
must move with the data. A compat symlink stays at the old root, because the
live JSON files, which include deck settings, pages and page backups, hold
absolute paths into the old tree.

The rename and the symlink cannot be atomic together, so a marker file records
the state. The pending marker goes into the old root, with an fsync, just
before the rename, so it travels with the tree. A crash after the rename
leaves a symlink-pending marker in the new tree, and the next start finishes
the work. A file lock serializes the real work, so two first-run launches
cannot race os.rename and rmtree on real user data. The migration never merges
and never deletes user data. If both roots hold files beyond the import-time
skeleton, it stops and reports the conflict.

autostart.py owns the autostart filenames and removes the pre-rename entries
at every launch, so this one-shot path does not handle them.
"""

import contextlib
import os
import shutil
import sys

import appinfo

OLD_ID = appinfo.OLD_APP_ID
NEW_ID = appinfo.APP_ID
OLD_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", OLD_ID))
NEW_ROOT = os.path.expanduser(os.path.join("~", ".var", "app", NEW_ID))

MARKER_NAME = ".migrated-from-" + OLD_ID
LOCK_NAME = ".deckard-migration.lock"
_STATE_PENDING = "symlink-pending"
_STATE_COMPLETE = "complete"

# Marker for the second, native-only migration, migrate_native_var_app_to_xdg.
# Old builds used ~/.var/app/<id> as the data root outside flatpak, and that
# tree moves to $XDG_DATA_HOME/deckard. A separate marker keeps it apart from
# the rename marker above. Both migrations can run, in that order.
XDG_MARKER_NAME = ".migrated-to-xdg"


def _is_flatpak() -> bool:
    return os.path.isfile("/.flatpak-info")


def _log(msg: str) -> None:
    # The logger has no configuration at this point in startup, and must not
    # have one, because its sinks open files inside the tree that this module
    # renames.
    print(f"[rebrand-migration] {msg}", file=sys.stderr)


def _abort(msg: str) -> None:
    _log("FATAL: " + msg)
    raise SystemExit(1)


def _read_marker(marker_path: str) -> str | None:
    try:
        with open(marker_path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write_marker(marker_path: str, state: str) -> bool:
    """Write the marker durably: fsync the file, fsync the directory, replace.

    Returns True on success. A truncated marker that survives a crash matches
    no state, reads as a fresh install, and strands the data with no symlink.
    """
    tmp = marker_path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, (state + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, marker_path)
        dir_fd = os.open(os.path.dirname(marker_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except OSError as e:
        _log(f"could not durably write marker {marker_path} ({e})")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


@contextlib.contextmanager
def _migration_lock(new_root: str):
    """Serialize the migration across concurrent first-run launches.

    The blocking exclusive lock makes a second launch wait, re-read the
    complete marker, and return, instead of racing os.rename and rmtree. The
    migration runs without a lock where fcntl is absent.
    """
    lock_dir = os.path.dirname(new_root)
    fd = None
    try:
        os.makedirs(lock_dir, exist_ok=True)
        fd = os.open(os.path.join(lock_dir, LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError) as e:
            _log(f"could not acquire migration lock ({e}); proceeding without it")
        yield
    finally:
        if fd is not None:
            os.close(fd)


def _old_instance_running() -> bool:
    try:
        from gi.repository import Gio, GLib
        return bool(Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (OLD_ID,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NO_AUTO_START,
            5000,
            None
        ).unpack()[0])
    except Exception as e:
        _log(f"could not probe the session bus for a pre-rename instance ({e}); assuming none")
        return False


def _data_override_active(argv: list[str]) -> bool:
    """True if argv holds a --data override.

    This uses the argparser that globals uses, so abbreviations such as --dat
    match exactly. A parse error reports no override, which lets the migration
    run instead of stranding the real data.
    """
    import cli_args
    try:
        ns, _ = cli_args.argparser.parse_known_args(argv[1:])
    except SystemExit:
        return False
    return ns.data is not None


def _is_skeleton(root: str) -> bool:
    """True only if root is a tree of empty directories.

    globals.py and mp4_tile_cache.py leave that residue at import time. A
    regular file, or a symlink anywhere below, means the tree holds real user
    state, such as a data-relocation symlink, and the migration keeps it.
    """
    if os.path.islink(root):
        return False
    for dirpath, dirnames, filenames in os.walk(root):
        if filenames:
            return False
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                return False
    return True


def _finish_symlink(old_root: str, new_root: str, marker_path: str) -> None:
    """Create (or verify) the compat symlink, then mark the migration done."""
    if os.path.lexists(old_root):
        if os.path.islink(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            pass  # already in place
        else:
            _log(
                f"MIGRATION STUCK: your data was moved to {new_root}, but {old_root} "
                f"reappeared as a real directory (a still-installed pre-rename build "
                f"likely recreated it). The compat symlink can't be created, so pages "
                f"referencing the old path may look empty. Fix: quit/uninstall the old "
                f"build, delete {old_root}, and restart Deckard."
            )
            return
    else:
        try:
            os.symlink(new_root, old_root)
            _log(f"compat symlink {old_root} -> {new_root}")
        except OSError as e:
            _log(f"could not create compat symlink ({e}); will retry next start")
            return
    # A failed complete-marker write recovers by itself. The marker that
    # travelled with the rename still reads pending, so the next start
    # re-enters _finish_symlink through the pending branch and retries.
    _write_marker(marker_path, _STATE_COMPLETE)


def migrate(old_root: str = OLD_ROOT, new_root: str = NEW_ROOT,
            argv: list[str] | None = None, require_pre_globals: bool = True,
            marker_name: str = MARKER_NAME, running_check=None, locked_fn=None) -> None:
    if require_pre_globals and "globals" in sys.modules:
        raise AssertionError(
            "rebrand_migration.migrate() must run before `import globals` -- "
            "globals creates the data dir at import time and poisons the checks below"
        )

    argv = sys.argv if argv is None else argv
    if _data_override_active(argv):
        _log("--data override active; skipping data-dir migration")
        return

    if running_check is None:
        running_check = _old_instance_running
    if locked_fn is None:
        locked_fn = _migrate_locked
    marker_path = os.path.join(new_root, marker_name)
    # Fast paths without the lock for the common cases, already migrated and
    # fresh install. Each does one marker read, takes no lock, and writes no
    # lock file.
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state is None and not os.path.lexists(old_root):
        return  # fresh install, nothing to migrate

    # The real work runs under the lock, and re-reads the state inside, in
    # case another launch completed the migration during the wait.
    with _migration_lock(new_root):
        locked_fn(old_root, new_root, marker_path, running_check)


def _migrate_locked(old_root: str, new_root: str, marker_path: str,
                    running_check) -> None:
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state == _STATE_PENDING:
        # A previous start crashed between the rename and the symlink.
        _finish_symlink(old_root, new_root, marker_path)
        return

    if not os.path.lexists(old_root):
        return  # fresh install, nothing to migrate

    if os.path.islink(old_root):
        if os.path.exists(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            # The compat link exists but the marker is absent, so an earlier
            # marker write failed. The data is already at the new root.
            _write_marker(marker_path, _STATE_COMPLETE)
            return
        _abort(
            f"{old_root} is a symlink but does not resolve to {new_root} (broken or "
            f"foreign). Refusing to touch it -- resolve it manually, then restart."
        )

    # old_root is a real directory holding the pre-rename data.
    if running_check():
        _abort(
            f"a pre-rename instance still owns {OLD_ID} on the session bus. Quit the "
            f"running StreamController first, then start Deckard again. Renaming the "
            f"data dir under a live instance would split its writes across two trees."
        )

    if os.path.lexists(new_root):
        if os.path.islink(new_root):
            _abort(
                f"{new_root} is a symlink; the migration expects to create it as a "
                f"real directory. Resolve it manually, then restart."
            )
        if _is_skeleton(new_root):
            _log(f"removing empty skeleton at {new_root} (import-time makedirs residue)")
            shutil.rmtree(new_root)
        else:
            _abort(
                f"both {old_root} and {new_root} contain files. Refusing to merge or "
                f"delete either. Move one of them aside manually, then restart."
            )

    # The pending marker travels with the rename. A marker that is not durable
    # is the one unrecoverable state, so the rename does not start without it.
    # old_root stays intact, so the next start retries cleanly.
    if not _write_marker(os.path.join(old_root, os.path.basename(marker_path)), _STATE_PENDING):
        _abort(
            f"could not durably write the migration marker into {old_root}; refusing "
            f"to rename without it. Fix permissions/space on that path and restart."
        )
    try:
        os.rename(old_root, new_root)
    except OSError as e:
        _abort(f"could not move {old_root} -> {new_root}: {e}")
    _log(f"moved {old_root} -> {new_root}")
    _finish_symlink(old_root, new_root, marker_path)


def _xdg_root() -> str:
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(os.path.join("~", ".local", "share"))
    return os.path.join(xdg, "deckard")


def _same_filesystem(src: str, dest: str) -> bool:
    """True if src and the place for dest share one filesystem.

    os.rename then cannot fail with EXDEV. dest usually does not exist, so this
    probes its nearest existing ancestor. An unknown result reports True, and
    migrate() then tries the rename and reports a real failure.
    """
    probe = dest
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return os.stat(src).st_dev == os.stat(probe).st_dev
    except OSError:
        return True


def native_data_root(legacy_root: str = NEW_ROOT, xdg_root: str | None = None) -> str:
    """Data root for a native install, the XDG dir or the old tree.

    The result is the old ~/.var/app/<id> tree when it exists and the XDG dir
    does not, which means the relocation stopped or waited. The app then runs
    from the old location instead of starting empty. After a successful move
    the old path is a symlink to the XDG dir, so the result is the XDG path.
    """
    xdg_root = xdg_root or _xdg_root()
    if os.path.exists(xdg_root) or not os.path.exists(legacy_root):
        return xdg_root
    return legacy_root


def _safe_rmtree(path: str) -> None:
    try:
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
    except OSError as e:
        _log(f"could not remove {path} ({e})")


def _fsync_tree(root: str) -> None:
    """fsync every regular file and every directory below root.

    The publish rename makes the directory entry consistent. This call makes
    the file contents durable before the migration deletes the source. It skips
    symlinks, never follows them, and ignores errors.
    """
    for dirpath, _dirnames, filenames in os.walk(root):  # followlinks=False
        for name in filenames:
            fp = os.path.join(dirpath, name)
            if os.path.islink(fp):
                continue
            try:
                fd = os.open(fp, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        try:
            dfd = os.open(dirpath, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


def _finish_copy(old_root: str, new_root: str, marker_path: str) -> None:
    """Finish a published copy at new_root.

    The pending marker in new_root proves the copy is complete and durable.
    This removes the original directory, then calls _finish_symlink. A failed
    removal keeps the marker pending, and the next start retries the cleanup.
    """
    if os.path.isdir(old_root) and not os.path.islink(old_root):
        try:
            shutil.rmtree(old_root)
        except OSError as e:
            _log(f"copy migration: could not remove the old tree {old_root} ({e}); "
                 f"the app runs from {new_root}, cleanup retries next start")
            return  # leave the marker pending
    _finish_symlink(old_root, new_root, marker_path)


def _copy_migrate_locked(old_root: str, new_root: str, marker_path: str,
                         running_check) -> None:
    """Cross-filesystem variant of _migrate_locked.

    os.rename cannot cross a filesystem. This copies old_root to a staging
    sibling on the new_root filesystem, fsyncs it, marks it pending, renames it
    into place, and only then removes the original. Each crash point recovers.
    A crash before the publish leaves old_root intact, and the copy repeats. A
    crash after the publish leaves a pending marker, and _finish_copy completes
    the work. This function ignores running_check.
    """
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state == _STATE_PENDING:
        # A prior run published the copy to new_root. Finish the cleanup.
        _finish_copy(old_root, new_root, marker_path)
        return

    if not os.path.lexists(old_root):
        return  # nothing to copy

    if os.path.islink(old_root):
        if os.path.exists(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            _write_marker(marker_path, _STATE_COMPLETE)  # backfill a lost marker
            return
        _abort(
            f"{old_root} is a symlink but does not resolve to {new_root} (broken or "
            f"foreign). Refusing to touch it -- resolve it manually, then restart."
        )

    # old_root is a real directory. Guard new_root like the rename path does.
    # Never merge into, and never overwrite, real user data at new_root.
    if os.path.lexists(new_root):
        if os.path.islink(new_root):
            _abort(
                f"{new_root} is a symlink; the migration expects to create it as a "
                f"real directory. Resolve it manually, then restart."
            )
        if _is_skeleton(new_root):
            _log(f"removing empty skeleton at {new_root} (import-time makedirs residue)")
            shutil.rmtree(new_root)
        else:
            _abort(
                f"both {old_root} and {new_root} contain files. Refusing to merge or "
                f"delete either. Move one of them aside manually, then restart."
            )

    # Build the copy in a staging sibling on the new_root filesystem, then
    # publish it with an atomic same-filesystem rename. old_root stays untouched
    # until the pending marker proves the copy durable and complete.
    staging = new_root + ".xdg-migrating"
    _safe_rmtree(staging)  # drop any partial staging from an earlier crash
    try:
        shutil.copytree(old_root, staging, symlinks=True)
    except OSError as e:
        _safe_rmtree(staging)
        _log(f"copy migration: copying {old_root} failed ({e}); the app keeps using "
             f"{old_root}, will retry next start")
        return
    # The pending marker goes into staging last, so a marker in the published
    # new_root means the copy is complete and durable. _finish_copy reads that
    # marker before it deletes old_root.
    if not _write_marker(os.path.join(staging, os.path.basename(marker_path)), _STATE_PENDING):
        _safe_rmtree(staging)
        _log("copy migration: could not durably mark the staged copy; will retry next start")
        return
    _fsync_tree(staging)
    try:
        os.rename(staging, new_root)  # same filesystem, so the publish is atomic
    except OSError as e:
        _safe_rmtree(staging)
        _log(f"copy migration: publishing the staged copy failed ({e}); will retry next start")
        return
    _log(f"copied {old_root} -> {new_root}")
    _finish_copy(old_root, new_root, marker_path)


def migrate_native_var_app_to_xdg(old_root: str = NEW_ROOT, xdg_root: str | None = None,
                                  argv: list[str] | None = None,
                                  require_pre_globals: bool = True) -> None:
    """Move the native data root from ~/.var/app/<id> to $XDG_DATA_HOME/deckard.

    This runs once and leaves a compat symlink. It does nothing under flatpak,
    where ~/.var/app/<id> is the correct per-app data root. Call it after
    migrate(), so the rename lands in ~/.var/app/<id> first.

    It reuses the crash-safe machinery of migrate() with its own marker, and it
    differs in two points. It runs no running-instance check, because it moves
    the tree of the same app, and the compat symlink keeps a live instance
    writing into one tree. One known limitation stays. Between the publish and
    the symlink, a live instance that opens a new absolute path gets ENOENT.
    A cross-filesystem move copies and then publishes, because os.rename fails
    with EXDEV. A same-filesystem move keeps the atomic rename. A copy that
    fails part-way is not fatal, because native_data_root() keeps the app on
    ~/.var/app/<id> until a later start finishes the move.
    """
    if _is_flatpak():
        return
    new_root = xdg_root or _xdg_root()
    # The mover is an atomic rename when old_root and the destination share a
    # filesystem, and a crash-safe copy otherwise. A resumed migration picks the same
    # route, because a cross-filesystem pair stays cross-filesystem. An old_root
    # that is already a symlink takes the rename path, which returns at once on
    # the complete marker.
    cross_fs = (os.path.isdir(old_root) and not os.path.islink(old_root)
                and not _same_filesystem(old_root, new_root))
    migrate(old_root=old_root, new_root=new_root, argv=argv,
            require_pre_globals=require_pre_globals,
            marker_name=XDG_MARKER_NAME, running_check=lambda: False,
            locked_fn=_copy_migrate_locked if cross_fs else None)
