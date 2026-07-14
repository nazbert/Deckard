"""
One-time whole-directory migration of the app's var-app tree from the
pre-rename id (com.core447.StreamController) to the Deckard id.

Hard constraint: migrate() must run before `import globals` (and before
anything that imports it). globals.py resolves DATA_PATH and os.makedirs()
it at *import* time -- on every invocation, including CLI early-return runs
-- which would create an empty skeleton under the NEW id and poison the
"does the new tree exist" check below. main.py calls migrate() right after
the patcher, before its main import block. This module is stdlib-only for
the same reason; it imports only `appinfo` and `cli_args`, both of which are
themselves stdlib-only and side-effect-free (dbus/fcntl are imported lazily).

Design (docs/rename-deckard-plan.md, Phase 2):

- The WHOLE ~/.var/app/<id> dir moves (data/ + static/ + flatpak-era cache/
  and config/): static/settings.json can carry a custom data-path pointer
  and must relocate with the data.
- A compat symlink is left at the old root: live JSON (deck settings, pages
  incl. backups) embeds absolute paths into the old tree.
- rename+symlink cannot be atomic together, so a marker file tracks the
  state instead. The pending marker is written -- durably (fsync) -- into
  the OLD root immediately before the rename and therefore travels with it:
  any crash after the rename leaves a "symlink-pending" marker in the new
  tree, which the next start finishes.
- The real work is serialized by a file lock so two first-run launches
  cannot race os.rename/rmtree on real user data.
- Never merge, never delete user data: if both roots contain files beyond
  the known import-time skeleton, refuse loudly and let the user resolve it.

Stale pre-rename autostart entries are NOT handled here -- autostart.py owns
autostart filenames and removes the legacy ones on every launch (self-
healing), which this one-shot path could not be.
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

# Second, native-only migration (migrate_native_var_app_to_xdg below): pre-XDG
# builds used ~/.var/app/<id> as the data root even outside flatpak; relocate
# that to $XDG_DATA_HOME/deckard. Its own marker so it can't collide with the
# StreamController->Deckard marker above -- both may run, in that order.
XDG_MARKER_NAME = ".migrated-to-xdg"


def _is_flatpak() -> bool:
    return os.path.isfile("/.flatpak-info")


def _log(msg: str) -> None:
    # The logger is not configured yet at this point in startup (and must not
    # be: its sinks would open files inside the tree being renamed).
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
    """Durably write the marker (fsync file + fsync dir + atomic replace).

    Returns True on success. Durability is load-bearing: a truncated or
    zero-length marker surviving a crash reads as neither state and would be
    treated as a fresh install, stranding the migrated data with no compat
    symlink.
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

    A blocking exclusive lock on ~/.var/app/<lock>: a second launch waits
    here, then re-reads the (now COMPLETE) marker inside and no-ops, instead
    of racing os.rename/rmtree. Degrades to no lock where fcntl is
    unavailable, which is no worse than before this guard existed.
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
        import dbus
        return bool(dbus.SessionBus().name_has_owner(OLD_ID))
    except Exception as e:
        _log(f"could not probe the session bus for a pre-rename instance ({e}); assuming none")
        return False


def _data_override_active(argv: list[str]) -> bool:
    """True if a --data override is present, resolved by the SAME argparser
    globals uses -- so argparse abbreviations (--dat, --da) and flag/value
    disambiguation match globals exactly instead of a fragile string guess.
    On a parse error (e.g. an ambiguous flag) assume no override, which lets
    the migration run rather than strand the real data unmigrated.
    """
    import cli_args
    try:
        ns, _ = cli_args.argparser.parse_known_args(argv[1:])
    except SystemExit:
        return False
    return ns.data is not None


def _is_skeleton(root: str) -> bool:
    """True only if `root` is a tree of empty directories -- the import-time
    makedirs residue globals.py / mp4_tile_cache.py leave behind.

    Any regular file, or ANY symlink anywhere below (a directory-symlink is
    reported by os.walk in dirnames, unfollowed; a file-symlink in
    filenames), means the tree holds real user state -- e.g. a data-
    relocation symlink -- and is not ours to delete.
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
    # A failed complete-marker write is self-healing: the marker that
    # travelled with the rename still reads "pending", so the next start
    # re-enters _finish_symlink via the pending branch and retries.
    _write_marker(marker_path, _STATE_COMPLETE)


def migrate(old_root: str = OLD_ROOT, new_root: str = NEW_ROOT,
            argv: list[str] | None = None, require_pre_globals: bool = True,
            marker_name: str = MARKER_NAME, running_check=None) -> None:
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
    marker_path = os.path.join(new_root, marker_name)
    # Lock-free fast paths for the common cases (already migrated / fresh
    # install): one marker read, no lock, no lock-file litter.
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state is None and not os.path.lexists(old_root):
        return  # fresh install -- nothing to migrate

    # Real work (migrate or finish a pending symlink): under the lock, and
    # re-check state inside in case another launch completed it while we
    # waited.
    with _migration_lock(new_root):
        _migrate_locked(old_root, new_root, marker_path, running_check)


def _migrate_locked(old_root: str, new_root: str, marker_path: str,
                    running_check) -> None:
    state = _read_marker(marker_path)
    if state == _STATE_COMPLETE:
        return
    if state == _STATE_PENDING:
        # Crashed (or failed) between rename and symlink on a previous start.
        _finish_symlink(old_root, new_root, marker_path)
        return

    if not os.path.lexists(old_root):
        return  # fresh install -- nothing to migrate

    if os.path.islink(old_root):
        if os.path.exists(old_root) and os.path.realpath(old_root) == os.path.realpath(new_root):
            # Our compat link, but the marker is missing (e.g. marker write
            # failed earlier): data is already at the new root.
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

    # The pending marker travels with the rename (see module docstring). A
    # non-durable marker here is the one unrecoverable state, so refuse to
    # rename without it -- old_root is still intact, so this is a clean retry.
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
    """True if `src` and where `dest` would be created live on one filesystem, so
    os.rename won't fail with EXDEV. `dest` usually does not exist yet, so probe
    its nearest existing ancestor (mkdir creates on the parent's filesystem).
    Assume same-fs when it can't be determined: migrate() then attempts the
    rename and aborts loudly on a genuine failure, rather than silently skipping.
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
    """Data root for a native (non-flatpak) install: the XDG dir, falling back to
    the pre-XDG ~/.var/app/<id> tree when it still exists and the XDG dir does not
    -- i.e. the relocation was deferred or skipped (e.g. across a filesystem
    boundary). Keeps the app working from the old location instead of starting
    empty. After a successful move the legacy path is a symlink to the XDG dir, so
    the first clause wins and this returns the XDG path.
    """
    xdg_root = xdg_root or _xdg_root()
    if os.path.exists(xdg_root) or not os.path.exists(legacy_root):
        return xdg_root
    return legacy_root


def migrate_native_var_app_to_xdg(old_root: str = NEW_ROOT, xdg_root: str | None = None,
                                  argv: list[str] | None = None,
                                  require_pre_globals: bool = True) -> None:
    """Native-only: relocate the data root from ~/.var/app/<id> (the flatpak-era
    location the pre-XDG builds used even outside flatpak) to
    $XDG_DATA_HOME/deckard, once, leaving a compat symlink behind.

    No-op under flatpak, where ~/.var/app/<id> IS the correct per-app data root
    and must not move. Call AFTER migrate() so a StreamController->Deckard rename
    lands in ~/.var/app/<id> first, then relocates here.

    Reuses migrate()'s crash-safe machinery with its own marker. Two deliberate
    departures from the cross-app rename:

    * No running-instance abort. This relocates the SAME app's tree; the compat
      symlink keeps a live instance's absolute-path writes unified with the moved
      data, so there is no split-writes hazard (and an abort-on-running check
      would never fire the migration for an always-autostarted instance). The one
      residual window is the microseconds between rename and symlink, where a live
      instance opening a *new* absolute path gets a transient ENOENT -- rare and
      non-fatal.
    * Cross-filesystem is skipped, not attempted. os.rename across filesystems
      (separate mounts, or btrfs subvolumes for ~/.var vs ~/.local) raises EXDEV,
      which migrate() would turn into a startup-aborting SystemExit. Skip instead;
      native_data_root() then keeps the app on ~/.var/app/<id>.
    """
    if _is_flatpak():
        return
    new_root = xdg_root or _xdg_root()
    if os.path.isdir(old_root) and not os.path.islink(old_root) \
            and not _same_filesystem(old_root, new_root):
        _log(f"{old_root} and {new_root} are on different filesystems; skipping XDG "
             f"relocation -- the app keeps using {old_root}")
        return
    migrate(old_root=old_root, new_root=new_root, argv=argv,
            require_pre_globals=require_pre_globals,
            marker_name=XDG_MARKER_NAME, running_check=lambda: False)
